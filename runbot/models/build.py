# -*- coding: utf-8 -*-
import glob
import logging
import operator
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import time
from subprocess import CalledProcessError
from ..common import dt2time, fqdn, now, grep, time2str, rfind, uniq_list, local_pgadmin_cursor, get_py_version
from ..container import docker_build, docker_run, docker_stop, docker_is_running, docker_get_gateway_ip, build_odoo_cmd
from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import config, appdirs

_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '
re_job = re.compile('_job_\d')

_logger = logging.getLogger(__name__)


class runbot_build(models.Model):
    
    _name = "runbot.build"
    _order = 'id desc'

    branch_id = fields.Many2one('runbot.branch', 'Branch', required=True, ondelete='cascade', index=True)
    repo_id = fields.Many2one(related='branch_id.repo_id', readonly=True, store=True)
    name = fields.Char('Revno', required=True)
    host = fields.Char('Host')
    port = fields.Integer('Port')
    dest = fields.Char(compute='_get_dest', type='char', string='Dest', readonly=1, store=True)
    domain = fields.Char(compute='_get_domain', type='char', string='URL')
    date = fields.Datetime('Commit date')
    author = fields.Char('Author')
    author_email = fields.Char('Author Email')
    committer = fields.Char('Committer')
    committer_email = fields.Char('Committer Email')
    subject = fields.Text('Subject')
    sequence = fields.Integer('Sequence')
    modules = fields.Char("Modules to Install")
    result = fields.Char('Result', default='')  # ok, ko, warn, skipped, killed, manually_killed
    guess_result = fields.Char(compute='_guess_result')
    pid = fields.Integer('Pid')
    state = fields.Char('Status', default='pending')  # pending, testing, running, done, duplicate, deathrow
    active_job = fields.Many2one('runbot.job', 'Job')
    job = fields.Char('Active job display name', compute='_compute_job')
    active_job_index = fields.Integer('Job index', default=-1, readonly=True)
    job_start = fields.Datetime('Job start')
    job_end = fields.Datetime('Job end')
    build_start = fields.Datetime('Build start')
    build_end = fields.Datetime('Build end')
    job_time = fields.Integer(compute='_get_time', string='Job time')
    job_age = fields.Integer(compute='_get_age', string='Job age')
    duplicate_id = fields.Many2one('runbot.build', 'Corresponding Build')
    server_match = fields.Selection([('builtin', 'This branch includes Odoo server'),
                                     ('match', 'This branch includes Odoo server'),
                                     ('default', 'No match found - defaults to master')],
                                    string='Server branch matching')
    revdep_build_ids = fields.Many2many('runbot.build', 'runbot_rev_dep_builds',
                                        column1='rev_dep_id', column2='dependent_id',
                                        string='Builds that depends on this build')
    extra_params = fields.Char('Extra cmd args')
    coverage = fields.Boolean('Enable code coverage')
    coverage_result = fields.Float('Coverage result', digits=(5, 2))
    build_type = fields.Selection([('scheduled', 'This build was automatically scheduled'),
                                   ('rebuild', 'This build is a rebuild'),
                                   ('normal', 'normal build'),
                                   ('indirect', 'Automatic rebuild'),
                                   ],
                                  default='normal',
                                  string='Build type')
    parent_id = fields.Many2one('runbot.build', 'Parent Build')
    children_ids = fields.One2many('runbot.build', 'parent_id')
    #job_type = fields.Selection([
    #    ('testing', 'Testing jobs only'),
    #    ('running', 'Running job only'),
    #    ('all', 'All jobs'),
    #    ('none', 'Do not execute jobs'),
    #])
    dependency_ids = fields.One2many('runbot.build.dependency', 'build_id')

    build_run_config = fields.Many2one('runbot.job.config', 'Run Config')
    run_config = fields.Many2one('runbot.job.config', 'Run Config', compute='_compute_run_config', inverse='_inverse_run_config')

    def _compute_job(self):
        for build in self:
            build.job = build.active_job.name

    def _compute_run_config(self):
        for build in self:
            if build.build_run_config:
                build.run_config = build.build_run_config
            else:
                build.run_config = build.branch_id.run_config

    def _inverse_run_config(self):
        for build in self:
            build.build_run_config = build.run_config

    def copy(self, values=None):
        raise UserError("Cannot duplicate build!")

    def create(self, vals):
        branch = self.env['runbot.branch'].search([('id', '=', vals.get('branch_id', False))])
        if branch.no_build:
            return self.env['runbot.build']
        vals['job_type'] = vals['job_type'] if 'job_type' in vals else branch.job_type
        build_id = super(runbot_build, self).create(vals)
        extra_info = {'sequence': build_id.id if not build_id.sequence else build_id.sequence}
        context = self.env.context

        # compute dependencies
        repo = build_id.repo_id
        dep_create_vals = []
        nb_deps = len(repo.dependency_ids)
        if not vals.get('dependency_ids'):
            for extra_repo in repo.dependency_ids:
                (build_closets_branch, match_type) = build_id.branch_id._get_closest_branch(extra_repo.id)
                closest_name = build_closets_branch.name
                closest_branch_repo = build_closets_branch.repo_id
                last_commit = closest_branch_repo._git_rev_parse(closest_name)
                dep_create_vals.append({
                    'build_id': build_id.id,
                    'dependecy_repo_id': extra_repo.id,
                    'closest_branch_id': build_closets_branch.id,
                    'dependency_hash': last_commit,
                    'match_type': match_type,
                })

        for dep_vals in dep_create_vals:
            self.env['runbot.build.dependency'].sudo().create(dep_vals)

        if not context.get('force_rebuild'):  # not vals.get('build_type') == rebuild': could be enough, but some cron on runbot are using this ctx key, to do later
            # detect duplicate
            duplicate_id = None
            domain = [
                ('repo_id', 'in', (build_id.repo_id.duplicate_id.id, build_id.repo_id.id)), # before, was only looking in repo.duplicate_id looks a little better to search in both
                ('id', '!=', build_id.id),
                ('name', '=', build_id.name),
                ('duplicate_id', '=', False),
                # ('build_type', '!=', 'indirect'),  # in case of performance issue, this little fix may improve performance a little but less duplicate will be detected when pushing an empty branch on repo with duplicates
                ('result', '!=', 'skipped'),
                ('job_type', '=', build_id.job_type),
            ]
            candidates = self.search(domain)
            if candidates and nb_deps:
                # check that all depedencies are matching.

                # Note: We avoid to compare closest_branch_id, because the same hash could be found on
                # 2 different branches (pr + branch). 
                # But we may want to ensure that the hash is comming from the right repo, we dont want to compare community
                # hash with enterprise hash.
                # this is unlikely to happen so branch comparaison is disabled
                self.env.cr.execute("""
                    SELECT DUPLIDEPS.build_id
                    FROM runbot_build_dependency as DUPLIDEPS
                    JOIN runbot_build_dependency as BUILDDEPS
                    ON BUILDDEPS.dependency_hash = DUPLIDEPS.dependency_hash
                    --AND BUILDDEPS.closest_branch_id = DUPLIDEPS.closest_branch_id -- only usefull if we are affraid of hash collision in different branches
                    AND BUILDDEPS.build_id = %s
                    AND DUPLIDEPS.build_id in %s
                    GROUP BY DUPLIDEPS.build_id
                    HAVING COUNT(DUPLIDEPS.*) = %s
                    ORDER BY DUPLIDEPS.build_id  -- remove this in case of performance issue, not so usefull
                    LIMIT 1
                """, (build_id.id, tuple(candidates.ids), nb_deps))
                filtered_candidates_ids = self.env.cr.fetchall()

                if filtered_candidates_ids:
                    duplicate_id = filtered_candidates_ids[0]
            else:
                duplicate_id = candidates[0].id if candidates else False

            if duplicate_id:
                extra_info.update({'state': 'duplicate', 'duplicate_id': duplicate_id})
                # maybe update duplicate priority if needed

        build_id.write(extra_info)
        if build_id.state == 'duplicate' and build_id.duplicate_id.state in ('running', 'done'):
            build_id._github_status()
        return build_id

    def write(self, values):
        res = super(runbot_build, self).write(values)
        # set result on parent
        result = values.get('result')
        if result and self.parent_id:
            loglevel = 'OK' if result == 'ok' else 'WARNING' if result == 'warn' else 'ERROR'
            self.parent_id._log('children_build', 'returned a "%s" result ' % (result), level=loglevel, ttype='subbuild', path=self.id)
            self.env['ir.logging'].search([('build_id', '=', self.parent_id.id), ('path', '=', self.id)]).write({'level': loglevel}) # or only first?
            # think to finish build if no running children and state done?
            if result != 'ok' and self.parent_id.result in ('ok', 'warn'):
                if result in ('ko', 'skipped'):
                    self.parent_id.result = 'ko'
                elif result == 'warn' and self.parent_id.result == 'ok':
                    self.parent_id.result = 'warn'
        return res

    def _reset(self):
        self.write({'state': 'pending'})

    @api.depends('name', 'branch_id.name')
    def _get_dest(self):
        for build in self:
            if build.name:
                nickname = build.branch_id.name.split('/')[2]
                nickname = re.sub(r'"|\'|~|\:', '', nickname)
                nickname = re.sub(r'_|/|\.', '-', nickname)
                build.dest = ("%05d-%s-%s" % (build.id, nickname[:32], build.name[:6])).lower()

    def _get_domain(self):
        domain = self.env['ir.config_parameter'].sudo().get_param('runbot.runbot_domain', fqdn())
        for build in self:
            if build.repo_id.nginx:
                build.domain = "%s.%s" % (build.dest, build.host)
            else:
                build.domain = "%s:%s" % (domain, build.port)

    def _guess_result(self):
        cr = self.env.cr
        testing_ids = [list(self.filtered(lambda b: b.state == 'testing').ids)]
        result = {}
        if testing_ids:
            cr.execute("""
                SELECT b.id,
                    CASE WHEN array_agg(l.level)::text[] && ARRAY['ERROR', 'CRITICAL'] THEN 'ko'
                            WHEN array_agg(l.level)::text[] && ARRAY['WARNING'] THEN 'warn'
                            ELSE 'ok'
                        END
                FROM runbot_build b
                LEFT JOIN ir_logging l ON (l.build_id = b.id AND l.level != 'INFO')
                    WHERE b.id = ANY(%s)
                GROUP BY b.id
            """, testing_ids)
            result = {row[0]: row[1] for row in cr.fetchall()}
        for build in self:
            build.guess_result = result.get(build.id, build.result)

    def _get_time(self):
        """Return the time taken by the tests"""
        for build in self:
            if build.job_end:
                build.job_time = int(dt2time(build.job_end) - dt2time(build.job_start))
            elif build.job_start:
                build.job_time = int(time.time() - dt2time(build.job_start))

    def _get_age(self):
        """Return the time between job start and now"""
        for build in self:
            if build.job_start:
                build.job_age = int(time.time() - dt2time(build.job_start))

    def _get_active_log_path(self):
        return self._path('logs', '%s.txt' % self.active_job)

    def _force(self, message=None):
        """Force a rebuild and return a recordset of forced builds"""
        forced_builds = self.env['runbot.build']
        for build in self:
            pending_ids = self.search([('state', '=', 'pending')], order='id', limit=1)
            if pending_ids:
                sequence = pending_ids[0].id
            else:
                sequence = self.search([], order='id desc', limit=1)[0].id
            # Force it now
            rebuild = True
            if build.state == 'done' and build.result == 'skipped':
                build.write({'state': 'pending', 'sequence': sequence, 'result': ''})
            # or duplicate it
            elif build.state in ['running', 'done', 'duplicate', 'deathrow']:
                new_build = build.with_context(force_rebuild=True).create({
                    'sequence': sequence,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'modules': build.modules,
                    'build_type': 'rebuild',
                    'build_run_config': build.build_run_config.id,
                })
                build = new_build
            else:
                rebuild = False
            if rebuild:
                forced_builds |= build
                user = request.env.user if request else self.env.user
                build._log('rebuild', 'Rebuild initiated by %s' % user.name)
                if message:
                    build._log('rebuild', message)
        return forced_builds

    def _skip(self, reason=None):
        """Mark builds ids as skipped"""
        if reason:
            self._logger('skip %s', reason)
        self.write({'state': 'done', 'result': 'skipped'})
        to_unduplicate = self.search([('id', 'in', self.ids), ('duplicate_id', '!=', False)])
        to_unduplicate._force()
    def _local_cleanup(self):
        for build in self:
            # Cleanup the *local* cluster
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname LIKE %s
                """, [build.dest + '%'])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(db)

        # cleanup: find any build older than 7 days.
        root = self.env['runbot.repo']._root()
        build_dir = os.path.join(root, 'build')
        builds = os.listdir(build_dir)
        if builds:
            self.env.cr.execute("""
                SELECT dest
                  FROM runbot_build
                 WHERE dest IN %s
                   AND (state != 'done' OR job_end > (now() - interval '7 days'))
            """, [tuple(builds)])
            actives = set(b[0] for b in self.env.cr.fetchall())

            for b in builds:
                path = os.path.join(build_dir, b)
                if b not in actives and os.path.isdir(path) and os.path.isabs(path):
                    shutil.rmtree(path)

        # cleanup old unused databases
        self.env.cr.execute("select id from runbot_build where state in ('testing', 'running')")
        db_ids = [id[0] for id in self.env.cr.fetchall()]
        if db_ids:
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname ~ '^[0-9]+-.*'
                       AND SUBSTRING(datname, '^([0-9]+)-.*')::int not in %s

                """, [tuple(db_ids)])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(db)

    def _list_jobs(self):
        """List methods that starts with _job_[[:digit:]]"""
        return sorted(job[1:] for job in dir(self) if re_job.match(job))

    def _find_port(self):
        # currently used port
        ids = self.search([('state', 'not in', ['pending', 'done'])])
        ports = set(i['port'] for i in ids.read(['port']))

        # starting port
        icp = self.env['ir.config_parameter']
        port = int(icp.get_param('runbot.runbot_starting_port', default=2000))

        # find next free port
        while port in ports:
            port += 3
        return port

    def _logger(self, *l):
        l = list(l)
        for build in self:
            l[0] = "%s %s" % (build.dest, l[0])
            _logger.debug(*l)

    def _get_docker_name(self):
        self.ensure_one()
        return '%s_%s' % (self.dest, self.active_job.name)

    def _schedule(self):
        """schedule the build"""
        jobs = self._list_jobs()

        icp = self.env['ir.config_parameter']
        # For retro-compatibility, keep this parameter in seconds

        for build in self:
            if build.state == 'deathrow':
                build._kill(result='manually_killed')
                continue

            if build.state == 'pending':
                # allocate port and schedule first job
                port = self._find_port()
                values = {
                    'host': fqdn(),
                    'port': port,
                    'job_start': now(),
                    'build_start': now(),
                    'job_end': False,
                }
                values.update(build._next_job_values())
                build.write(values)
                if not build.active_job:
                    build._log('schedule', 'No job in config, doing nothing')
                    continue
                try:
                    build._log('init', 'Init build environment with config %s ' % build.run_config.name)
                    # notify pending build - avoid confusing users by saying nothing
                    build._github_status()
                    build._checkout()
                    build._log('docker_build', 'Building docker image')
                    docker_build(build._path('logs', 'docker_build.txt'), build._path())
                except Exception:
                    _logger.exception('Failed initiating build %s', build.dest)
                    build._log('Failed initiating build')
                    build._kill(result='ko')
                    continue
               
            else:  # testing/running build
                # check if current job is finished
                if docker_is_running(build._get_docker_name()):
                    timeout = min(build.active_job.cpu_limit, int(icp.get_param('runbot.runbot_timeout', default=10000)))
                    if build.state != 'running' and build.job_time > timeout:
                        build._logger('%s time exceded (%ss)', build.active_job.name if build.active_job else "?", build.job_time)
                        build.write({'job_end': now()})
                        build._kill(result='killed')
                    else:
                        # failfast
                        if not build.result and build.guess_result in ('ko', 'warn'):
                            build.result = build.guess_result # Could be more interesting to do that on logging creation?
                            build._github_status()
                    continue
                # No job running, make result and select nex job
                build_values = {
                    'job_end': now(),
                }
                if build.active_job.job_type != 'create_build':
                    build._log('end_job', 'Job %s finished in %s' % (build.job, build.job_time))
                # make result of previous job
                if build.active_job.coverage:
                    build._log('coverage_result', 'Start getting coverage result')
                    cov_path = build._path('coverage/index.html')
                    if os.path.exists(cov_path):
                        with open(cov_path,'r') as f:
                            data = f.read()
                            covgrep = re.search(r'pc_cov.>(?P<coverage>\d+)%', data)
                            build_values['coverage_result'] = covgrep and covgrep.group('coverage') or False
                    else:
                        build._log('coverage_result', 'Coverage file not found')

                if build.active_job.test_enable or build.active_job.test_tags:
                    build._log('run', 'Getting results for build %s' % build.dest)
                    log_file = build._get_active_log_path()
                    log_time = time.localtime(os.path.getmtime(log_file))
                    build_values = {
                        'job_end': time2str(log_time),
                    }
                    if not build.result or build.result in ['ok', "warn"]:
                        if grep(log_file, ".modules.loading: Modules loaded."):
                            if rfind(log_file, _re_error):
                                build_values['result'] = 'ko'
                            elif build.result == 'ok' and rfind(log_file, _re_warning):
                                build_values['result'] = 'warn'
                            elif not grep(build._server("test/common.py"), "post_install") or grep(log_file, "Initiating shutdown."):
                                build_values['result'] = 'ok'
                        else:
                            build_values['result'] = 'ko'

                # Non running build in
                build._logger('%s finished', build.active_job.name)

                # select next job
                #try:
                build_values.update(build._next_job_values()) # find next job or set to done
                #except: # editing configuration while build using it is running.
                #    build._log('next_job', 'Unexpected error while searching next job to execute')
                #    build.write({'job_end': now()})
                #    build._kill(result='ko')
                #    continue

                build.write(build_values)
                build._github_status()

            # run job
            pid = None
            if build.state != 'done':
                build._logger('running %s', build.active_job.name)
                os.makedirs(build._path('logs'), exist_ok=True)
                os.makedirs(build._path('datadir'), exist_ok=True)
                log_path = build._get_active_log_path()
                try:
                    pid = build.active_job.run(build, log_path) # run should be on build?
                    build.write({'pid': pid})
                except Exception:
                    _logger.exception('%s failed running method %s', build.dest, build.active_job.name)
                    build._log("", "Failed running job %s, see runbot log" % build.active_job.name)
                    build._kill(result='ko')
                    continue

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build._local_cleanup()

    def _path(self, *l, **kw):
        """Return the repo build path"""
        self.ensure_one()
        build = self
        root = self.env['runbot.repo']._root()
        return os.path.join(root, 'build', build.dest, *l)

    def _server(self, *l, **kw): # not really build related, specific to odoo version, could be a data
        """Return the build server path"""
        self.ensure_one()
        build = self
        if os.path.exists(build._path('odoo')):
            return build._path('odoo', *l)
        return build._path('openerp', *l)

    def _filter_modules(self, modules, available_modules, explicit_modules):
        blacklist_modules = set(['auth_ldap', 'document_ftp', 'base_gengo',
                                 'website_gengo', 'website_instantclick',
                                 'pad', 'pad_project', 'note_pad',
                                 'pos_cache', 'pos_blackbox_be'])

        mod_filter = lambda m: (
            m in available_modules and
            (m in explicit_modules or (not m.startswith(('hw_', 'theme_', 'l10n_')) and
                                       m not in blacklist_modules))
        )
        return uniq_list(filter(mod_filter, modules))

    def _checkout(self):
        self.ensure_one() # will raise exception if hash not found, we don't want to fail for all build.
        # starts from scratch
        build = self
        if os.path.isdir(build._path()):
            shutil.rmtree(build._path())

        # runbot log path
        os.makedirs(build._path("logs"), exist_ok=True)
        os.makedirs(build._server('addons'), exist_ok=True)

        # update repo if needed
        if not build.repo_id._hash_exists(build.name):
            build.repo_id._update(build.repo_id)

        # checkout branch
        build.branch_id.repo_id._git_export(build.name, build._path())

        has_server = os.path.isfile(build._server('__init__.py'))
        server_match = 'builtin'

        # build complete set of modules to install
        modules_to_move = []
        modules_to_test = ((build.branch_id.modules or '') + ',' +
                            (build.repo_id.modules or ''))
        modules_to_test = list(filter(None, modules_to_test.split(',')))  # ???
        explicit_modules = set(modules_to_test)
        _logger.debug("manual modules_to_test for build %s: %s", build.dest, modules_to_test)

        if not has_server:
            if build.repo_id.modules_auto == 'repo':
                modules_to_test += [
                    os.path.basename(os.path.dirname(a))
                    for a in (glob.glob(build._path('*/__openerp__.py')) +
                                glob.glob(build._path('*/__manifest__.py')))
                ]
                _logger.debug("local modules_to_test for build %s: %s", build.dest, modules_to_test)

            # todo make it backward compatible, or create migration script?
            for build_dependency in build.dependency_ids:
                closest_branch = build_dependency.closest_branch_id
                latest_commit = build_dependency.dependency_hash
                repo = closest_branch.repo_id
                closest_name = closest_branch.name
                if build_dependency.match_type == 'default':
                    server_match = 'default'
                elif server_match != 'default':
                    server_match = 'match'

                build._log(
                    'Building environment',
                    '%s match branch %s of %s' % (build_dependency.match_type, closest_name, repo.name)
                )
                if not repo._hash_exists(latest_commit):
                    repo._update(force=True)
                if not repo._hash_exists(latest_commit):
                    repo._git(['fetch', 'origin', latest_commit])
                if not repo._hash_exists(latest_commit):
                    build._log('_checkout',"Dependency commit %s in repo %s is unreachable" % (latest_commit, repo.name))
                    raise Exception

                commit_oneline = repo._git(['show', '--pretty="%H -- %s"', '-s', latest_commit]).strip()
                build._log(
                    'Building environment',
                    'Server built based on commit %s from %s' % (commit_oneline, closest_name)
                )
                repo._git_export(latest_commit, build._path())

            # Finally mark all addons to move to openerp/addons
            modules_to_move += [
                os.path.dirname(module)
                for module in (glob.glob(build._path('*/__openerp__.py')) +
                                glob.glob(build._path('*/__manifest__.py')))
            ]

        # move all addons to server addons path
        for module in uniq_list(glob.glob(build._path('addons/*')) + modules_to_move):
            basename = os.path.basename(module)
            addon_path = build._server('addons', basename)
            if os.path.exists(addon_path):
                build._log(
                    'Building environment',
                    'You have duplicate modules in your branches "%s"' % basename
                )
                if os.path.islink(addon_path) or os.path.isfile(addon_path):
                    os.remove(addon_path)
                else:
                    shutil.rmtree(addon_path)
            shutil.move(module, build._server('addons'))

        available_modules = [
            os.path.basename(os.path.dirname(a))
            for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                        glob.glob(build._server('addons/*/__manifest__.py')))
        ]
        if build.repo_id.modules_auto == 'all' or (build.repo_id.modules_auto != 'none' and has_server):
            modules_to_test += available_modules

        modules_to_test = self._filter_modules(modules_to_test,
                                                set(available_modules), explicit_modules)
        _logger.debug("modules_to_test for build %s: %s", build.dest, modules_to_test)
        build.write({'server_match': server_match,
                        'modules': ','.join(modules_to_test)})

    def _local_pg_dropdb(self, dbname):
        with local_pgadmin_cursor() as local_cr:
            pid_col = 'pid' if local_cr.connection.server_version >= 90200 else 'procpid'
            query = 'SELECT pg_terminate_backend({}) FROM pg_stat_activity WHERE datname=%s'.format(pid_col)
            local_cr.execute(query, [dbname])
            local_cr.execute('DROP DATABASE IF EXISTS "%s"' % dbname)
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname) for pn in 'OpenERP Odoo'.split()]
        cmd = ['rm', '-rf'] + paths
        _logger.debug(' '.join(cmd))
        subprocess.call(cmd)

    def _local_pg_createdb(self, dbname):
        self._local_pg_dropdb(dbname)
        _logger.debug("createdb %s", dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("""CREATE DATABASE "%s" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""" % dbname)

    def _log(self, func, message, level='INFO', ttype='runbot', path='runbot'):
        self.ensure_one()
        _logger.debug("Build %s %s %s", self.id, func, message)
        self.env['ir.logging'].create({
            'build_id': self.id,
            'level': level,
            'type': ttype,
            'name': 'odoo.runbot',
            'message': message,
            'path': path,
            'func': func,
            'line': '0',
        })

    def reset(self):
        self.write({'state': 'pending'})

    def _reap(self):
        while True:
            try:
                pid, status, rusage = os.wait3(os.WNOHANG)
            except OSError:
                break
            if pid == 0:
                break
            _logger.debug('reaping: pid: %s status: %s', pid, status)

    def _kill(self, result=None):
        host = fqdn()
        for build in self:
            if build.host != host:
                continue
            build._log('kill', 'Kill build %s' % build.dest)
            docker_stop(build._get_docker_name())
            v = {'state': 'done', 'active_job': False}
            if result:
                v['result'] = result
            build.write(v)
            self.env.cr.commit()
            build._github_status()
            build._local_cleanup()

    def _ask_kill(self):
        self.ensure_one()
        user = request.env.user if request else self.env.user
        uid = user.id
        build = self.duplicate_id if self.state == 'duplicate' else self
        if build.state == 'pending':
            build._skip()
            build._log('_ask_kill', 'Skipping build %s, requested by %s (user #%s)' % (build.dest, user.name, uid))
        elif build.state in ['testing', 'running']:
            build.write({'state': 'deathrow'})
            build._log('_ask_kill', 'Killing build %s, requested by %s (user #%s)' % (build.dest, user.name, uid))

    def _cmd(self): # why not remove build.modules output ? 
        """Return a tuple describing the command to start the build
        First part is list with the command and parameters
        Second part is a list of Odoo modules
        """
        self.ensure_one()
        build = self
        bins = [
            'odoo-bin',                 # >= 10.0
            'openerp-server',           # 9.0, 8.0
            'openerp-server.py',        # 7.0
            'bin/openerp-server.py',    # < 7.0
        ]
        for odoo_bin in bins:
            if os.path.isfile(build._path(odoo_bin)):
                break

        # commandline
        cmd = [ os.path.join('/data/build', odoo_bin), ]
        # options
        if grep(build._server("tools/config.py"), "no-xmlrpcs"): # move that to configs ?
            cmd.append("--no-xmlrpcs") 
        if grep(build._server("tools/config.py"), "no-netrpc"): 
            cmd.append("--no-netrpc")
        if grep(build._server("tools/config.py"), "log-db"):
            logdb_uri = self.env['ir.config_parameter'].get_param('runbot.runbot_logdb_uri')
            logdb = self.env.cr.dbname
            if logdb_uri and grep(build._server('sql_db.py'), 'allow_uri'):
                logdb = '%s' % logdb_uri
            cmd += ["--log-db=%s" % logdb]
            if grep(build._server('tools/config.py'), 'log-db-level'):
                cmd += ["--log-db-level", '25']

        if grep(build._server("tools/config.py"), "data-dir"):
            datadir = build._path('datadir')
            if not os.path.exists(datadir):
                os.mkdir(datadir)
            cmd += ["--data-dir", '/data/build/datadir']

        # if build.branch_id.test_tags:
        #    cmd.extend(['--test_tags', "'%s'" % build.branch_id.test_tags])  # keep for next version

        return cmd, build.modules


    def _github_status_notify_all(self, status):
        """Notify each repo with a status"""
        self.ensure_one()
        commits = {(b.repo_id, b.name) for b in self.search([('name', '=', self.name)])}
        for repo, commit_hash in commits:
            _logger.debug("github updating %s status %s to %s in repo %s", status['context'], commit_hash, status['state'], repo.name)
            repo._github('/repos/:owner/:repo/statuses/%s' % commit_hash, status, ignore_errors=True)

    def _github_status(self):
        """Notify github of failed/successful builds"""
        if self.run_config.update_github_state:
            runbot_domain = self.env['runbot.repo']._domain()
            for build in self:
                desc = "runbot build %s" % (build.dest,)
                if build.state == 'testing':
                    state = 'pending'
                elif build.state in ('running', 'done'):
                    state = 'error'
                else:
                    continue
                desc += " (runtime %ss)" % (build.job_time,)
                if build.result == 'ok':
                    state = 'success'
                if build.result in ('ko', 'warn'):
                    state = 'failure'
                status = {
                    "state": state,
                    "target_url": "http://%s/runbot/build/%s" % (runbot_domain, build.id),
                    "description": desc,
                    "context": "ci/runbot"
                }
                build._github_status_notify_all(status)

    def _next_job_values(self):
        self.ensure_one()
        ordered_jobs = list(self.run_config.jobs)
        if not ordered_jobs:
            return {'active_job': False, 'state':'done', 'result': self.result or self.guess_result}

        index = self.active_job_index
        next_index = index+1
        if next_index >= len(ordered_jobs):
            return {'active_job': False, 'active_job_index': next_index, 'state':'done', 'result': self.result or self.guess_result}

        new_job = ordered_jobs[next_index]
        return {'active_job': new_job.id, 'active_job_index': next_index, 'state': new_job.job_state()}
      

class runbot_build_dependency(models.Model):
    _name = "runbot.build.dependency"

    build_id = fields.Many2one('runbot.build', 'Build', required=True, ondelete='cascade', index=True)
    dependecy_repo = fields.Many2one('runbot.repo', 'Dependency repo', required=True, ondelete='cascade')
    # build_hash = fields.Char(related=build_id.name, required=True, index=True, autojoin=True)
    dependency_hash = fields.Char('Name of commit', index=True)
    # just in case store branch
    closest_branch_id = fields.Many2one('runbot.branch', 'Branch', required=True, ondelete='cascade')
    match_type = fields.Char('Match Type')

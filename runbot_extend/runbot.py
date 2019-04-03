# -*- encoding: utf-8 -*-

import glob
import io
import logging
import re
import time
import os
import datetime

from odoo.addons.runbot.models.build import runbot_job, _re_error, _re_warning, re_job
from odoo import models, fields, api, _
from odoo.addons.runbot.container import docker_build, docker_run, build_odoo_cmd
from odoo.addons.runbot.common import dt2time, fqdn, now, grep, time2str, rfind, uniq_list, local_pgadmin_cursor, get_py_version



_logger = logging.getLogger(__name__)


class RunbotJob(models.Model):
    _name = "runbot.job"

    name = fields.Char(required=True)
    sequence = fields.Integer()
    logs_location = fields.Char(string="Log file location", compute="compute_logs_location")
    logs_name = fields.Char(string="Log name")
    logs_path = fields.Char(string="Log path")
    logs_filename = fields.Char(string="Log filename")
    is_default_parsed = fields.Boolean(default=False)
    can_be_parsed = fields.Boolean(default=False)
    logs_can_be_accessed = fields.Boolean(default=False)


    @api.depends('logs_path', 'logs_name')
    def compute_logs_location(self):
        for rec in self:
            rec.logs_location = '%s/%s' % (rec.logs_path, rec.logs_filename)

class runbot_repo(models.Model):
    _inherit = "runbot.repo"

    nobuild = fields.Boolean(default=False)
    skip_job_ids = fields.Many2many('runbot.job', 'runbot_job_runbot_repo_skip_rel', string='Jobs to skip')
    parse_job_ids = fields.Many2many('runbot.job', 'runbot_job_runbot_repo_parse_rel', string='Jobs to parse',
                                     domain="[('can_be_parsed','=',True)]", default=lambda self: self.env['runbot.job'].search([('is_default_parsed', '=', True)]))
    log_access_job_ids = fields.Many2many('runbot.job', 'runbot_job_runbot_repo_log_access_rel', string='Jobs to be accessed',
                                     domain="[('logs_can_be_accessed','=',True)]", default=lambda self: self.env['runbot.job'].search([('logs_can_be_accessed', '=', True)]))
    restored_db_name = fields.Char(string='Database name to replicated')
    force_update_all = fields.Boolean('Force Update ALL', help='Force update all on job_26 otherwise it will update only the modules in the repository', default=False)
    testenable_job26 = fields.Boolean(
        'Test enable on upgrade', help='test enabled on update of the restored database', default=False)
    force_coverage = fields.Boolean(string='Force coverage', help='Coverage is ran nightly on sticky branches', default=False)
    custom_coverage = fields.Char(string='Custom coverage repository',
                                  help='Use --include arg on coverage: list of file name patterns, for example *addons/module1*,*addons/module2*')
    custom_parse_ids = fields.One2many('runbot.job.parse', 'repo_id', string='Custom parse')

    @api.onchange('parse_job_ids')
    def _onchange_parse_jo(self):
        new_job_ids = self.parse_job_ids - self.custom_parse_ids.mapped('job_id')
        removed_job_ids = self.custom_parse_ids.mapped('job_id') - self.parse_job_ids
        for new_job_id in new_job_ids:
            for parse_type in ['error', 'warning']:
                self.custom_parse_ids += self.env['runbot.job.parse'].new({
                    'repo_id': self.id,
                    'job_id': new_job_id.id,
                    'parse_type': parse_type,
                    'regex_id': self.env.ref('runbot_extend.regex_%s' % parse_type).id,
                })
        if removed_job_ids:
            self.custom_parse_ids -= self.custom_parse_ids.filtered(lambda r: r.job_id in removed_job_ids)

class runbot_repo(models.Model):
    _name = "runbot.job.parse"
    _rec_order = "repo_id, job_id"

    repo_id = fields.Many2one('runbot.repo', string='Repository', required=True, ondelete='cascade')
    job_id = fields.Many2one('runbot.job', string='Job', domain="[('can_be_parsed', '=', True), ('id', 'in', parent.parse_job_ids)]", required=True, ondelete='cascade')
    regex_id = fields.Many2one('runbot.regex', string='Regex desc.')
    regex = fields.Char(related='regex_id.regex', string='Regex', readonly=True)
    parse_type = fields.Selection(string="Result type", required=True, selection=[('warning', 'Warning - Yellow'), ('error', 'Error - Red')])

    _sql_constraints = [
        ('runbot_job_parse_uniq',
         'unique (repo_id,job_id,parse_type)',
         'You can only chose 1 type of result type per job.')
    ]

    @api.onchange('parse_type')
    def _onchange_parse_type(self):
        if self.parse_type in ['error', 'warning']:
            self.regex_id = self.env.ref('runbot_extend.regex_%s' % self.parse_type)

class runbot_repo(models.Model):
    _name = "runbot.regex"

    name = fields.Char(required=True)
    regex = fields.Char(required=True)

class runbot_branch(models.Model):
    _inherit = "runbot.branch"

    def _get_branch_quickconnect_url(self, fqdn, dest):
        self.ensure_one()
        if self.repo_id.restored_db_name:
            r = {}
            r[self.id] = "http://%s/web/login?db=%s-custom&login=admin&redirect=/web?debug=1" % (
                fqdn, dest)
        else:
            r = super(runbot_branch, self)._get_branch_quickconnect_url(
                fqdn, dest)
        return r

class runbot_build(models.Model):
    _inherit = "runbot.build"

    restored_db_name = fields.Char(string='Database name to replicated')

    def create(self, vals):
        build_id = super(runbot_build, self).create(vals)
        if build_id.repo_id.restored_db_name:
            build_id.write(
                {'restored_db_name': build_id.repo_id.restored_db_name})
        if build_id.repo_id.nobuild:
            build_id.write({'state': 'done'})
        return build_id

    def _list_jobs(self):
        all_jobs = super(runbot_build, self)._list_jobs()
        jobs = self._clean_jobs(all_jobs)
        return jobs

    def _clean_jobs(self, jobs):
        self.ensure_one()
        jobs = jobs[:]
        for job_to_skip in self.repo_id.skip_job_ids:
            jobs.remove(job_to_skip.name)
        return jobs

    @runbot_job('testing', 'running')
    def _job_29_results(self, build, log_path):

        build._log('run', 'Getting results for build %s' % build.dest)
        v = {}
        result = []
        for job_id in build.repo_id.parse_job_ids:
            log_all = build._path(job_id.logs_path, job_id.logs_filename)
            log_time = time.localtime(os.path.getmtime(log_all))
            v['job_end'] = time2str(log_time)
            if grep(log_all, ".modules.loading: Modules loaded."):
                error_custom_parse = self.env['runbot.job.parse'].search([
                    ('repo_id', '=', build.repo_id.id),
                    ('job_id', '=', job_id.id),
                    ('parse_type', '=', 'error'),
                ], limit=1)
                warning_custom_parse = self.env['runbot.job.parse'].search([
                    ('repo_id', '=', build.repo_id.id),
                    ('job_id', '=', job_id.id),
                    ('parse_type', '=', 'warning'),
                ], limit=1)
                if rfind(log_all, r'' + error_custom_parse.regex_id.regex or _re_error):
                    result.append("ko")
                elif rfind(log_all, r'' + warning_custom_parse.regex_id.regex or _re_warning):
                    result.append("warn")
                elif not grep(build._server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                    result.append("ok")
            else:
                result.append("ko")
        if 'ko' in result:
            v['result'] = 'ko'
        elif 'warn' in result:
            v['result'] = 'warn'
        else:
            v['result'] = 'ok'
        build.write(v)
        build._github_status()
        return -2

    @runbot_job('testing')
    def _job_21_coverage_html(self, build, log_path):
        if not build.repo_id.custom_coverage:
            return super(runbot_build, self)._job_21_coverage_html(build, log_path)
        if not build.coverage:
            return -2
        build._log('coverage_html', 'Start generating custom coverage html')
        cov_path = build._path('coverage')
        os.makedirs(cov_path, exist_ok=True)
        cmd = [get_py_version(build), "-m", "coverage", "html",
               "-d", "/data/build/coverage", "--include %s" % build.repo_id.custom_coverage, "--ignore-errors"]
        return docker_run(build_odoo_cmd(cmd), log_path, build._path(), build._get_docker_name())

    @runbot_job('testing', 'running')
    def _job_25_restore(self, build, log_path):
        if not build.restored_db_name:
            return -2
        build._log('restore', 'Restoring %s on %s-custom' %
                   (build.restored_db_name, build.dest))
        cmd = "createdb -T %s %s-custom" % (
            build.restored_db_name, build.dest)
        return docker_run(cmd, log_path, build._path(), build._get_docker_name())

    @runbot_job('testing', 'running')
    def _job_26_upgrade(self, build, log_path):
        if not build.restored_db_name:
            return -2
        to_test = build.modules if build.modules and not build.repo_id.force_update_all else 'all'
        cmd, mods = build._cmd()
        build._log('upgrade', 'Start Upgrading %s modules on %s-custom' % (to_test, build.dest))
        cmd += ['-d', '%s-custom' % build.dest, '-u', to_test, '--stop-after-init', '--log-level=info']
        if build.repo_id.testenable_job26:
            cmd.append("--test-enable")
        return docker_run(build_odoo_cmd(cmd), log_path, build._path(), build._get_docker_name())


    @api.model
    def _cron_create_coverage_build(self, hostname):
        if hostname != fqdn():
            return 'Not for me'
        def prefixer(message, prefix):
            m = '[' in message and message[message.index('['):] or message
            if m.startswith(prefix):
                return m
            return '%s%s' % (prefix, m)
        branch_ids = self.env['runbot.branch'].search([
            ('sticky', '=', True),
            ('repo_id.nobuild', '=', False),
            ('repo_id.force_coverage', '=', True)], order='id')
        for branch_id in branch_ids:
            for last_build in self.search([('branch_id', '=', branch_id.id)], limit=1, order='sequence desc'):
                last_build.with_context(force_rebuild=True).create({
                    'branch_id': last_build.branch_id.id,
                    'date': datetime.datetime.now(),
                    'name': last_build.name,
                    'author': last_build.author,
                    'author_email': last_build.author_email,
                    'committer': last_build.committer,
                    'committer_email': last_build.committer_email,
                    'subject': prefixer(last_build.subject, '(coverage)'),
                    'modules': last_build.modules,
                    'extra_params': '',
                    'coverage': True,
                    'job_type': 'testing',
                    'build_type': 'scheduled',
                })

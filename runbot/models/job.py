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

_logger = logging.getLogger(__name__)

class Config(models.Model):
    _name = "runbot.job.config"
    _inherit = "mail.thread"

    name = fields.Char('Db name postfix to use', required=True, unique=True, tracking=True)
    description = fields.Char('Config description')
    jobs = fields.Many2many('runbot.job', help="")
    update_github_state = fields.Boolean('Notify build state to github', default=False, tracking=True)
    protected = fields.Boolean('Protected', default=False, tracking=True)

    def write(self, values):
        #if self.protected and not (self.env.user.has_group('runbot.group_job_administrator') and values.get('protected') is False): # or check that it is used on any config linked to a repo or a sticky branch? 
        #    raise UserError('Record is protected, protection can only be removed by Job Administrators')
        res = super(Config, self).write(values)
        self._check_jobs_order()
        return res

    def create(self, values):
        res = super(Config, self).create(values)
        res._check_jobs_order()
        return res

    def _check_jobs_order(self):
        for job in self.jobs:
            if job != self.jobs[-1] and job.job_type == 'run_odoo':
                raise UserError('Jobs of type run_odoo should be the last one')

class Job(models.Model):
    _name = 'runbot.job'
    _inherit = 'mail.thread'
    _order = 'sequence, id desc'

    #general info
    name = fields.Char('Db name postfix to use', required=True, unique=True, tracking=True, help="Unique name for job, please use trigram as postfix for cutom jobs")
        #nome need to be unique art least in one config, mainly to keep database name/coverage result folder unique. 
        #todo needs to sanitize it
    job_type = fields.Selection([
        ('test_odoo', 'Test odoo'),
        ('run_odoo', 'Run odoo'),
        ('python', 'Python code'),
        ('create_build', 'Create build'),
    ], default='test_odoo', required=True, tracking=True)
    protected = fields.Boolean('Protected', default=False, tracking=True)
    sequence = fields.Integer('Sequence', default=100, tracking=True) # or run after? # or in many2many rel?
    #odoo
    create_db = fields.Boolean('Create Db', default=True, tracking=True)
    install_modules = fields.Char('Modules to install', help="List of module to install, use * for all modules") # could be based on branch/repo (join) or
    custom_db_name = fields.Char('Custom Db Name', tracking=True)
    db_name = fields.Char('Db Name', compute='_compute_db_name', inverse='_inverse_db_name', tracking=True)
    cpu_limit = fields.Integer('Cpu limit', default=2400, tracking=True)
    coverage = fields.Boolean('Coverage', dafault=False, tracking=True)
    test_enable = fields.Boolean('Test enable', default=False, tracking=True)
    test_tags = fields.Char('Test tags', help="comma separated list of test tags")
    extra_params = fields.Char('Extra cmd args', tracking=True)
    # python
    python_code = fields.Text('Python code', tracking=True, default="# type python code here\n\n\n\n\n\n")
    running_job = fields.Boolean('Job final state is running', default=False, help="Docker won't be killed if checked")
    # create_build
    create_config_ids = fields.Many2many('runbot.job.config', string='New Build Configs', tracking=True, index=True)
    number_builds = fields.Integer('Number of build to create', default=1, tracking=True)
    # CARE TO RECUSION

    @api.depends('name', 'custom_db_name')
    def _compute_db_name(self):
        for job in self:
            job.db_name = job.custom_db_name or job.name

    def _inverse_db_name(self):
        for job in self:
            job.custom_db_name = job.db_name

    #@api.constrains('python_code')
    #def _check_python_code(self):
    #    for action in self.sudo().filtered('code'):
    #        msg = test_python_expr(expr=action.code.strip(), mode="exec")
    #        if msg:
    #            raise ValidationError(msg)

    def copy(self):
        #remove protection on copy
        copy = super(Job, self).copy()
        copy._write({'protected':False})
        return copy

    def create(self, values):
        self.check(values)
        return super(Job, self).create(values)

    def write(self, values):
        #if self.protected and not (self.env.user.has_group('runbot.group_job_administrator') and values.get('protected') is False): # or check that it is used on any config linked to a repo or a sticky branch? 
        #    raise UserError('Record is protected, protection can only be removed by Job Administrators')
        self.check(values)
        return super(Job, self).write(values)

    def unlink(self):
        if self.protected:
            raise UserError()
        super(Job, self).unlink()

    def check(self, values):
        if 'name 'in values:
            name_reg = r'^[a-zA-Z0-9\-_]*$'
            if not re.match(name_reg, values.get('name')):
                raise UserError('Name cannot contain special char or spaces exepts "_" and "-"')
        if not self.env.user.has_group('runbot.group_job_administrator'):
            if (values.get('job_type') == 'python' or ('python_code' in values and values['python_code'])):
                raise UserError('cannot create or edit job of type python code')
            if (values.get('extra_params')): # todo check extra params:
                reg = r'^[a-zA-Z0-9\-_ "]*$'
                if not re.match(reg, values.get('extra_params')):
                    _logger.log('%s tried to create an non supported test_param %s' % (self.env.user.name, values.get('extra_params')))
                    #if not re.match())
                    raise UserError('Cannot add extra_params on jobs')

    def run(self, build, log_path):
        build.write({'job_start': now(), 'job_end': False}) # state, ...
        if self.job_type == 'run_odoo':
            return self._run_odoo_run(build, log_path)
        if self.job_type == 'test_odoo':
            return self._run_odoo_test(build, log_path)
        elif self.job_type == 'python':
            return self._run_python(build, log_path)
        elif self.job_type == 'create_build':
            return self.create_build(build, log_path)

    def create_build(self, build, log_path):
        # todo: display number of running/pending subbuild on /repo
        # todo: kill all subbuild when killing buil
        # todo: bunch of things about parent/children

        # should we add a limit to avoid explosion of configs? 
        for i in range(self.number_builds):
            for create_config in self.create_config_ids:
                children = self.env['runbot.build'].create({
                    'dependency_ids': [(4, did) for did in build.dependency_ids],
                    'run_config_id': create_config.id,
                    'parent_id': build.id,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'build_type': build.build_type,
                    'author': build.author,
                    'date': build.date,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'modules': build.modules,
                })
                build._log('create_build', 'created with config %s' % create_config.name, ttype='subbuild', path=str(children.id))

    def _run_python(self, build, log_path):
        pass

    def _run_odoo_test(self, build, log_path): # in config or on build?
        job_config = self
        build._log('test_%s'% job_config.name, 'Start test %s' % job_config.name )
        cmd, _ = build._cmd()
        # create db if needed
        db_name = "%s-%s" % (build.dest, job_config.db_name)
        if job_config.create_db:
            build._local_pg_createdb("%s-%s" % (build.dest, job_config.db_name))
        cmd += ['-d', db_name]
        # list module to install
        modules_to_install = set([mod.strip() for mod in job_config.install_modules.split(',')])
        if '*' in modules_to_install:
            default_mod = set([mod.strip() for mod in build.modules.split(',')])
            modules_to_install = default_mod | modules_to_install
            #todo add without support
        mods = ",".join(modules_to_install)
        if mods:
            cmd += ['-i', mods]
        if job_config.test_enable:
            if grep(build._server("tools/config.py"), "test-enable"):
                cmd.extend(['--test-enable', '--log-level=test'])
            else: 
                build._log('test_all', 'Installing modules without testing')
        if job_config.test_tags:
                test_tags = job_config.test_tags.replace(' ','')
                cmd.extend(['--test-enable', test_tags])
        
        cmd += ['--stop-after-init'] # test job should always finish
        cmd += ['--log-level=test', '--max-cron-threads=0']
        if job_config.extra_params: # replace build extraparams, to check what can be done, merge, or remobe build extra_params support
            cmd.extend(shlex.split(job_config.extra_params))
        if job_config.coverage:
            #cpu_limit *= 1.5
            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                          glob.glob(build._server('addons/*/__manifest__.py')))
            ]
            bad_modules = set(available_modules) - set((mods or '').split(','))
            omit = ['--omit', ','.join('*addons/%s/*' %m for m in bad_modules) + '*__manifest__.py']
            py_version = get_py_version(build)
            cmd = [ py_version, '-m', 'coverage', 'run', '--branch', '--source', '/data/build'] + omit + cmd

            # prepare coverage result
            cov_path = build._path('coverage/test_mail_cov_xdo')
            os.makedirs(cov_path, exist_ok=True)
            cmdcov = ['&&', py_version, "-m", "coverage", "html", "-d", "/data/build/coverage", "--ignore-errors"]
            cmd += cmdcov
        max_timeout = int(self.env['ir.config_parameter'].get_param('runbot.runbot_timeout', default=10000))
        timeout = min(job_config.cpu_limit, max_timeout)
        return docker_run(build_odoo_cmd(cmd), log_path, build._path(), build._get_docker_name(), cpu_limit=timeout)

    def _run_odoo_run(self, build, log_path):
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        # run server
        cmd, mods = build._cmd()
        if os.path.exists(build._server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "8070"]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', '%s-all' % build.dest]

        if grep(build._server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter', '%d.*$']
            else:
                cmd += ['--db-filter', '%s.*$' % build.dest]
        smtp_host = docker_get_gateway_ip()
        if smtp_host:
            cmd += ['--smtp', smtp_host]
        return docker_run(build_odoo_cmd(cmd), log_path, build._path(), build._get_docker_name(), exposed_ports = [build.port, build.port + 1])

    def job_state(self):
        if self.job_type == 'run_odoo' or (self.job_type == 'python' and self.running_job):
            return 'running'
        return 'testing'

# -*- coding: utf-8 -*-

from odoo.addons.runbot.controllers.frontend import Runbot


class RunbotJobs(Runbot):

    def build_info(self, build):
        res = super(RunbotJobs, self).build_info(build)
        res['parse_job_ids'] = build.repo_id.parse_job_ids
        res['log_access_job_ids'] = build.repo_id.log_access_job_ids
        res['restored_db_name'] = build.restored_db_name
        return res



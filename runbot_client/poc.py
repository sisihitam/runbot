#!/usr/bin/python3
import argparse
import logging
import os
import socket
import sys
import time

_logger = logging.getLogger(__name__)


class RunbotClient():

    def __init__(self, env, args):
        self.env = env
        self.args = args
        self.fqdn = socket.getfqdn()

    def get_available_slots(self):
        Build = self.env['runbot.build']
        nb_testing = Build.search_count([('state', '=', 'testing'), ('host', '=', self.fqdn)])
        return self.args.nb_workers - nb_testing

    def main_loop(self):
        while True:
            print(self.get_available_slots())
            Repo = self.env['runbot.repo']
            active_repos = Repo.search([('mode', '!=', 'disabled')])
            Repo._scheduler(active_repos.ids)
            self.env.cr.commit()
            self.env.reset()
            Repo._reload_nginx()
            time.sleep(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--odoo-path', help='Odoo sources path')
    parser.add_argument('--db-host', default='127.0.0.1')
    parser.add_argument('--db-port', default='5432')
    parser.add_argument('--db-user', default=os.getlogin())
    parser.add_argument('--db-password')
    parser.add_argument('--runbot-db', default='runbot', help='name of runbot db')
    parser.add_argument('--nb-workers', type=int, default=4, help='Number of parallel builds')
    args = parser.parse_args()
    sys.path.append(args.odoo_path)
    import odoo
    odoo.tools.config['db_host'] = args.db_host
    odoo.tools.config['db_port'] = args.db_port
    odoo.tools.config['db_user'] = args.db_user
    odoo.tools.config['db_password'] = args.db_password
    addon_path = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
    odoo.tools.config['addons_path'] = ','.join([odoo.tools.config['addons_path'], addon_path])
    registry = odoo.registry(args.runbot_db)
    with odoo.api.Environment.manage():
        with registry.cursor() as cr:
            ctx = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})['res.users'].context_get()
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, ctx)
            runbot_client = RunbotClient(env, args)
            runbot_client.main_loop()

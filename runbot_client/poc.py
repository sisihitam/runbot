#!/usr/bin/python3
import argparse
import logging
import os
import socket
import sys
import time

_logger = logging.getLogger(__name__)


def main_loop(env):
    fqdn = socket.getfqdn()
    Build = env['runbot.build']
    while True:
        nb_testing = Build.search_count([('state', '=', 'testing'), ('host', '=', fqdn)])
        available_slots = workers - nb_testing
        print(available_slots)
        time.sleep(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--odoo-path', help='Odoo sources path')
    parser.add_argument('--db-host')
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
    workers = 4
    addon_path = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), '..'))
    odoo.tools.config['addons_path'] = ','.join([odoo.tools.config['addons_path'], addon_path])
    registry = odoo.registry(args.runbot_db)
    with odoo.api.Environment.manage():
        with registry.cursor() as cr:
            ctx = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})['res.users'].context_get()
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, ctx)
            main_loop(env)

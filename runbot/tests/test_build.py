# -*- coding: utf-8 -*-
from unittest.mock import patch
from odoo.tests import common

class Test_Build(common.TransactionCase):

    def setUp(self):
        super(Test_Build, self).setUp()
        self.Repo = self.env['runbot.repo']
        self.repo = self.Repo.create({'name': 'bla@example.com:foo/bar'})
        self.Branch = self.env['runbot.branch']
        self.branch = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/master'
        })
        self.branch_10 = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/10.0'
        })
        self.branch_11 = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/heads/11.0'
        })
        self.Build = self.env['runbot.build']

    @patch('odoo.addons.runbot.models.build.fqdn')
    def test_base_fields(self, mock_fqdn):
        build = self.Build.create({
            'branch_id': self.branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
            'port' : '1234',
        })
        self.assertEqual(build.id, build.sequence)
        self.assertEqual(build.dest, '%05d-master-d0d0ca' % build.id)
        # test dest change on new commit
        build.name = 'deadbeef0000ffffffffffffffffffffffffffff'
        self.assertEqual(build.dest, '%05d-master-deadbe' % build.id)

        # Test domain compute with fqdn and ir.config_parameter
        mock_fqdn.return_value = 'runbot98.nowhere.org'
        self.assertEqual(build.domain, 'runbot98.nowhere.org:1234')
        self.env['ir.config_parameter'].set_param('runbot.runbot_domain', 'runbot99.example.org')
        build._get_domain()
        self.assertEqual(build.domain, 'runbot99.example.org:1234')

    def test_pr_is_duplicate(self):
        """ test PR is a duplicate of a dev branch build """
        dup_repo = self.Repo.create({
            'name': 'bla@example.com:foo-dev/bar',
            'duplicate_id': self.repo.id
        })
        self.repo.duplicate_id = dup_repo.id
        dev_branch = self.Branch.create({
            'repo_id': dup_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        dev_build = self.Build.create({
            'branch_id': dev_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        pr = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/pull/12345'
        })

        pr_build = self.Build.create({
            'branch_id': pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(pr_build.state, 'duplicate')
        self.assertEqual(pr_build.duplicate_id.id, dev_build.id)

    def test_dev_is_duplicate(self):
        """ test dev branch build is a duplicate of a PR """
        dup_repo = self.Repo.create({
            'name': 'bla@example.com:foo-dev/bar',
            'duplicate_id': self.repo.id
        })
        self.repo.duplicate_id = dup_repo.id
        dev_branch = self.Branch.create({
            'repo_id': dup_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        pr = self.Branch.create({
            'repo_id': self.repo.id,
            'name': 'refs/pull/12345'
        })

        pr_build = self.Build.create({
            'branch_id': pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        dev_build = self.Build.create({
            'branch_id': dev_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual(dev_build.state, 'duplicate')
        self.assertEqual(dev_build.duplicate_id.id, pr_build.id)

    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    def test_closest_branch_01(self, mock_is_on_remote):
        """ test find a matching branch in a target repo based on branch name """
        mock_is_on_remote.return_value = True
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar'})
        self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        addons_branch = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/heads/10.0-fix-thing-moc'
        })
        addons_build = self.Build.create({
            'branch_id': addons_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((server_repo.id, addons_branch.name, 'exact'), addons_build._get_closest_branch_name(server_repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_closest_branch_02(self, mock_github):
        """ test find two matching PR having the same head name """
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:bar_branch'},
            'base' : {'ref': 'master'},
            'state': 'open'
        }
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar', 'token':  '1'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        server_pr = self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/pull/123456'
        })
        addons_pr = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/pull/789101'
        })
        addons_build = self.Build.create({
            'branch_id': addons_pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((server_repo.id, server_pr.name, 'exact'), addons_build._get_closest_branch_name(server_repo.id))

    @patch('odoo.addons.runbot.models.build.runbot_build._branch_exists')
    def test_closest_branch_03(self, mock_branch_exists):
        """ test find a branch based on dashed prefix"""
        mock_branch_exists.return_value = True
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        addons_branch = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/heads/10.0-fix-blah-blah-moc'
        })
        addons_build = self.Build.create({
            'branch_id': addons_branch.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((self.repo.id, 'refs/heads/10.0', 'prefix'), addons_build._get_closest_branch_name(self.repo.id))

    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_closest_branch_05(self, mock_github):
        """ test last resort value """
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:bar_branch'},
            'base' : {'ref': '10.0'},
            'state': 'open'
        }
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar', 'token':  '1'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        server_pr = self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/pull/123456'
        })
        mock_github.return_value = {
            'head' : {'label': 'foo-dev:foobar_branch'},
            'base' : {'ref': '10.0'},
            'state': 'open'
        }
        addons_pr = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/pull/789101'
        })
        addons_build = self.Build.create({
            'branch_id': addons_pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })
        self.assertEqual((server_repo.id, server_pr.target_branch_name, 'default'), addons_build._get_closest_branch_name(server_repo.id))

    def test_closest_branch_05_master(self):
        """ test last resort value when nothing common can be found"""
        server_repo = self.Repo.create({'name': 'bla@example.com:foo-dev/bar', 'token':  '1'})
        addons_repo = self.Repo.create({'name': 'bla@example.com:ent-dev/bar', 'token': '1'})
        server_pr = self.Branch.create({
            'repo_id': server_repo.id,
            'name': 'refs/heads/10.0'
        })
        addons_pr = self.Branch.create({
            'repo_id': addons_repo.id,
            'name': 'refs/head/badref-fix-foo'
        })
        addons_build = self.Build.create({
            'branch_id': addons_pr.id,
            'name': 'd0d0caca0000ffffffffffffffffffffffffffff',
        })

        self.assertEqual((server_repo.id, 'master', 'default'), addons_build._get_closest_branch_name(server_repo.id))


class Test_Build_find_duplicate(common.TransactionCase):

    @patch('odoo.addons.runbot.models.branch.runbot_branch._is_on_remote')
    @patch('odoo.addons.runbot.models.repo.runbot_repo._github')
    def test_find_duplicate(self, mock_github, mock_is_on_remote):
        foo_repo = self.env['runbot.repo'].create({'name': 'git@somewhere.com:foo/foo'})
        foo_dev_repo = self.env['runbot.repo'].create({'name': 'git@somewhere.com:foo-dev/foo'})
        foo_repo_ent = self.env['runbot.repo'].create({
            'name': 'git@somewhere.com:foo/enterprise',
            'dependency_ids': [(4, foo_repo.id), ],
        })
        foo_dev_repo_ent = self.env['runbot.repo'].create({
            'name': 'git@somewhere.com:foo-dev/enterprise',
            'dependency_ids': [(4, foo_dev_repo.id), ],
        })

        foo_repo.duplicate_id = foo_dev_repo.id
        foo_dev_repo.duplicate_id = foo_repo.id
        foo_repo_ent.duplicate_id = foo_dev_repo_ent.id
        foo_dev_repo_ent.duplicate_id = foo_repo_ent.id

        saas_ent_branch = self.env['runbot.branch'].create({
            'repo_id': foo_repo_ent.id,
            'name': 'refs/heads/saas-12.1'
        })

        saas_ent_build = self.env['runbot.build'].create({
            'branch_id': saas_ent_branch.id,
            'name': 'f312aaa55fb2ed52a31a38f8576d71f1a578f3c2',
            'result': 'ko',
            'state': 'running'
        })

        dev_branch = self.env['runbot.branch'].create({
            'repo_id': foo_dev_repo.id,
            'name': 'refs/heads/saas-12.1-fix-kanban-mobile-rfr',
        })

        # dev_build is finished
        dev_build = self.env['runbot.build'].create({
            'branch_id': dev_branch.id,
            'name': '4f5482010c7d6367b806db71d7402aa3a0a55d16',
            'state': 'done',
            'result': 'ok'
        })

        # New PR created and detected after the build is Finished
        pull_branch = self.env['runbot.branch'].create({
            'repo_id': foo_repo.id,
            'name': 'refs/pull/30177'
        })

        # now find the pull request
        mock_github.return_value = {
            'head': {'label': 'foo-dev:saas-12.1-fix-kanban-mobile-rfr'},
            'base': {'ref': 'saas-12.1'},
            'state': 'open'
        }
        pull_build = self.env['runbot.build'].create({
            'branch_id': pull_branch.id,
            'name': '4f5482010c7d6367b806db71d7402aa3a0a55d16',
        })
        self.assertEqual(pull_build.duplicate_id.id, dev_build.id)
        self.assertEqual(pull_build.state, 'duplicate')

        dev_ent_branch = self.env['runbot.branch'].create({
            'repo_id': foo_dev_repo_ent.id,
            'name': 'refs/heads/saas-12.1-fix-kanban-mobile-rfr',
        })

        # dev_ent build same as sass-12.1
        mock_is_on_remote.return_value = True
        dev_ent_build = self.env['runbot.build'].create({
            'branch_id': dev_ent_branch.id,
            'name': 'f312aaa55fb2ed52a31a38f8576d71f1a578f3c2'
        })

        # This test is false, it shoud be considered a duplicate
        # as the branch of this build is just a copy of foo/enterpsie:saas-12.1
        # the duplicate_id is found during creation but is discarded:
        # build_closest_name = build_id._get_closest_branch_name(extra_repo.id)[1]
        # returns 'refs/heads/saas-12.1-fix-kanban-mobile-rfr'
        # duplicate_closest_name = duplicate._get_closest_branch_name(extra_repo.id)[1]
        # returns 'master' (because saas-12.1 cannot be found in foo-dev repo)
        self.assertEqual(dev_ent_build.state, 'pending')
        self.assertEqual(dev_ent_build.duplicate_id.id, False)

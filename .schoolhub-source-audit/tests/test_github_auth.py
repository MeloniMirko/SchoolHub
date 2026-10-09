import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from schoolhub_gui import SchoolHub
from workspace import WorkspaceError

class GitHubAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = SchoolHub.__new__(SchoolHub)
        self.app.remote = 'https://github.com/SchoolOrganization/Lessons.git'
        self.app.github_user = ''
        self.app.branch = 'master'
        self.app.write_log = Mock()

    def test_organization_is_not_used_as_login(self):
        self.assertEqual(self.app._git_remote_for_auth(), self.app.remote)
        self.app.github_user = 'Student'
        self.assertEqual(self.app._git_remote_for_auth(),
                         'https://Student@github.com/SchoolOrganization/Lessons.git')

    def test_public_repository_is_allowed_without_warning(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'private': False}).encode())):
            self.app._assert_remote_private()
        self.assertNotIn('⚠', self.app.write_log.call_args.args[0])

    def test_login_replaces_stale_account_with_verified_identity(self):
        self.app.github_user = 'OldAccount'
        self.app.git = Mock(return_value=(0, '', ''))
        self.app._github_host_credential = Mock(return_value=('hint', 'test-session'))
        self.app._github_api_json = Mock(return_value={'login': 'CorrectStudent'})
        self.app._github_browser_login('temporary-repo')
        self.assertEqual(self.app.github_user, 'CorrectStudent')
        self.assertNotIn('test-session', str(self.app.write_log.call_args_list))

    def test_push_uses_refreshed_account_and_retries_once(self):
        calls = []
        def git(args, **kwargs):
            calls.append(args)
            if args[0] == 'push' and len(calls) == 1:
                return 128, '', 'Permission to repository denied to OldAccount'
            return 0, 'ok', ''
        self.app.git = git
        self.app._github_browser_login = lambda _: setattr(self.app, 'github_user', 'CorrectStudent')
        self.assertEqual(self.app._push_with_auth_retry('temporary-repo'), 'ok')
        self.assertEqual(calls[1], ['remote', 'set-url', 'origin',
                         'https://CorrectStudent@github.com/SchoolOrganization/Lessons.git'])
        self.assertEqual(sum(c[0] == 'push' for c in calls), 2)
        self.assertTrue(all('--force' not in c for c in calls))

    def test_permission_denied_after_login_stops_with_explanation(self):
        self.app.git = Mock(side_effect=[(128, '', 'write access denied'),
                                         (0, '', ''), (128, '', 'write access denied')])
        self.app._github_browser_login = Mock()
        with self.assertRaisesRegex(WorkspaceError, 'collaboratori') as caught:
            self.app._push_with_auth_retry('temporary-repo')
        self.assertEqual(SchoolHub._error_code('sync', caught.exception), 'SH-GIT-101')
        self.app._github_browser_login.assert_called_once()

    def test_local_permission_error_is_not_github_auth(self):
        self.assertNotEqual(SchoolHub._error_code('sync', PermissionError('Permission denied: local file')), 'SH-GIT-101')

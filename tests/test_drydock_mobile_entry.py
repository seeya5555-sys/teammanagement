import os
import tempfile
import unittest

import app as appmod


class DrydockMobileEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            admin = appmod.query("SELECT * FROM users WHERE role='admin' LIMIT 1", one=True)
            self.admin_token = appmod._issue_token(admin)
            member_id = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                ('member', appmod.generate_password_hash('member-pass'), 'Member', 'member'),
            )
            member = appmod.query('SELECT * FROM users WHERE id=?', (member_id,), one=True)
            self.member_token = appmod._issue_token(member)
            admin2_id = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                ('admin2', appmod.generate_password_hash('admin2-pass'), 'Admin Two', 'admin'),
            )
            admin2 = appmod.query('SELECT * FROM users WHERE id=?', (admin2_id,), one=True)
            self.admin2_token = appmod._issue_token(admin2)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    @staticmethod
    def bearer(token):
        return {'Authorization': f'Bearer {token}'}

    def test_admin_bearer_issues_session_cookie_and_redirects(self):
        client = appmod.app.test_client()
        response = client.get('/api/drydock/mobile-entry', headers=self.bearer(self.admin_token))
        self.assertEqual(302, response.status_code)
        self.assertEqual('/drydock/', response.headers['Location'])
        self.assertIn('session=', response.headers.get('Set-Cookie', ''))
        self.assertEqual('no-store', response.headers.get('Cache-Control'))

    def test_existing_cookie_cannot_bypass_fresh_bearer(self):
        client = appmod.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'stale-admin'
            session['role'] = 'admin'
        response = client.get('/api/drydock/mobile-entry')
        self.assertEqual(401, response.status_code)
        self.assertEqual('fresh_bearer_required', response.get_json()['error'])

    def test_member_bearer_is_forbidden_and_replaces_stale_admin_cookie(self):
        client = appmod.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'stale-admin'
            session['role'] = 'admin'
        response = client.get('/api/drydock/mobile-entry', headers=self.bearer(self.member_token))
        self.assertEqual(403, response.status_code)
        self.assertIn('session=', response.headers.get('Set-Cookie', ''))
        me = client.get('/api/me').get_json()
        self.assertEqual('member', me['username'])
        self.assertEqual('member', me['role'])

    def test_bridge_overwrites_stale_identity_with_fresh_admin_bearer(self):
        client = appmod.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'stale-admin'
            session['role'] = 'admin'
        response = client.get('/api/drydock/mobile-entry', headers=self.bearer(self.admin2_token))
        self.assertEqual(302, response.status_code)
        me = client.get('/api/me').get_json()
        self.assertEqual('admin2', me['username'])
        self.assertEqual('admin', me['role'])

    def test_stale_admin_cookie_with_invalid_bearer_returns_401(self):
        client = appmod.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'stale-admin'
            session['role'] = 'admin'
        response = client.get('/api/drydock/mobile-entry', headers=self.bearer('invalid-token'))
        self.assertEqual(401, response.status_code)
        self.assertEqual('fresh_bearer_required', response.get_json()['error'])

    def test_regular_bearer_api_remains_stateless(self):
        client = appmod.app.test_client()
        response = client.get('/api/me', headers=self.bearer(self.admin_token))
        self.assertEqual(200, response.status_code)
        self.assertNotIn('Set-Cookie', response.headers)


if __name__ == '__main__':
    unittest.main()

"""선박 3D: 소유자만 열람·다운로드, 슬러그 검증(경로 탐색 차단), glb는 instance 에서만 서빙."""
import os
import tempfile
import unittest

import app as appmod
import routes_ship3d
from app_core import execute


class Ship3DTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (appmod.DATABASE, appmod.app.config['DATABASE'], appmod.app.config.get('TESTING'), routes_ship3d.SHIP3D_DIR)
        db = os.path.join(self.tmp.name, 't.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        self.mdir = os.path.join(self.tmp.name, 'ship3d')
        os.makedirs(self.mdir)
        routes_ship3d.SHIP3D_DIR = self.mdir
        with open(os.path.join(self.mdir, 'sa_prosperity.glb'), 'wb') as f:
            f.write(b'glTF\x02\x00\x00\x00test')
        with open(os.path.join(self.mdir, 'sa_prosperity.json'), 'w', encoding='utf-8') as f:
            f.write('{"title": "South Africa Prosperity", "note": "n"}')
        with open(os.path.join(self.tmp.name, 'secret.glb'), 'wb') as f:
            f.write(b'nope')
        with appmod.app.app_context():
            appmod.init_db(False)
            self.owner = execute("INSERT INTO users(username, password_hash, role, active) VALUES('SS0094','x','admin',1)")
            self.other = execute("INSERT INTO users(username, password_hash, role, active) VALUES('other','y','admin',1)")
        self.c = appmod.app.test_client()

    def tearDown(self):
        appmod.DATABASE, appmod.app.config['DATABASE'], appmod.app.config['TESTING'], routes_ship3d.SHIP3D_DIR = self.old
        self.tmp.cleanup()

    def _login(self, uid, username):
        with self.c.session_transaction() as s:
            s['user_id'] = uid
            s['username'] = username
            s['role'] = 'admin'

    def test_anonymous_redirected(self):
        self.assertIn(self.c.get('/ship3d').status_code, (302, 401))
        self.assertIn(self.c.get('/ship3d/model/sa_prosperity.glb').status_code, (302, 401))

    def test_other_account_404(self):
        self._login(self.other, 'other')
        self.assertEqual(404, self.c.get('/ship3d').status_code)
        self.assertEqual(404, self.c.get('/ship3d/model/sa_prosperity.glb').status_code)

    def test_owner_page_and_model(self):
        self._login(self.owner, 'SS0094')
        r = self.c.get('/ship3d')
        self.assertEqual(200, r.status_code)
        body = r.get_data(as_text=True)
        self.assertIn('South Africa Prosperity', body)
        self.assertIn('/ship3d/model/sa_prosperity.glb', body)
        m = self.c.get('/ship3d/model/sa_prosperity.glb')
        self.assertEqual(200, m.status_code)
        self.assertEqual('model/gltf-binary', m.mimetype)
        self.assertIn('no-store', m.headers.get('Cache-Control', ''))
        m.close()

    def test_bad_slug_and_traversal_404(self):
        self._login(self.owner, 'SS0094')
        for path in ('/ship3d/model/missing.glb', '/ship3d/model/..%2Fsecret.glb', '/ship3d/model/Bad.glb', '/ship3d/model/..secret.glb'):
            self.assertEqual(404, self.c.get(path).status_code, path)

    def test_bad_meta_json_does_not_break_page(self):
        with open(os.path.join(self.mdir, 'sa_prosperity.json'), 'w', encoding='utf-8') as f:
            f.write('null')
        self._login(self.owner, 'SS0094')
        r = self.c.get('/ship3d')
        self.assertEqual(200, r.status_code)
        self.assertIn('no-store', r.headers.get('Cache-Control', ''))
        h = self.c.head('/ship3d/model/sa_prosperity.glb')
        self.assertEqual(200, h.status_code)
        self.assertIn('private', h.headers.get('Cache-Control', ''))
        h.close()

    def test_empty_dir_page_ok(self):
        routes_ship3d.SHIP3D_DIR = os.path.join(self.tmp.name, 'none')
        self._login(self.owner, 'SS0094')
        r = self.c.get('/ship3d')
        self.assertEqual(200, r.status_code)
        self.assertIn('등록된 3D 모델이 없음', r.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()

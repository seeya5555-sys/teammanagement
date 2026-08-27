#!/usr/bin/env python3
"""우리자산 API: 2인 가입, 가구 격리, 소유자 검증, CRUD 계약."""
import os, sys, sqlite3, tempfile
from werkzeug.security import generate_password_hash

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
DB = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = DB

import app as A
A.DATABASE = DB; A.app.config['DATABASE'] = DB; A.app.config['TESTING'] = True
A.init_db(drop=False); A._auto_migrate()
A.app.app_context().push()


def add_user(username):
    return A.execute("INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                     (username, generate_password_hash('x'), username, 'member'))


def add_family_user(username):
    return A.execute("INSERT INTO users(username,password_hash,display_name,role,app_scope,active) "
                     "VALUES(?,?,?,?,?,1)",
                     (username, generate_password_hash('x'), username, 'member', 'family'))


u1, u2, outsider, outsider2 = (add_user('husband'), add_user('wife'),
                               add_user('outside'), add_user('outside2'))


def client(uid, name, role='member', app_scope='business'):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid; s['username'] = name; s['display_name'] = name
        s['role'] = role; s['app_scope'] = app_scope
    return c


c1, c2 = client(u1, 'husband'), client(u2, 'wife')
c3, c4 = client(outsider, 'outside'), client(outsider2, 'outside2')

# 자산 전용 계정은 같은 Bearer 인증을 쓰더라도 업무 API 표면이 전부 닫힌다.
family_uid = add_family_user('family-only')
family = client(family_uid, 'family-only')
with family.session_transaction() as s:
    s['app_scope'] = 'family'
assert family.get('/api/me').status_code == 200
assert family.get('/api/family-assets').status_code == 200
assert family.get('/api/users').status_code == 403
assert family.get('/api/vessels').status_code == 403
token_login = A.app.test_client().post('/api/auth/token',
                                       json={'username': 'family-only', 'password': 'x'})
assert token_login.status_code == 200 and token_login.json['app_scope'] == 'family'
family_token = token_login.json['token']
bearer = {'Authorization': f'Bearer {family_token}'}
stateless = A.app.test_client()
assert stateless.get('/api/family-assets', headers=bearer).status_code == 200
assert stateless.get('/api/users', headers=bearer).status_code == 403
assert stateless.get('/api/family-assets-shadow', headers=bearer).status_code == 403
assert stateless.get('/dashboard', headers=bearer).status_code in (302, 401)
A.execute('UPDATE users SET active=0 WHERE id=?', (family_uid,))
assert stateless.get('/api/family-assets', headers=bearer).status_code == 401
A.execute('UPDATE users SET active=1 WHERE id=?', (family_uid,))

# 관리자 생성 경로도 family scope를 role=member/supervisor=NULL로 강제한다.
admin_id = A.query("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1", one=True)['id']
admin = client(admin_id, 'admin', role='admin')
A.g.pop('_sess_account', None)  # 이 standalone test의 수명 긴 app_context 캐시를 요청 전에 비운다.
created = admin.post('/api/users', json={'username': 'asset-login', 'password': 'secret1',
                                         'display_name': '자산전용', 'role': 'admin',
                                         'app_scope': 'family', 'supervisor_id': 999})
assert created.status_code == 201, created.get_data(as_text=True)
asset_login = A.query('SELECT role,app_scope,supervisor_id FROM users WHERE id=?',
                      (created.json['id'],), one=True)
assert tuple(asset_login) == ('member', 'family', None)
assert admin.put(f"/api/users/{created.json['id']}",
                 json={'app_scope': 'business'}).status_code == 400
assert admin.put(f"/api/users/{created.json['id']}",
                 json={'role': 'admin', 'supervisor_id': 1}).status_code == 200
asset_login = A.query('SELECT role,app_scope,supervisor_id FROM users WHERE id=?',
                      (created.json['id'],), one=True)
assert tuple(asset_login) == ('member', 'family', None)

r = c1.post('/api/family-assets/households', json={'name': '우리집', 'display_name': '유석'})
assert r.status_code == 201, r.get_data(as_text=True)
code = r.json['household']['invite_code']
assert len(code) == 8 and r.json['members'][0]['id'] == u1

r = c2.post('/api/family-assets/join', json={'invite_code': code.lower(), 'display_name': '혜진'})
assert r.status_code == 201 and len(r.json['members']) == 2, r.get_data(as_text=True)
assert c3.post('/api/family-assets/join', json={'invite_code': code}).status_code == 409

payload = {'kind': 'saving', 'name': '목돈 통장', 'amount': 12000000,
           'owner_mode': 'member', 'owner_user_id': u2, 'institution': '은행', 'note': ''}
r = c1.post('/api/family-assets/assets', json=payload)
assert r.status_code == 201, r.get_data(as_text=True)
asset_id = r.json['id']
snap = c2.get('/api/family-assets').json
assert snap['assets'][0]['amount'] == 12000000 and snap['assets'][0]['owner_user_id'] == u2
assert c3.get('/api/family-assets').json['setup_required'] is True
assert c3.patch(f'/api/family-assets/assets/{asset_id}', json=payload).status_code == 409

# 다른 가구 구성원은 존재하는 id를 알아도 404 — 무가구 409와 다른 실제 IDOR 경로.
assert c3.post('/api/family-assets/households', json={'name': '다른집', 'display_name': '밖1'}).status_code == 201
other_code = c3.get('/api/family-assets').json['household']['invite_code']
assert c4.post('/api/family-assets/join', json={'invite_code': other_code, 'display_name': '밖2'}).status_code == 201
assert c3.patch(f'/api/family-assets/assets/{asset_id}', json=payload).status_code == 404
assert c4.delete(f'/api/family-assets/assets/{asset_id}').status_code == 404

joint = {**payload, 'kind': 'property', 'name': '우리집', 'amount': 700000000,
         'owner_mode': 'joint', 'owner_user_id': None, 'joint_share': 60}
assert c2.patch(f'/api/family-assets/assets/{asset_id}', json=joint).status_code == 200
edited = c1.get('/api/family-assets').json['assets'][0]
assert edited['owner_mode'] == 'joint' and edited['joint_share'] == 60
assert c1.delete(f'/api/family-assets/assets/{asset_id}').status_code == 200
assert c2.delete(f'/api/family-assets/assets/{asset_id}').status_code == 404

bad = {**payload, 'owner_user_id': outsider}
assert c1.post('/api/family-assets/assets', json=bad).status_code == 400
assert c1.post('/api/family-assets/assets', json={**payload, 'amount': 12.9}).status_code == 400
assert c1.post('/api/family-assets/assets', json={**joint, 'joint_share': 101}).status_code == 400

# 운영처럼 기존 DB에 신규 표가 없는 상태에서도 _auto_migrate가 schema.sql을 재적용한다.
db = A.get_db()
db.execute('DROP TABLE family_asset_entry'); db.execute('DROP TABLE family_asset_member')
db.execute('DROP TABLE family_asset_household'); db.commit()
A._auto_migrate()
for table in ('family_asset_household', 'family_asset_member', 'family_asset_entry'):
    assert A.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,), one=True), table

# 데이터가 이미 있는 legacy users 표도 row 손실 없이 business 기본값으로 이동한다.
legacy = tempfile.mktemp(suffix='.db')
conn = sqlite3.connect(legacy)
conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, "
             "display_name TEXT, supervisor_id INTEGER, role TEXT, active INTEGER, "
             "last_login_at TEXT, created_at TEXT)")
conn.execute("INSERT INTO users VALUES(1,'legacy','hash','Legacy',NULL,'member',1,NULL,'now')")
conn.commit(); conn.close()
current_db = A.DATABASE
A.DATABASE = legacy
A._auto_migrate()
conn = sqlite3.connect(legacy)
assert conn.execute("SELECT username,app_scope FROM users WHERE id=1").fetchone() == ('legacy', 'business')
conn.close()
A.DATABASE = current_db
print('family assets API: ok')

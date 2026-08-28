#!/usr/bin/env python3
"""우리자산 API: 2인 가입, 가구 격리, 소유자 검증, CRUD 계약."""
import os, sys, sqlite3, tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from werkzeug.security import check_password_hash, generate_password_hash

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

# owner가 배우자 family 계정 생성+우리집 가입을 한 번에 끝낸다.
provision_owner_id = add_user('provision-owner')
provision_owner = client(provision_owner_id, 'provision-owner')
assert provision_owner.post('/api/family-assets/households',
                            json={'name': '온보딩집', 'display_name': '소유자'}).status_code == 201
assert provision_owner.post('/api/family-assets/partner-account',
                            json={'username': 'bad space', 'password': 'secret123',
                                  'display_name': '배우자'}).status_code == 400
assert provision_owner.post('/api/family-assets/partner-account',
                            json={'username': 'partner.login', 'password': 'short',
                                  'display_name': '배우자'}).status_code == 400
created_partner = provision_owner.post('/api/family-assets/partner-account',
    json={'username': 'Partner.Login', 'password': 'secret123', 'display_name': '배우자'})
assert created_partner.status_code == 201 and len(created_partner.json['members']) == 2
assert 'secret123' not in created_partner.get_data(as_text=True)
partner_row = A.query('SELECT id,username,password_hash,role,app_scope,supervisor_id FROM users '
                      'WHERE username=?', ('partner.login',), one=True)
assert partner_row['role'] == 'member' and partner_row['app_scope'] == 'family'
assert partner_row['supervisor_id'] is None and check_password_hash(partner_row['password_hash'], 'secret123')
try:
    A.get_db().execute("INSERT INTO users(username,password_hash,display_name,role,app_scope,active) "
                       "VALUES('PARTNER.LOGIN','x','dup','member','family',1)")
    raise AssertionError('NOCASE unique index did not reject duplicate username')
except sqlite3.IntegrityError:
    A.get_db().rollback()
assert A.query('SELECT role FROM family_asset_member WHERE user_id=?',
               (partner_row['id'],), one=True)['role'] == 'member'
partner_login = A.app.test_client().post('/api/auth/token',
    json={'username': 'partner.login', 'password': 'secret123'})
assert partner_login.status_code == 200 and partner_login.json['app_scope'] == 'family'
partner_headers = {'Authorization': f"Bearer {partner_login.json['token']}"}
assert A.app.test_client().post('/api/family-assets/partner-account',
    json={'username': 'anonymous-user', 'password': 'secret123',
          'display_name': '익명'}).status_code == 401
assert c1.post('/api/family-assets/partner-account',
    json={'username': 'no-house-user', 'password': 'secret123',
          'display_name': '무가구'}).status_code == 409
assert A.app.test_client().post('/api/family-assets/partner-account', headers=partner_headers,
    json={'username': 'forbidden-user', 'password': 'secret123',
          'display_name': '금지'}).status_code == 403
reset = provision_owner.post('/api/family-assets/partner-account/password',
                             json={'password': 'new-secret-456'})
assert reset.status_code == 200 and 'new-secret-456' not in reset.get_data(as_text=True)
assert A.app.test_client().get('/api/family-assets', headers=partner_headers).status_code == 401
assert A.app.test_client().post('/api/auth/token',
    json={'username': 'partner.login', 'password': 'secret123'}).status_code == 401
partner_relogin = A.app.test_client().post('/api/auth/token',
    json={'username': 'partner.login', 'password': 'new-secret-456'})
assert partner_relogin.status_code == 200
assert A.app.test_client().post('/api/family-assets/partner-account/password',
    headers={'Authorization': f"Bearer {partner_relogin.json['token']}"},
    json={'password': 'member-cannot-reset'}).status_code == 403
assert provision_owner.post('/api/family-assets/partner-account',
    json={'username': 'never-created', 'password': 'secret123',
          'display_name': '초과'}).status_code == 409
assert A.query('SELECT id FROM users WHERE username=?', ('never-created',), one=True) is None

# username 대소문자 중복과 동시 두 번 생성도 원자적으로 한 계정만 남긴다.
race_owner_id = add_user('race-owner')
race_owner1, race_owner2 = client(race_owner_id, 'race-owner'), client(race_owner_id, 'race-owner')
assert race_owner1.post('/api/family-assets/households',
                       json={'name': '경합집', 'display_name': '경합소유자'}).status_code == 201
assert race_owner1.post('/api/family-assets/partner-account',
    json={'username': 'PARTNER.LOGIN', 'password': 'secret123',
          'display_name': '중복'}).status_code == 409
partner_barrier = Barrier(2)
def race_partner(client_, username_):
    partner_barrier.wait()
    return client_.post('/api/family-assets/partner-account',
        json={'username': username_, 'password': 'secret123', 'display_name': username_}).status_code
with ThreadPoolExecutor(max_workers=2) as pool:
    partner_f1 = pool.submit(race_partner, race_owner1, 'race-partner-a')
    partner_f2 = pool.submit(race_partner, race_owner2, 'race-partner-b')
    partner_statuses = sorted((partner_f1.result(), partner_f2.result()))
assert partner_statuses == [201, 409], partner_statuses
race_hid = A.query('SELECT household_id FROM family_asset_member WHERE user_id=?',
                   (race_owner_id,), one=True)['household_id']
assert A.query('SELECT COUNT(*) n FROM family_asset_member WHERE household_id=?',
               (race_hid,), one=True)['n'] == 2
assert A.query("SELECT COUNT(*) n FROM users WHERE username IN ('race-partner-a','race-partner-b')",
               one=True)['n'] == 1

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
assert c1.post('/api/family-assets/partner-account/password',
               json={'password': 'cannot-reset-business'}).status_code == 409
assert c3.post('/api/family-assets/join', json={'invite_code': code}).status_code == 409

payload = {'kind': 'saving', 'name': '목돈 통장', 'amount': 12000000,
           'owner_mode': 'member', 'owner_user_id': u2, 'institution': '은행', 'note': ''}
r = c1.post('/api/family-assets/assets', json=payload)
assert r.status_code == 201, r.get_data(as_text=True)
asset_id = r.json['id']
snap = c2.get('/api/family-assets').json
assert snap['assets'][0]['amount'] == 12000000 and snap['assets'][0]['owner_user_id'] == u2
revision1 = snap['assets'][0]['revision']
assert revision1 > 0
assert snap['history'][0]['action'] == 'create' and snap['history'][0]['amount_after'] == 12000000
assert snap['trends'][-1]['total_assets'] == 12000000 and snap['trends'][-1]['net_worth'] == 12000000
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
assert c1.patch(f'/api/family-assets/assets/{asset_id}', json=joint).status_code == 400
assert c2.patch(f'/api/family-assets/assets/{asset_id}',
                json={**joint, 'expected_revision': revision1}).status_code == 200
edited = c1.get('/api/family-assets').json['assets'][0]
assert edited['owner_mode'] == 'joint' and edited['joint_share'] == 60
revision2 = edited['revision']
assert revision2 > revision1
audit = c1.get('/api/family-assets').json
assert [h['action'] for h in audit['history'][:2]] == ['update', 'create']
assert audit['history'][0]['amount_before'] == 12000000
assert audit['history'][0]['amount_after'] == 700000000

# 두 기기가 같은 revision에서 시작하면 첫 저장만 성공하고 stale 저장/삭제는 원장·이력을 못 건드린다.
history_count = len(audit['history'])
stale = c1.patch(f'/api/family-assets/assets/{asset_id}',
                 json={**payload, 'expected_revision': revision1})
assert stale.status_code == 409 and stale.json['error'] == 'edit_conflict'
assert c1.get('/api/family-assets').json['assets'][0]['amount'] == 700000000
assert len(c1.get('/api/family-assets').json['history']) == history_count
assert c1.delete(f'/api/family-assets/assets/{asset_id}',
                 json={'expected_revision': revision1}).status_code == 409
assert c1.delete(f'/api/family-assets/assets/{asset_id}').status_code == 400
assert c1.delete(f'/api/family-assets/assets/{asset_id}',
                 json={'expected_revision': revision2}).status_code == 200
assert c2.delete(f'/api/family-assets/assets/{asset_id}').status_code == 404
after_delete = c1.get('/api/family-assets').json
assert after_delete['history'][0]['action'] == 'delete'
assert after_delete['history'][0]['amount_before'] == 700000000
assert after_delete['trends'][-1]['net_worth'] == 0
assert c3.get('/api/family-assets').json['history'] == []  # 가구간 이력 격리

# 진짜 병렬 요청도 같은 revision에서 정확히 하나만 성공한다.
race_create = c1.post('/api/family-assets/assets', json=payload)
race_id = race_create.json['id']
assert race_id > asset_id  # AUTOINCREMENT라 삭제된 asset id를 재사용하지 않음.
race_revision = c1.get('/api/family-assets').json['assets'][0]['revision']
for malformed in ('1', -1, 1.5, True):
    assert c1.patch(f'/api/family-assets/assets/{race_id}',
                    json={**payload, 'expected_revision': malformed}).status_code == 400
barrier = Barrier(2)
def race_update(client_, amount_):
    barrier.wait()
    return client_.patch(f'/api/family-assets/assets/{race_id}',
                         json={**payload, 'amount': amount_,
                               'expected_revision': race_revision}).status_code
with ThreadPoolExecutor(max_workers=2) as pool:
    future1 = pool.submit(race_update, c1, 13000000)
    future2 = pool.submit(race_update, c2, 14000000)
    statuses = sorted((future1.result(), future2.result()))
assert statuses == [200, 409], statuses
race_snap = c1.get('/api/family-assets').json
race_asset = next(a for a in race_snap['assets'] if a['id'] == race_id)
race_history = [h for h in race_snap['history'] if h['asset_id'] == race_id]
assert race_asset['revision'] == race_revision + 1
assert race_asset['amount'] in (13000000, 14000000)
assert [h['action'] for h in race_history] == ['update', 'create']
assert c1.delete(f'/api/family-assets/assets/{race_id}',
                 json={'expected_revision': race_asset['revision']}).status_code == 200

bad = {**payload, 'owner_user_id': outsider}
assert c1.post('/api/family-assets/assets', json=bad).status_code == 400
assert c1.post('/api/family-assets/assets', json={**payload, 'amount': 12.9}).status_code == 400
assert c1.post('/api/family-assets/assets', json={**joint, 'joint_share': 101}).status_code == 400

# GET은 DB를 쓰지 않고, 13개월 중 최신 12개월만 월 중복 없이 오름차순 반환한다.
hid = c1.get('/api/family-assets').json['household']['id']
db = A.get_db()
for months_ago in range(1, 14):
    db.execute(
        "INSERT OR REPLACE INTO family_asset_monthly_snapshot"
        "(household_id,month,total_assets,total_debt,net_worth) "
        "VALUES(?,strftime('%Y-%m','now','+9 hours',?),?,?,?)",
        (hid, f'-{months_ago} months', months_ago * 100, 0, months_ago * 100))
db.commit()
snapshot_count = db.execute(
    'SELECT COUNT(*) FROM family_asset_monthly_snapshot WHERE household_id=?', (hid,)).fetchone()[0]
bounded = c1.get('/api/family-assets').json['trends']
assert len(bounded) == 12 and [x['month'] for x in bounded] == sorted({x['month'] for x in bounded})
assert len({x['month'] for x in bounded}) == 12
assert db.execute('SELECT COUNT(*) FROM family_asset_monthly_snapshot WHERE household_id=?',
                  (hid,)).fetchone()[0] == snapshot_count

# 이력은 최신 id 순서로 정확히 50건만 노출한다.
for idx in range(51):
    db.execute("INSERT INTO family_asset_history"
               "(household_id,asset_id,action,asset_name,kind,amount_after,changed_by) "
               "VALUES(?,NULL,'create',?,'saving',?,?)", (hid, f'경계-{idx}', idx, u1))
db.commit()
bounded_history = c1.get('/api/family-assets').json['history']
assert len(bounded_history) == 50
assert [x['id'] for x in bounded_history] == sorted([x['id'] for x in bounded_history], reverse=True)

# 기존 family_asset_entry 표에도 additive migration으로 revision=1을 보강한다.
migration_asset = c1.post('/api/family-assets/assets', json=payload).json['id']
db.execute('ALTER TABLE family_asset_entry DROP COLUMN revision'); db.commit()
A._auto_migrate()
assert 'revision' in {r[1] for r in db.execute('PRAGMA table_info(family_asset_entry)')}
assert db.execute('SELECT revision FROM family_asset_entry WHERE id=?',
                  (migration_asset,)).fetchone()[0] == 1

# 운영처럼 기존 DB에 신규 표가 없는 상태에서도 _auto_migrate가 schema.sql을 재적용한다.
db.execute('DROP TABLE family_asset_history'); db.execute('DROP TABLE family_asset_monthly_snapshot')
db.execute('DROP TABLE family_asset_entry'); db.execute('DROP TABLE family_asset_member')
db.execute('DROP TABLE family_asset_household'); db.commit()
A._auto_migrate()
for table in ('family_asset_household', 'family_asset_member', 'family_asset_entry',
              'family_asset_history', 'family_asset_monthly_snapshot'):
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

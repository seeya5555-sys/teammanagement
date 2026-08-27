#!/usr/bin/env python3
"""우리자산 API: 2인 가입, 가구 격리, 소유자 검증, CRUD 계약."""
import os, sys, tempfile
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


u1, u2, outsider, outsider2 = (add_user('husband'), add_user('wife'),
                               add_user('outside'), add_user('outside2'))


def client(uid, name):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = uid; s['username'] = name; s['display_name'] = name; s['role'] = 'member'
    return c


c1, c2 = client(u1, 'husband'), client(u2, 'wife')
c3, c4 = client(outsider, 'outside'), client(outsider2, 'outside2')

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
print('family assets API: ok')

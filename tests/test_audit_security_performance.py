#!/usr/bin/env python3
"""Regression checks for the 2026-09 Astra cross-surface audit."""
import os
import sys
import tempfile

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
db = tempfile.mktemp(suffix='.db')
os.environ['TRMT_DB'] = db

import app as A

A.DATABASE = db
A.app.config['DATABASE'] = db
A.app.config['TESTING'] = True
A.init_db(drop=False)
A._auto_migrate()

with A.app.app_context():
    sup = A.execute("INSERT INTO supervisors(name) VALUES('AUDIT SUP')")
    vessel = A.execute("INSERT INTO vessels(name, active) VALUES('AUDIT VSL', 1)")
    uid = A.execute(
        "INSERT INTO users(username,password_hash,display_name,role,supervisor_id,active) "
        "VALUES('audit','x','Audit','admin',?,1)", (sup,))
    for status in ('Open', 'InProgress', 'Closed'):
        A.execute(
            "INSERT INTO issues(vessel_id,supervisor_id,item_topic,status,issue_date) "
            "VALUES(?,?,?,?,?)", (vessel, sup, status, status, '2026-09-06'))

c = A.app.test_client()
with c.session_transaction() as s:
    s.update(user_id=uid, username='audit', role='admin', supervisor_id=sup)

# A live downgrade must affect login_required views immediately, without a new login.
with A.app.app_context():
    A.execute("UPDATE users SET role='member' WHERE id=?", (uid,))
r = c.get('/api/dashboard/cockpit')
assert r.status_code == 200
with c.session_transaction() as s:
    assert s['role'] == 'member'
assert r.get_json()['approvals'] == {'fundreq': 0, 'aor': 0, 'invoice': 0, 'oldest': None}

# The opt-in filter preserves every non-Closed status and the unfiltered contract.
active = c.get('/api/issues?exclude_status=Closed').get_json()
all_rows = c.get('/api/issues').get_json()
assert [x['status'] for x in active] == ['Open', 'InProgress']
assert [x['status'] for x in all_rows] == ['Open', 'InProgress', 'Closed']

print('✅ audit security/performance regressions pass')

"""우리자산 — TRMT iOS 전용 2인 가구 자산 동기화 API.

웹 UI는 제공하지 않는다. 기존 TRMT 인증 계정 두 개가 초대코드로 같은 가구에
가입하고, 그 가구의 구성원만 자산 원장을 읽고 쓸 수 있다.
"""
import secrets
import sqlite3
import re

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash

from app_core import get_db, query
from helpers_shared import login_required

bp = Blueprint("routes_family_assets", __name__)

KINDS = {'income', 'cash', 'saving', 'stock', 'property', 'loan', 'other'}
OWNER_MODES = {'member', 'joint'}
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{3,39}$')


def _clean_text(value, field, *, required=False, limit=120):
    value = '' if value is None else str(value).strip()
    if required and not value:
        raise ValueError(f'{field} 필수')
    if len(value) > limit:
        raise ValueError(f'{field} 최대 {limit}자')
    return value


def _member():
    return query(
        "SELECT m.household_id,m.user_id,m.display_name,m.role,h.name household_name,h.invite_code "
        "FROM family_asset_member m JOIN family_asset_household h ON h.id=m.household_id "
        "WHERE m.user_id=?", (session['user_id'],), one=True)


def _invite_code(db):
    for _ in range(20):
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if not db.execute('SELECT 1 FROM family_asset_household WHERE invite_code=?', (code,)).fetchone():
            return code
    raise RuntimeError('invite code allocation failed')


def _snapshot(member):
    if not member:
        return {'setup_required': True, 'household': None, 'members': [], 'assets': [],
                'history': [], 'trends': []}
    hid = member['household_id']
    members = query(
        "SELECT m.user_id id,m.display_name,m.role,m.joined_at,"
        "CASE WHEN u.app_scope='family' THEN u.username END login_username "
        "FROM family_asset_member m JOIN users u ON u.id=m.user_id "
        "WHERE m.household_id=? ORDER BY m.joined_at,m.user_id", (hid,))
    assets = query(
        "SELECT a.id,a.kind,a.name,a.amount,a.owner_mode,a.owner_user_id,a.joint_share,"
        "a.institution,a.note,a.revision,a.updated_at,a.updated_by,u.display_name updated_by_name "
        "FROM family_asset_entry a LEFT JOIN users u ON u.id=a.updated_by "
        "WHERE a.household_id=? ORDER BY a.updated_at DESC,a.id DESC", (hid,))
    history = query(
        "SELECT h.id,h.asset_id,h.action,h.asset_name,h.kind,h.amount_before,h.amount_after,"
        "h.changed_by,h.created_at,COALESCE(u.display_name,u.username,'구성원') changed_by_name "
        "FROM family_asset_history h LEFT JOIN users u ON u.id=h.changed_by "
        "WHERE h.household_id=? ORDER BY h.id DESC LIMIT 50", (hid,))
    stored_trends = query(
        "SELECT month,total_assets,total_debt,net_worth,captured_at "
        "FROM family_asset_monthly_snapshot WHERE household_id=? "
        "ORDER BY month DESC LIMIT 12", (hid,))
    current = _totals_from_rows(assets)
    # 서비스 기준 시각은 KST로 고정한다. 서버 OS timezone과 무관하게 월 경계가 같다.
    month = query("SELECT strftime('%Y-%m','now','+9 hours') month", one=True)['month']
    trends_by_month = {r['month']: dict(r) for r in stored_trends}
    trends_by_month[month] = {'month': month, **current, 'captured_at': None}
    return {
        'setup_required': False,
        'household': {'id': hid, 'name': member['household_name'],
                      'invite_code': member['invite_code'], 'me_user_id': session['user_id']},
        'members': [dict(x) for x in members],
        'assets': [dict(x) for x in assets],
        'history': [dict(x) for x in history],
        'trends': [trends_by_month[k] for k in sorted(trends_by_month)[-12:]],
    }


def _totals_from_rows(rows):
    assets = debt = 0
    for row in rows:
        if row['kind'] == 'income':
            continue
        if row['kind'] == 'loan':
            debt += int(row['amount'])
        else:
            assets += int(row['amount'])
    return {'total_assets': assets, 'total_debt': debt, 'net_worth': assets - debt}


def _record_change(db, member, action, asset_id, before, after):
    row = after or before
    db.execute(
        "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
        "amount_before,amount_after,changed_by) VALUES(?,?,?,?,?,?,?,?)",
        (member['household_id'], asset_id, action, row['name'], row['kind'],
         before['amount'] if before else None, after['amount'] if after else None,
         session['user_id']))


def _upsert_monthly_snapshot(db, household_id):
    rows = db.execute("SELECT kind,amount FROM family_asset_entry WHERE household_id=?",
                      (household_id,)).fetchall()
    totals = _totals_from_rows(rows)
    db.execute(
        "INSERT INTO family_asset_monthly_snapshot(household_id,month,total_assets,total_debt,net_worth) "
        "VALUES(?,strftime('%Y-%m','now','+9 hours'),?,?,?) "
        "ON CONFLICT(household_id,month) DO UPDATE SET total_assets=excluded.total_assets,"
        "total_debt=excluded.total_debt,net_worth=excluded.net_worth,"
        "captured_at=datetime('now','+9 hours')",
        (household_id, totals['total_assets'], totals['total_debt'], totals['net_worth']))


@bp.get('/api/family-assets')
@login_required
def family_assets_get():
    return jsonify(_snapshot(_member()))


@bp.post('/api/family-assets/households')
@login_required
def family_assets_create_household():
    if _member():
        return jsonify({'error': 'already_joined'}), 409
    d = request.get_json(silent=True) or {}
    try:
        name = _clean_text(d.get('name') or '우리집', '가구 이름', required=True, limit=40)
        display = _clean_text(d.get('display_name') or session.get('display_name') or
                              session.get('username'), '표시 이름', required=True, limit=30)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('SELECT 1 FROM family_asset_member WHERE user_id=?',
                      (session['user_id'],)).fetchone():
            db.rollback(); return jsonify({'error': 'already_joined'}), 409
        code = _invite_code(db)
        hid = db.execute(
            "INSERT INTO family_asset_household(name,invite_code,created_by) VALUES(?,?,?)",
            (name, code, session['user_id'])).lastrowid
        db.execute(
            "INSERT INTO family_asset_member(household_id,user_id,display_name,role) "
            "VALUES(?,?,?,'owner')", (hid, session['user_id'], display))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member())), 201


@bp.post('/api/family-assets/join')
@login_required
def family_assets_join():
    if _member():
        return jsonify({'error': 'already_joined'}), 409
    d = request.get_json(silent=True) or {}
    try:
        code = _clean_text(d.get('invite_code'), '초대 코드', required=True, limit=16).upper().replace('-', '')
        display = _clean_text(d.get('display_name') or session.get('display_name') or
                              session.get('username'), '표시 이름', required=True, limit=30)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        household = db.execute(
            'SELECT id FROM family_asset_household WHERE invite_code=?', (code,)).fetchone()
        if not household:
            db.rollback(); return jsonify({'error': 'invalid_invite_code'}), 404
        count = db.execute(
            'SELECT COUNT(*) FROM family_asset_member WHERE household_id=?', (household['id'],)).fetchone()[0]
        if count >= 2:
            db.rollback(); return jsonify({'error': 'household_full'}), 409
        db.execute(
            "INSERT INTO family_asset_member(household_id,user_id,display_name,role) "
            "VALUES(?,?,?,'member')", (household['id'], session['user_id'], display))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member())), 201


@bp.post('/api/family-assets/partner-account')
@login_required
def family_assets_create_partner_account():
    """가구 owner가 배우자용 family 계정과 membership을 한 transaction으로 만든다."""
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    if member['role'] != 'owner':
        return jsonify({'error': 'owner_required'}), 403
    d = request.get_json(silent=True) or {}
    try:
        username = _clean_text(d.get('username'), '로그인 ID', required=True, limit=40).lower()
        display = _clean_text(d.get('display_name'), '표시 이름', required=True, limit=30)
        password = d.get('password')
        if not USERNAME_RE.fullmatch(username):
            raise ValueError('로그인 ID는 영문 소문자·숫자·._- 조합 4~40자')
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError('비밀번호는 8~128자')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # 느린 password KDF는 SQLite write lock을 잡기 전에 끝낸다.
    password_hash = generate_password_hash(password)
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        owner = db.execute(
            "SELECT household_id FROM family_asset_member "
            "WHERE user_id=? AND household_id=? AND role='owner'",
            (session['user_id'], member['household_id'])).fetchone()
        if not owner:
            db.rollback(); return jsonify({'error': 'owner_required'}), 403
        count = db.execute(
            'SELECT COUNT(*) FROM family_asset_member WHERE household_id=?',
            (member['household_id'],)).fetchone()[0]
        if count >= 2:
            db.rollback(); return jsonify({'error': 'household_full'}), 409
        if db.execute('SELECT 1 FROM users WHERE lower(username)=?', (username,)).fetchone():
            db.rollback(); return jsonify({'error': 'username_taken'}), 409
        uid = db.execute(
            "INSERT INTO users(username,password_hash,display_name,role,app_scope,supervisor_id,active) "
            "VALUES(?,?,?,'member','family',NULL,1)",
            (username, password_hash, display)).lastrowid
        db.execute(
            "INSERT INTO family_asset_member(household_id,user_id,display_name,role) "
            "VALUES(?,?,?,'member')", (member['household_id'], uid, display))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'error': 'username_taken'}), 409
    except Exception:
        db.rollback(); raise
    # 비밀번호는 response/log/history 어디에도 되돌려주지 않는다.
    return jsonify(_snapshot(_member())), 201


@bp.post('/api/family-assets/partner-account/password')
@login_required
def family_assets_reset_partner_password():
    """owner가 이 가구에 자동 생성한 family 배우자 계정의 비밀번호만 재발급한다."""
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    if member['role'] != 'owner':
        return jsonify({'error': 'owner_required'}), 403
    password = (request.get_json(silent=True) or {}).get('password')
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return jsonify({'error': '비밀번호는 8~128자'}), 400
    password_hash = generate_password_hash(password)
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        owner = db.execute(
            "SELECT household_id FROM family_asset_member "
            "WHERE user_id=? AND household_id=? AND role='owner'",
            (session['user_id'], member['household_id'])).fetchone()
        if not owner:
            db.rollback(); return jsonify({'error': 'owner_required'}), 403
        partner = db.execute(
            "SELECT u.id,u.username FROM family_asset_member m JOIN users u ON u.id=m.user_id "
            "WHERE m.household_id=? AND m.user_id<>? AND m.role='member' "
            "AND u.app_scope='family' AND u.role='member' AND u.active=1",
            (member['household_id'], session['user_id'])).fetchone()
        if not partner:
            db.rollback(); return jsonify({'error': 'partner_not_managed'}), 409
        db.execute('UPDATE users SET password_hash=? WHERE id=?', (password_hash, partner['id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))


def _asset_payload(d, member):
    kind = _clean_text(d.get('kind'), '분류', required=True, limit=20)
    if kind not in KINDS:
        raise ValueError('지원하지 않는 분류')
    owner_mode = _clean_text(d.get('owner_mode') or 'member', '소유 방식', required=True, limit=12)
    if owner_mode not in OWNER_MODES:
        raise ValueError('지원하지 않는 소유 방식')
    raw_amount = d.get('amount')
    try:
        # JSON 12.9를 int()로 받아 12로 조용히 깎지 않는다. 원 단위 원장은 정수만 허용.
        if isinstance(raw_amount, bool) or isinstance(raw_amount, float):
            raise ValueError
        amount = int(raw_amount)
        if isinstance(raw_amount, str) and str(amount) != raw_amount.strip():
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError('금액은 원 단위 정수로 입력')
    if amount < 0 or amount > 999_999_999_999_999:
        raise ValueError('금액 범위 오류')
    owner_user_id = None
    joint_share = 50
    if owner_mode == 'member':
        try:
            owner_user_id = int(d.get('owner_user_id') or 0)
        except (TypeError, ValueError):
            raise ValueError('소유자 필수')
        exists = query('SELECT 1 FROM family_asset_member WHERE household_id=? AND user_id=?',
                       (member['household_id'], owner_user_id), one=True)
        if not exists:
            raise ValueError('같은 가구 구성원만 소유자로 지정 가능')
    else:
        raw_share = d.get('joint_share', 50)
        try:
            if isinstance(raw_share, bool) or isinstance(raw_share, float):
                raise ValueError
            joint_share = int(raw_share)
            if isinstance(raw_share, str) and str(joint_share) != raw_share.strip():
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError('공동 지분은 정수로 입력')
        if not 0 <= joint_share <= 100:
            raise ValueError('공동 지분은 0~100')
    return {
        'kind': kind,
        'name': _clean_text(d.get('name'), '이름', required=True, limit=80),
        'amount': amount,
        'owner_mode': owner_mode,
        'owner_user_id': owner_user_id,
        'joint_share': joint_share,
        'institution': _clean_text(d.get('institution'), '금융기관', limit=80),
        'note': _clean_text(d.get('note'), '메모', limit=300),
    }


def _expected_revision(d):
    raw = d.get('expected_revision')
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError('최신 revision 필수')
    return raw


@bp.post('/api/family-assets/assets')
@login_required
def family_asset_create():
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    try:
        p = _asset_payload(request.get_json(silent=True) or {}, member)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO family_asset_entry(household_id,kind,name,amount,owner_mode,owner_user_id,"
        "joint_share,institution,note,created_by,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (member['household_id'], p['kind'], p['name'], p['amount'], p['owner_mode'],
         p['owner_user_id'], p['joint_share'], p['institution'], p['note'],
         session['user_id'], session['user_id']))
    _record_change(db, member, 'create', cur.lastrowid, None, p)
    _upsert_monthly_snapshot(db, member['household_id'])
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid}), 201


@bp.patch('/api/family-assets/assets/<int:asset_id>')
@login_required
def family_asset_update(asset_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    existing = query(
        "SELECT id,kind,name,amount,revision FROM family_asset_entry "
        "WHERE id=? AND household_id=?",
        (asset_id, member['household_id']), one=True)
    if not existing:
        return jsonify({'error': 'not_found'}), 404
    try:
        body = request.get_json(silent=True) or {}
        p = _asset_payload(body, member)
        expected_revision = _expected_revision(body)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db()
    cur = db.execute(
        "UPDATE family_asset_entry SET kind=?,name=?,amount=?,owner_mode=?,owner_user_id=?,"
        "joint_share=?,institution=?,note=?,updated_by=?,revision=revision+1,"
        "updated_at=datetime('now','localtime') WHERE id=? AND household_id=? AND revision=?",
        (p['kind'], p['name'], p['amount'], p['owner_mode'], p['owner_user_id'],
         p['joint_share'], p['institution'], p['note'], session['user_id'], asset_id,
         member['household_id'], expected_revision))
    if not cur.rowcount:
        db.rollback()
        return jsonify({'error': 'edit_conflict'}), 409
    _record_change(db, member, 'update', asset_id, existing, p)
    _upsert_monthly_snapshot(db, member['household_id'])
    db.commit()
    return jsonify({'ok': True})


@bp.delete('/api/family-assets/assets/<int:asset_id>')
@login_required
def family_asset_delete(asset_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    db = get_db()
    existing = db.execute(
        "SELECT id,kind,name,amount,revision FROM family_asset_entry "
        "WHERE id=? AND household_id=?",
        (asset_id, member['household_id'])).fetchone()
    if not existing:
        return jsonify({'error': 'not_found'}), 404
    try:
        expected_revision = _expected_revision(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    cur = db.execute(
        "DELETE FROM family_asset_entry WHERE id=? AND household_id=? AND revision=?",
        (asset_id, member['household_id'], expected_revision))
    if not cur.rowcount:
        db.rollback()
        return jsonify({'error': 'edit_conflict'}), 409
    _record_change(db, member, 'delete', asset_id, existing, None)
    _upsert_monthly_snapshot(db, member['household_id'])
    db.commit()
    return jsonify({'ok': True})

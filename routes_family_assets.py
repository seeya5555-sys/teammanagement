"""우리자산 — TRMT iOS 전용 2인 가구 자산 동기화 API.

웹 UI는 제공하지 않는다. 기존 TRMT 인증 계정 두 개가 초대코드로 같은 가구에
가입하고, 그 가구의 구성원만 자산 원장을 읽고 쓸 수 있다.
"""
import secrets
import string

from flask import Blueprint, jsonify, request, session

from app_core import get_db, query
from helpers_shared import login_required

bp = Blueprint("routes_family_assets", __name__)

KINDS = {'income', 'cash', 'saving', 'stock', 'property', 'loan', 'other'}
OWNER_MODES = {'member', 'joint'}
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


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
        return {'setup_required': True, 'household': None, 'members': [], 'assets': []}
    hid = member['household_id']
    members = query(
        "SELECT m.user_id id,m.display_name,m.role,m.joined_at "
        "FROM family_asset_member m WHERE m.household_id=? ORDER BY m.joined_at,m.user_id", (hid,))
    assets = query(
        "SELECT a.id,a.kind,a.name,a.amount,a.owner_mode,a.owner_user_id,a.joint_share,"
        "a.institution,a.note,a.updated_at,a.updated_by,u.display_name updated_by_name "
        "FROM family_asset_entry a LEFT JOIN users u ON u.id=a.updated_by "
        "WHERE a.household_id=? ORDER BY a.updated_at DESC,a.id DESC", (hid,))
    return {
        'setup_required': False,
        'household': {'id': hid, 'name': member['household_name'],
                      'invite_code': member['invite_code'], 'me_user_id': session['user_id']},
        'members': [dict(x) for x in members],
        'assets': [dict(x) for x in assets],
    }


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
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid}), 201


@bp.patch('/api/family-assets/assets/<int:asset_id>')
@login_required
def family_asset_update(asset_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    existing = query('SELECT id FROM family_asset_entry WHERE id=? AND household_id=?',
                     (asset_id, member['household_id']), one=True)
    if not existing:
        return jsonify({'error': 'not_found'}), 404
    try:
        p = _asset_payload(request.get_json(silent=True) or {}, member)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db()
    db.execute(
        "UPDATE family_asset_entry SET kind=?,name=?,amount=?,owner_mode=?,owner_user_id=?,"
        "joint_share=?,institution=?,note=?,updated_by=?,updated_at=datetime('now','localtime') "
        "WHERE id=? AND household_id=?",
        (p['kind'], p['name'], p['amount'], p['owner_mode'], p['owner_user_id'],
         p['joint_share'], p['institution'], p['note'], session['user_id'], asset_id,
         member['household_id']))
    db.commit()
    return jsonify({'ok': True})


@bp.delete('/api/family-assets/assets/<int:asset_id>')
@login_required
def family_asset_delete(asset_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    db = get_db()
    cur = db.execute('DELETE FROM family_asset_entry WHERE id=? AND household_id=?',
                     (asset_id, member['household_id']))
    db.commit()
    if not cur.rowcount:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'ok': True})

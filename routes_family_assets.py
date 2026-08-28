"""우리자산 — TRMT iOS 전용 2인 가구 자산 동기화 API.

웹 UI는 제공하지 않는다. 기존 TRMT 인증 계정 두 개가 초대코드로 같은 가구에
가입하고, 그 가구의 구성원만 자산 원장을 읽고 쓸 수 있다.
"""
import secrets
import sqlite3
import re
import base64
import binascii
import datetime as dt

from flask import Blueprint, Response, jsonify, request, session
from werkzeug.security import generate_password_hash

from app_core import get_db, query
from helpers_shared import login_required

bp = Blueprint("routes_family_assets", __name__)

KINDS = {'income', 'cash', 'saving', 'stock', 'property', 'loan', 'other'}
OWNER_MODES = {'member', 'joint'}
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{3,39}$')
EVIDENCE_KINDS = {'income', 'saving', 'stock'}
MAX_EVIDENCE_BYTES = 2_000_000
CASH_EXPENSE_CATEGORIES = {
    'living', 'utilities', 'home_loan_interest', 'car_loan_interest',
    'insurance', 'education', 'medical', 'other',
}
MAX_MONEY = 999_999_999_999_999


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
                'history': [], 'trends': [], 'cash_flow': None}
    hid = member['household_id']
    members = query(
        "SELECT m.user_id id,m.display_name,m.role,m.joined_at,"
        "CASE WHEN u.app_scope='family' THEN u.username END login_username "
        "FROM family_asset_member m JOIN users u ON u.id=m.user_id "
        "WHERE m.household_id=? ORDER BY m.joined_at,m.user_id", (hid,))
    assets = query(
        "SELECT a.id,a.kind,a.name,a.amount,a.owner_mode,a.owner_user_id,a.joint_share,"
        "a.institution,a.note,CASE WHEN a.monthly_flow_month=strftime('%Y-%m','now','+9 hours') "
        "THEN a.monthly_flow_amount ELSE 0 END monthly_flow_amount,"
        "a.revision,a.updated_at,a.updated_by,u.display_name updated_by_name,"
        "CASE WHEN a.evidence_image IS NOT NULL THEN 1 ELSE 0 END evidence_available "
        "FROM family_asset_entry a LEFT JOIN users u ON u.id=a.updated_by "
        "WHERE a.household_id=? ORDER BY a.updated_at DESC,a.id DESC", (hid,))
    history = query(
        "SELECT h.id,h.asset_id,h.action,h.asset_name,h.kind,h.amount_before,h.amount_after,"
        "h.monthly_flow_before,h.monthly_flow_after,h.changed_by,h.created_at,"
        "COALESCE(u.display_name,u.username,'구성원') changed_by_name "
        "FROM family_asset_history h LEFT JOIN users u ON u.id=h.changed_by "
        "WHERE h.household_id=? ORDER BY h.id DESC LIMIT 50", (hid,))
    current = _totals_from_rows(assets)
    # 서비스 기준 시각은 KST로 고정한다. 서버 OS timezone과 무관하게 월 경계가 같다.
    month = query("SELECT strftime('%Y-%m','now','+9 hours') month", one=True)['month']
    first_month = _shift_month(month, -11)
    stored_trends = query(
        "SELECT month,total_assets,total_debt,net_worth,captured_at "
        "FROM family_asset_monthly_snapshot WHERE household_id=? AND month BETWEEN ? AND ? "
        "ORDER BY month", (hid, first_month, month))
    opening_trend = query(
        "SELECT month,total_assets,total_debt,net_worth,captured_at "
        "FROM family_asset_monthly_snapshot WHERE household_id=? AND month<? "
        "ORDER BY month DESC LIMIT 1", (hid, first_month), one=True)
    trends = _continuous_trends(stored_trends, opening_trend, month, current)
    asset_items = []
    for row in assets:
        item = dict(row)
        item['evidence_available'] = bool(item['evidence_available'])
        asset_items.append(item)
    return {
        'setup_required': False,
        'household': {'id': hid, 'name': member['household_name'],
                      'invite_code': member['invite_code'], 'me_user_id': session['user_id']},
        'members': [dict(x) for x in members],
        'assets': asset_items,
        'history': [dict(x) for x in history],
        'trends': trends,
        'cash_flow': _cashflow_snapshot(hid, members, assets, month),
    }


def _cashflow_snapshot(household_id, members, assets, month):
    expenses = query(
        "SELECT e.id,e.category,e.name,e.amount,e.spent_on,e.created_by,"
        "COALESCE(u.display_name,u.username,'구성원') created_by_name "
        "FROM family_cash_expense e LEFT JOIN users u ON u.id=e.created_by "
        "WHERE e.household_id=? AND substr(e.spent_on,1,7)=? "
        "ORDER BY e.spent_on DESC,e.id DESC", (household_id, month))
    ordinary_total = sum(int(row['amount']) for row in expenses)
    allowance_items = []
    allowance_total = 0
    for member in members:
        budget = query(
            "SELECT id,allocated_amount,revision FROM family_allowance_budget "
            "WHERE household_id=? AND member_user_id=? AND month=?",
            (household_id, member['id'], month), one=True)
        spent_items = []
        allocated = revision = budget_id = 0
        if budget:
            budget_id = budget['id']; allocated = int(budget['allocated_amount'])
            revision = int(budget['revision'])
            spent_items = [dict(row) for row in query(
                "SELECT e.id,e.name,e.amount,e.spent_on,e.created_by,"
                "COALESCE(u.display_name,u.username,'구성원') created_by_name "
                "FROM family_allowance_expense e LEFT JOIN users u ON u.id=e.created_by "
                "WHERE e.budget_id=? AND e.household_id=? ORDER BY e.spent_on DESC,e.id DESC",
                (budget_id, household_id))]
        spent = sum(int(row['amount']) for row in spent_items)
        allowance_total += allocated
        allowance_items.append({
            'id': budget_id, 'member_user_id': member['id'],
            'member_name': member['display_name'], 'month': month,
            'allocated_amount': allocated, 'spent_amount': spent,
            'remaining_amount': allocated - spent, 'revision': revision,
            'expenses': spent_items,
        })
    salary = sum(int(row['amount']) for row in assets if row['kind'] == 'income')
    expense_total = ordinary_total + allowance_total
    return {
        'month': month,
        'salary_income': salary,
        'ordinary_expenses': ordinary_total,
        'allowance_allocated': allowance_total,
        'expense_total': expense_total,
        'available_after_expenses': salary - expense_total,
        'expenses': [dict(row) for row in expenses],
        'allowances': allowance_items,
    }


def _money(d, key, label, *, allow_zero=False):
    raw = d.get(key)
    try:
        if isinstance(raw, bool) or isinstance(raw, float):
            raise ValueError
        value = int(raw)
        if isinstance(raw, str) and str(value) != raw.strip():
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'{label}은 원 단위 정수로 입력')
    minimum = 0 if allow_zero else 1
    if not minimum <= value <= MAX_MONEY:
        raise ValueError(f'{label} 범위 오류')
    return value


def _current_spent_on(value):
    try:
        spent_on = dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError('사용일은 YYYY-MM-DD 형식')
    today_text = query("SELECT date('now','+9 hours') today", one=True)['today']
    today = dt.date.fromisoformat(today_text)
    if spent_on.strftime('%Y-%m') != today.strftime('%Y-%m') or spent_on > today:
        raise ValueError('이번 달 오늘까지의 사용내역만 입력 가능')
    return spent_on.isoformat(), today.strftime('%Y-%m')


def _shift_month(month, delta):
    """YYYY-MM를 연/월 경계를 보존해 delta개월 이동한다."""
    year, number = (int(part) for part in month.split('-'))
    index = year * 12 + number - 1 + delta
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def _continuous_trends(stored_rows, opening_row, current_month, current_totals):
    """실데이터가 시작된 뒤의 빈 달만 직전 확정 잔액으로 이월한다."""
    first_month = _shift_month(current_month, -11)
    stored = {row['month']: dict(row) for row in stored_rows}
    carried = dict(opening_row) if opening_row else None
    result = []
    for offset in range(12):
        month = _shift_month(first_month, offset)
        if month == current_month:
            result.append({'month': month, **current_totals, 'captured_at': None,
                           'carried_forward': False})
            continue
        if month in stored:
            carried = stored[month]
            # 쿼리/스키마가 확장돼도 API에는 trend 계약 필드만 노출한다.
            result.append({
                'month': month,
                'total_assets': carried['total_assets'],
                'total_debt': carried['total_debt'],
                'net_worth': carried['net_worth'],
                'captured_at': carried['captured_at'],
                'carried_forward': False,
            })
        elif carried:
            result.append({
                'month': month,
                'total_assets': carried['total_assets'],
                'total_debt': carried['total_debt'],
                'net_worth': carried['net_worth'],
                'captured_at': None,
                'carried_forward': True,
            })
    return result


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
        "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (member['household_id'], asset_id, action, row['name'], row['kind'],
         before['amount'] if before else None, after['amount'] if after else None,
         before['monthly_flow_amount'] if before else None,
         after['monthly_flow_amount'] if after else None,
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
    flow_provided = 'monthly_flow_amount' in d
    monthly_flow_amount = None
    if flow_provided:
        raw_flow = d.get('monthly_flow_amount')
        try:
            if isinstance(raw_flow, bool) or isinstance(raw_flow, float):
                raise ValueError
            monthly_flow_amount = int(raw_flow)
            if isinstance(raw_flow, str) and str(monthly_flow_amount) != raw_flow.strip():
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError('이번 달 흐름은 원 단위 정수로 입력')
        if monthly_flow_amount < 0 or monthly_flow_amount > 999_999_999_999_999:
            raise ValueError('이번 달 흐름 금액 범위 오류')
    if kind not in {'saving', 'stock', 'loan'}:
        if monthly_flow_amount not in (None, 0):
            raise ValueError('저축·투자·대출만 이번 달 흐름을 기록할 수 있음')
        # 분류를 바꿀 때 옛 납입액이 다른 자산에 붙어 남지 않게 명시 초기화한다.
        monthly_flow_amount = 0
        flow_provided = True
    evidence_provided = 'evidence_base64' in d
    evidence_image = evidence_mime = None
    if evidence_provided:
        encoded = d.get('evidence_base64')
        if not isinstance(encoded, str) or not encoded:
            raise ValueError('증빙 캡처 데이터 오류')
        # 2MB binary의 base64 상한(ceil(n/3)*4)보다 큰 입력은 디코딩 전에 거절한다.
        # 그렇지 않으면 공격자가 불필요한 대형 bytes allocation을 만들 수 있다.
        if len(encoded) > ((MAX_EVIDENCE_BYTES + 2) // 3) * 4:
            raise ValueError('증빙 캡처는 2MB 이하 이미지여야 함')
        try:
            evidence_image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError('증빙 캡처 데이터 오류')
        if not 1_000 <= len(evidence_image) <= MAX_EVIDENCE_BYTES:
            raise ValueError('증빙 캡처는 2MB 이하 이미지여야 함')
        if evidence_image.startswith(b'\xff\xd8\xff'):
            evidence_mime = 'image/jpeg'
        elif evidence_image.startswith(b'\x89PNG\r\n\x1a\n'):
            evidence_mime = 'image/png'
        else:
            raise ValueError('증빙 캡처는 JPEG 또는 PNG만 가능')
        if kind not in EVIDENCE_KINDS:
            raise ValueError('이 분류는 증빙 캡처 자동입력 대상이 아님')
    return {
        'kind': kind,
        'name': _clean_text(d.get('name'), '이름', required=True, limit=80),
        'amount': amount,
        'owner_mode': owner_mode,
        'owner_user_id': owner_user_id,
        'joint_share': joint_share,
        'institution': _clean_text(d.get('institution'), '금융기관', limit=80),
        'note': _clean_text(d.get('note'), '메모', limit=300),
        'monthly_flow_amount': monthly_flow_amount,
        'monthly_flow_provided': flow_provided,
        'evidence_provided': evidence_provided,
        'evidence_image': evidence_image,
        'evidence_mime': evidence_mime,
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
    if p['kind'] in EVIDENCE_KINDS and not p['evidence_provided']:
        return jsonify({'error': 'screenshot_required'}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO family_asset_entry(household_id,kind,name,amount,owner_mode,owner_user_id,"
        "joint_share,institution,note,monthly_flow_amount,monthly_flow_month,evidence_image,evidence_mime,"
        "evidence_captured_at,created_by,updated_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m','now','+9 hours'),?,?,"
        "CASE WHEN ? IS NOT NULL THEN datetime('now','+9 hours') END,?,?)",
        (member['household_id'], p['kind'], p['name'], p['amount'], p['owner_mode'],
         p['owner_user_id'], p['joint_share'], p['institution'], p['note'],
         p['monthly_flow_amount'] or 0, p['evidence_image'], p['evidence_mime'], p['evidence_image'],
         session['user_id'], session['user_id']))
    p['monthly_flow_amount'] = p['monthly_flow_amount'] or 0
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
        "SELECT id,kind,name,amount,CASE WHEN monthly_flow_month=strftime('%Y-%m','now','+9 hours') "
        "THEN monthly_flow_amount ELSE 0 END monthly_flow_amount,revision,"
        "CASE WHEN evidence_image IS NOT NULL THEN 1 ELSE 0 END evidence_available "
        "FROM family_asset_entry WHERE id=? AND household_id=?",
        (asset_id, member['household_id']), one=True)
    if not existing:
        return jsonify({'error': 'not_found'}), 404
    try:
        body = request.get_json(silent=True) or {}
        p = _asset_payload(body, member)
        expected_revision = _expected_revision(body)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    evidence_needed = (p['kind'] in EVIDENCE_KINDS and
                       (p['kind'] != existing['kind'] or p['amount'] != existing['amount']))
    if evidence_needed and not p['evidence_provided']:
        return jsonify({'error': 'screenshot_required'}), 400
    db = get_db()
    cur = db.execute(
        "UPDATE family_asset_entry SET kind=?,name=?,amount=?,owner_mode=?,owner_user_id=?,"
        "joint_share=?,institution=?,note=?,"
        "monthly_flow_amount=CASE WHEN ? THEN ? ELSE monthly_flow_amount END,"
        "monthly_flow_month=CASE WHEN ? THEN strftime('%Y-%m','now','+9 hours') ELSE monthly_flow_month END,"
        "evidence_image=CASE WHEN ? THEN ? WHEN ? THEN NULL ELSE evidence_image END,"
        "evidence_mime=CASE WHEN ? THEN ? WHEN ? THEN NULL ELSE evidence_mime END,"
        "evidence_captured_at=CASE WHEN ? THEN datetime('now','+9 hours') "
        "WHEN ? THEN NULL ELSE evidence_captured_at END,"
        "updated_by=?,revision=revision+1,"
        "updated_at=datetime('now','localtime') WHERE id=? AND household_id=? AND revision=?",
        (p['kind'], p['name'], p['amount'], p['owner_mode'], p['owner_user_id'],
         p['joint_share'], p['institution'], p['note'], p['monthly_flow_provided'],
         p['monthly_flow_amount'] or 0, p['monthly_flow_provided'],
         p['evidence_provided'], p['evidence_image'], p['kind'] not in EVIDENCE_KINDS,
         p['evidence_provided'], p['evidence_mime'], p['kind'] not in EVIDENCE_KINDS,
         p['evidence_provided'], p['kind'] not in EVIDENCE_KINDS,
         session['user_id'], asset_id,
         member['household_id'], expected_revision))
    if not cur.rowcount:
        db.rollback()
        return jsonify({'error': 'edit_conflict'}), 409
    if p['monthly_flow_amount'] is None:
        p['monthly_flow_amount'] = existing['monthly_flow_amount']
    _record_change(db, member, 'update', asset_id, existing, p)
    _upsert_monthly_snapshot(db, member['household_id'])
    db.commit()
    return jsonify({'ok': True})


@bp.get('/api/family-assets/assets/<int:asset_id>/evidence')
@login_required
def family_asset_evidence(asset_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    row = get_db().execute(
        "SELECT evidence_image,evidence_mime FROM family_asset_entry "
        "WHERE id=? AND household_id=?", (asset_id, member['household_id'])).fetchone()
    if not row or row['evidence_image'] is None:
        return jsonify({'error': 'not_found'}), 404
    return Response(row['evidence_image'], mimetype=row['evidence_mime'] or 'image/jpeg',
                    headers={'Cache-Control': 'private, no-store',
                             'X-Content-Type-Options': 'nosniff'})


@bp.delete('/api/family-assets/assets/<int:asset_id>')
@login_required
def family_asset_delete(asset_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    db = get_db()
    existing = db.execute(
        "SELECT id,kind,name,amount,CASE WHEN monthly_flow_month=strftime('%Y-%m','now','+9 hours') "
        "THEN monthly_flow_amount ELSE 0 END monthly_flow_amount,revision FROM family_asset_entry "
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


@bp.post('/api/family-assets/cash-expenses')
@login_required
def family_cash_expense_create():
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    d = request.get_json(silent=True) or {}
    try:
        category = _clean_text(d.get('category'), '분류', required=True, limit=30)
        if category not in CASH_EXPENSE_CATEGORIES:
            raise ValueError('지원하지 않는 지출 분류')
        name = _clean_text(d.get('name'), '사용처', required=True, limit=80)
        amount = _money(d, 'amount', '지출 금액')
        spent_on, _month = _current_spent_on(d.get('spent_on'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        db.execute(
            "INSERT INTO family_cash_expense(household_id,category,name,amount,spent_on,created_by) "
            "VALUES(?,?,?,?,?,?)",
            (member['household_id'], category, name, amount, spent_on, session['user_id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member())), 201


@bp.delete('/api/family-assets/cash-expenses/<int:expense_id>')
@login_required
def family_cash_expense_delete(expense_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute(
            "SELECT source_type FROM family_cash_expense WHERE id=? AND household_id=?",
            (expense_id, member['household_id'])).fetchone()
        if not row:
            db.rollback(); return jsonify({'error': 'not_found'}), 404
        if row['source_type']:
            db.rollback(); return jsonify({'error': 'linked_expense_cannot_delete'}), 409
        db.execute("DELETE FROM family_cash_expense WHERE id=? AND household_id=?",
                   (expense_id, member['household_id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))


@bp.put('/api/family-assets/allowances/<int:member_user_id>')
@login_required
def family_allowance_set(member_user_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    d = request.get_json(silent=True) or {}
    try:
        amount = _money(d, 'allocated_amount', '용돈 배정액', allow_zero=True)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        target = db.execute(
            "SELECT 1 FROM family_asset_member WHERE household_id=? AND user_id=?",
            (member['household_id'], member_user_id)).fetchone()
        if not target:
            db.rollback(); return jsonify({'error': 'not_found'}), 404
        month = db.execute("SELECT strftime('%Y-%m','now','+9 hours')").fetchone()[0]
        existing = db.execute(
            "SELECT id FROM family_allowance_budget WHERE household_id=? AND member_user_id=? AND month=?",
            (member['household_id'], member_user_id, month)).fetchone()
        spent = 0
        if existing:
            spent = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM family_allowance_expense "
                "WHERE budget_id=? AND household_id=?",
                (existing['id'], member['household_id'])).fetchone()[0]
        if amount < int(spent):
            db.rollback(); return jsonify({'error': 'allowance_below_spent'}), 400
        if existing:
            db.execute(
                "UPDATE family_allowance_budget SET allocated_amount=?,updated_by=?,revision=revision+1,"
                "updated_at=datetime('now','+9 hours') WHERE id=? AND household_id=?",
                (amount, session['user_id'], existing['id'], member['household_id']))
        else:
            db.execute(
                "INSERT INTO family_allowance_budget(household_id,member_user_id,month,"
                "allocated_amount,created_by,updated_by) VALUES(?,?,?,?,?,?)",
                (member['household_id'], member_user_id, month, amount,
                 session['user_id'], session['user_id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))


@bp.post('/api/family-assets/allowances/<int:member_user_id>/expenses')
@login_required
def family_allowance_expense_create(member_user_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    d = request.get_json(silent=True) or {}
    try:
        name = _clean_text(d.get('name'), '사용처', required=True, limit=80)
        amount = _money(d, 'amount', '용돈 사용액')
        spent_on, month = _current_spent_on(d.get('spent_on'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        budget = db.execute(
            "SELECT id,allocated_amount FROM family_allowance_budget "
            "WHERE household_id=? AND member_user_id=? AND month=?",
            (member['household_id'], member_user_id, month)).fetchone()
        if not budget:
            db.rollback(); return jsonify({'error': 'allowance_required'}), 409
        spent = int(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM family_allowance_expense "
            "WHERE budget_id=? AND household_id=?",
            (budget['id'], member['household_id'])).fetchone()[0])
        if spent + amount > int(budget['allocated_amount']):
            db.rollback(); return jsonify({'error': 'allowance_exceeded'}), 400
        db.execute(
            "INSERT INTO family_allowance_expense(budget_id,household_id,name,amount,spent_on,created_by) "
            "VALUES(?,?,?,?,?,?)",
            (budget['id'], member['household_id'], name, amount, spent_on, session['user_id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member())), 201


@bp.delete('/api/family-assets/allowance-expenses/<int:expense_id>')
@login_required
def family_allowance_expense_delete(expense_id):
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        cur = db.execute(
            "DELETE FROM family_allowance_expense WHERE id=? AND household_id=?",
            (expense_id, member['household_id']))
        if not cur.rowcount:
            db.rollback(); return jsonify({'error': 'not_found'}), 404
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))

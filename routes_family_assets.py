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


def _input_salary_items(household_id, month):
    return [dict(row) for row in query(
        "SELECT s.member_user_id,COALESCE(m.display_name,u.display_name,u.username,'구성원') member_name,"
        "s.amount FROM family_cashflow_monthly_salary s "
        "LEFT JOIN family_asset_member m ON m.household_id=s.household_id "
        "AND m.user_id=s.member_user_id LEFT JOIN users u ON u.id=s.member_user_id "
        "WHERE s.household_id=? AND s.month=? ORDER BY s.member_user_id",
        (household_id, month))]


def _close_salary_items(close_id):
    return [dict(row) for row in query(
        "SELECT member_user_id,member_name,amount FROM family_cashflow_monthly_close_salary "
        "WHERE close_id=? ORDER BY member_user_id", (close_id,))]


def _salary_projection(total, items):
    assigned = sum(int(item['amount']) for item in items)
    return items, int(total) - assigned


def _reconciliation_items(reconciliation_id):
    return [dict(row) for row in query(
        "SELECT asset_id,asset_name,kind,book_amount,actual_amount,"
        "actual_amount-book_amount difference,asset_revision,note "
        "FROM family_asset_reconciliation_item WHERE reconciliation_id=? "
        "ORDER BY kind,asset_name,asset_id", (reconciliation_id,))]


def _reconciliation_projection(row):
    item = dict(row)
    items = _reconciliation_items(item['id'])
    book_assets = sum(int(x['book_amount']) for x in items if x['kind'] != 'loan')
    actual_assets = sum(int(x['actual_amount']) for x in items if x['kind'] != 'loan')
    book_debt = sum(int(x['book_amount']) for x in items if x['kind'] == 'loan')
    actual_debt = sum(int(x['actual_amount']) for x in items if x['kind'] == 'loan')
    item.update({
        'items': items,
        'book_assets': book_assets,
        'actual_assets': actual_assets,
        'book_debt': book_debt,
        'actual_debt': actual_debt,
        'book_net_worth': book_assets - book_debt,
        'actual_net_worth': actual_assets - actual_debt,
        'difference': (actual_assets - actual_debt) - (book_assets - book_debt),
        'is_balanced': all(int(x['difference']) == 0 for x in items),
    })
    return item


def _snapshot(member):
    if not member:
        return {'setup_required': True, 'household': None, 'members': [], 'assets': [],
                'history': [], 'trends': [], 'cash_flow': None, 'reconciliations': []}
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
    close_items = []
    for row in query(
            "SELECT c.id,c.month,c.revision,c.salary_income,c.ordinary_expenses,"
            "c.allowance_allocated,c.saving_transfers,c.investment_transfers,"
            "c.loan_principal_payments,c.allocated_income,c.unallocated_income,"
            "c.closed_by,COALESCE(u.display_name,u.username,'구성원') closed_by_name,c.closed_at "
            "FROM family_cashflow_monthly_close c LEFT JOIN users u ON u.id=c.closed_by "
            "WHERE c.household_id=? AND c.month BETWEEN ? AND ? "
            "ORDER BY c.month DESC,c.revision DESC", (hid, first_month, month)):
        item = dict(row)
        item['salary_by_member'], item['salary_unassigned'] = _salary_projection(
            item['salary_income'], _close_salary_items(item['id']))
        close_items.append(item)
    input_items = []
    for row in query(
            "SELECT month,salary_income,saving_transfers,investment_transfers,"
            "loan_principal_payments,revision,updated_at FROM family_cashflow_monthly_input "
            "WHERE household_id=? AND month BETWEEN ? AND ? ORDER BY month DESC",
            (hid, first_month, month)):
        item = dict(row)
        item['salary_by_member'], item['salary_unassigned'] = _salary_projection(
            item['salary_income'], _input_salary_items(hid, item['month']))
        input_items.append(item)
    reconciliations = [_reconciliation_projection(row) for row in query(
        "SELECT r.id,r.month,r.revision,r.reconciled_by,"
        "COALESCE(u.display_name,u.username,'구성원') reconciled_by_name,r.reconciled_at "
        "FROM family_asset_reconciliation r LEFT JOIN users u ON u.id=r.reconciled_by "
        "WHERE r.household_id=? AND r.month BETWEEN ? AND ? "
        "ORDER BY r.month DESC,r.revision DESC", (hid, first_month, month))]
    return {
        'setup_required': False,
        'household': {'id': hid, 'name': member['household_name'],
                      'invite_code': member['invite_code'], 'me_user_id': session['user_id']},
        'members': [dict(x) for x in members],
        'assets': asset_items,
        'history': [dict(x) for x in history],
        'trends': trends,
        'cash_flow': _cashflow_snapshot(hid, members, assets, month),
        'cash_flow_history': close_items,
        'cash_flow_inputs': input_items,
        'reconciliations': reconciliations,
    }


def _cashflow_snapshot(household_id, members, assets, month):
    expenses = query(
        "SELECT e.id,e.category,e.name,e.amount,e.spent_on,e.created_by,"
        "COALESCE(u.display_name,u.username,'구성원') created_by_name "
        "FROM family_cash_expense e LEFT JOIN users u ON u.id=e.created_by "
        "WHERE e.household_id=? AND e.created_by=? AND substr(e.spent_on,1,7)=? "
        "ORDER BY e.spent_on DESC,e.id DESC", (household_id, session['user_id'], month))
    ordinary_total = int(query(
        "SELECT COALESCE(SUM(amount),0) total FROM family_cash_expense "
        "WHERE household_id=? AND substr(spent_on,1,7)=?",
        (household_id, month), one=True)['total'])
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
            if member['id'] == session['user_id']:
                spent_items = [dict(row) for row in query(
                    "SELECT e.id,e.name,e.amount,e.spent_on,e.created_by,"
                    "COALESCE(u.display_name,u.username,'구성원') created_by_name "
                    "FROM family_allowance_expense e LEFT JOIN users u ON u.id=e.created_by "
                    "WHERE e.budget_id=? AND e.household_id=? ORDER BY e.spent_on DESC,e.id DESC",
                    (budget_id, household_id))]
        spent = int(query(
            "SELECT COALESCE(SUM(amount),0) total FROM family_allowance_expense "
            "WHERE budget_id=? AND household_id=?", (budget_id, household_id), one=True)['total']) \
            if budget_id else 0
        allowance_total += allocated
        allowance_items.append({
            'id': budget_id, 'member_user_id': member['id'],
            'member_name': member['display_name'], 'month': month,
            'allocated_amount': allocated, 'spent_amount': spent,
            'remaining_amount': allocated - spent, 'revision': revision,
            'expenses': spent_items, 'details_private': member['id'] != session['user_id'],
        })
    monthly_input = query(
        "SELECT salary_income,saving_transfers,investment_transfers,"
        "loan_principal_payments,revision FROM family_cashflow_monthly_input "
        "WHERE household_id=? AND month=?", (household_id, month), one=True)
    if monthly_input:
        salary = int(monthly_input['salary_income'])
        salary_by_member, salary_unassigned = _salary_projection(
            salary, _input_salary_items(household_id, month))
        saving_transfers = int(monthly_input['saving_transfers'])
        investment_transfers = int(monthly_input['investment_transfers'])
        loan_payments = int(monthly_input['loan_principal_payments'])
        input_revision = int(monthly_input['revision'])
        input_source = 'monthly_input'
    else:
        salary = sum(int(row['amount']) for row in assets if row['kind'] == 'income')
        salary_by_member = []
        salary_unassigned = salary
        saving_transfers = sum(int(row['monthly_flow_amount']) for row in assets
                               if row['kind'] == 'saving')
        investment_transfers = sum(int(row['monthly_flow_amount']) for row in assets
                                   if row['kind'] == 'stock')
        loan_payments = sum(int(row['monthly_flow_amount']) for row in assets
                            if row['kind'] == 'loan')
        input_revision = 0
        input_source = 'asset_fallback'
    # In allocation model v2 every loan monthly_flow_amount is principal-only.
    # Interest is recorded independently as an ordinary expense, so it cannot be double counted.
    loan_principal_payments = loan_payments
    loan_interest_expenses = int(query(
        "SELECT COALESCE(SUM(amount),0) total FROM family_cash_expense "
        "WHERE household_id=? AND substr(spent_on,1,7)=? "
        "AND category IN ('home_loan_interest','car_loan_interest')",
        (household_id, month), one=True)['total'])
    expense_total = ordinary_total + allowance_total
    allocated_income = (expense_total + saving_transfers + investment_transfers
                        + loan_principal_payments)
    latest_close = query(
        "SELECT id,revision,salary_income,ordinary_expenses,allowance_allocated,saving_transfers,"
        "investment_transfers,loan_principal_payments,allocated_income,unallocated_income,closed_at "
        "FROM family_cashflow_monthly_close WHERE household_id=? AND month=? "
        "ORDER BY revision DESC LIMIT 1", (household_id, month), one=True)
    current_values = (salary, ordinary_total, allowance_total, saving_transfers,
                      investment_transfers, loan_principal_payments, allocated_income,
                      salary - allocated_income)
    close_values = tuple(int(latest_close[key]) for key in (
        'salary_income', 'ordinary_expenses', 'allowance_allocated', 'saving_transfers',
        'investment_transfers', 'loan_principal_payments', 'allocated_income',
        'unallocated_income')) if latest_close else None
    current_salary_values = tuple(
        (int(item['member_user_id']), int(item['amount'])) for item in salary_by_member)
    closed_salary_values = tuple(
        (int(item['member_user_id']), int(item['amount']))
        for item in _close_salary_items(latest_close['id'])) if latest_close else None
    return {
        'month': month,
        'allocation_model_version': 2,
        'monthly_input_revision': input_revision,
        'input_source': input_source,
        'salary_income': salary,
        'salary_by_member': salary_by_member,
        'salary_unassigned': salary_unassigned,
        'ordinary_expenses': ordinary_total,
        'my_ordinary_expenses': sum(int(row['amount']) for row in expenses),
        'expense_details_private': True,
        'allowance_allocated': allowance_total,
        'saving_transfers': saving_transfers,
        'investment_transfers': investment_transfers,
        'loan_payments': loan_payments,
        'loan_interest_expenses': loan_interest_expenses,
        'loan_principal_payments': loan_principal_payments,
        'allocated_income': allocated_income,
        'unallocated_income': salary - allocated_income,
        'close_revision': int(latest_close['revision']) if latest_close else 0,
        'closed_at': latest_close['closed_at'] if latest_close else None,
        'close_stale': bool(latest_close and (
            close_values != current_values or closed_salary_values != current_salary_values)),
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


def _member_salary_payload(d, member):
    raw_items = d.get('salary_by_member')
    if raw_items is None:
        return None, _money(d, 'salary_income', '월급·기타수입', allow_zero=True)
    if not isinstance(raw_items, list):
        raise ValueError('구성원별 급여 형식 오류')
    member_ids = {int(row['user_id']) for row in query(
        "SELECT user_id FROM family_asset_member WHERE household_id=?",
        (member['household_id'],))}
    parsed = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError('구성원별 급여 형식 오류')
        raw_id = item.get('member_user_id')
        if isinstance(raw_id, bool):
            raise ValueError('구성원별 급여 대상 오류')
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError, OverflowError):
            raise ValueError('구성원별 급여 대상 오류')
        if user_id in seen:
            raise ValueError('구성원별 급여 대상 중복')
        seen.add(user_id)
        parsed.append({'member_user_id': user_id,
                       'amount': _money(item, 'amount', '구성원 급여', allow_zero=True)})
    if seen != member_ids:
        raise ValueError('현재 가구 구성원 전체 급여 입력 필요')
    total = sum(item['amount'] for item in parsed)
    if total > MAX_MONEY:
        raise ValueError('가구 총급여 범위 오류')
    return sorted(parsed, key=lambda item: item['member_user_id']), total


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


def _editable_cashflow_month(raw, current):
    month = str(raw or current).strip()
    if not re.fullmatch(r'\d{4}-\d{2}', month) or not _shift_month(current, -11) <= month <= current:
        raise ValueError('최근 12개월만 입력·마감할 수 있음')
    return month


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
    # 급여는 월별 CAS 원장이 단일 정본이다. income kind는 구버전 데이터 decode와
    # 월 원장 미입력 시 fallback을 위해 읽기만 유지하고 신규 생성은 막는다.
    if p['kind'] == 'income':
        return jsonify({'error': 'income_use_monthly_input'}), 409
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
    # 기존 income 행은 삭제하거나 실제 자산 분류로 재분류할 수 있지만, 급여 금액을
    # 자산 편집기에서 계속 수정하거나 다른 자산을 income으로 바꾸지는 못한다.
    if p['kind'] == 'income':
        return jsonify({'error': 'income_use_monthly_input'}), 409
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
            "SELECT source_type FROM family_cash_expense "
            "WHERE id=? AND household_id=? AND created_by=?",
            (expense_id, member['household_id'], session['user_id'])).fetchone()
        if not row:
            db.rollback(); return jsonify({'error': 'not_found'}), 404
        if row['source_type']:
            db.rollback(); return jsonify({'error': 'linked_expense_cannot_delete'}), 409
        db.execute("DELETE FROM family_cash_expense WHERE id=? AND household_id=? AND created_by=?",
                   (expense_id, member['household_id'], session['user_id']))
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))


@bp.put('/api/family-assets/cash-flow/monthly-input')
@login_required
def family_cashflow_monthly_input_set():
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    d = request.get_json(silent=True) or {}
    try:
        current = query("SELECT strftime('%Y-%m','now','+9 hours') month", one=True)['month']
        month = _editable_cashflow_month(d.get('month'), current)
        salary_items, salary_total = _member_salary_payload(d, member)
        values = {key: _money(d, key, label, allow_zero=True) for key, label in (
            ('saving_transfers', '저축 이체'),
            ('investment_transfers', '투자 이체'),
            ('loan_principal_payments', '대출 원금'))}
        values['salary_income'] = salary_total
        expected = d.get('expected_revision', 0)
        if isinstance(expected, bool) or int(expected) != expected or int(expected) < 0:
            raise ValueError('입력 revision 오류')
        expected = int(expected)
    except (ValueError, TypeError, OverflowError) as e:
        return jsonify({'error': str(e)}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        existing = db.execute(
            "SELECT revision FROM family_cashflow_monthly_input WHERE household_id=? AND month=?",
            (member['household_id'], month)).fetchone()
        current_revision = int(existing['revision']) if existing else 0
        if current_revision != expected:
            db.rollback(); return jsonify({'error': 'revision_conflict'}), 409
        if salary_items is None and db.execute(
                "SELECT 1 FROM family_cashflow_monthly_salary WHERE household_id=? AND month=? LIMIT 1",
                (member['household_id'], month)).fetchone():
            db.rollback(); return jsonify({'error': 'salary_breakdown_required'}), 409
        if existing:
            db.execute(
                "UPDATE family_cashflow_monthly_input SET salary_income=?,saving_transfers=?,"
                "investment_transfers=?,loan_principal_payments=?,revision=revision+1,updated_by=?,"
                "updated_at=datetime('now','+9 hours') WHERE household_id=? AND month=?",
                (values['salary_income'], values['saving_transfers'],
                 values['investment_transfers'], values['loan_principal_payments'],
                 session['user_id'], member['household_id'], month))
        else:
            db.execute(
                "INSERT INTO family_cashflow_monthly_input(household_id,month,salary_income,"
                "saving_transfers,investment_transfers,loan_principal_payments,updated_by) "
                "VALUES(?,?,?,?,?,?,?)", (member['household_id'], month, values['salary_income'],
                 values['saving_transfers'], values['investment_transfers'],
                 values['loan_principal_payments'], session['user_id']))
        if salary_items is not None:
            db.execute(
                "DELETE FROM family_cashflow_monthly_salary WHERE household_id=? AND month=?",
                (member['household_id'], month))
            db.executemany(
                "INSERT INTO family_cashflow_monthly_salary(household_id,month,member_user_id,"
                "amount,updated_by) VALUES(?,?,?,?,?)",
                [(member['household_id'], month, item['member_user_id'], item['amount'],
                  session['user_id']) for item in salary_items])
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))


@bp.post('/api/family-assets/cash-flow/close')
@login_required
def family_cashflow_close():
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    d = request.get_json(silent=True) or {}
    try:
        current = query("SELECT strftime('%Y-%m','now','+9 hours') month", one=True)['month']
        month = _editable_cashflow_month(d.get('month'), current)
        expected = d.get('expected_revision', 0)
        if isinstance(expected, bool) or int(expected) != expected or int(expected) < 0:
            raise ValueError
        expected = int(expected)
    except (ValueError, TypeError, OverflowError):
        return jsonify({'error': '입력 revision 오류'}), 400
    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        latest = db.execute(
            "SELECT COALESCE(MAX(revision),0) revision FROM family_cashflow_monthly_close "
            "WHERE household_id=? AND month=?", (member['household_id'], month)).fetchone()
        current_revision = int(latest['revision'])
        if current_revision != expected:
            db.rollback(); return jsonify({'error': 'revision_conflict'}), 409
        members = query("SELECT user_id id,display_name FROM family_asset_member "
                        "WHERE household_id=? ORDER BY joined_at,user_id", (member['household_id'],))
        if month != current and not db.execute(
                "SELECT 1 FROM family_cashflow_monthly_input WHERE household_id=? AND month=?",
                (member['household_id'], month)).fetchone():
            db.rollback(); return jsonify({'error': 'monthly_input_required'}), 409
        assets = query(
            "SELECT id,kind,amount,CASE WHEN monthly_flow_month=strftime('%Y-%m','now','+9 hours') "
            "THEN monthly_flow_amount ELSE 0 END monthly_flow_amount FROM family_asset_entry "
            "WHERE household_id=?", (member['household_id'],)) if month == current else []
        flow = _cashflow_snapshot(member['household_id'], members, assets, month)
        close_insert = db.execute(
            "INSERT INTO family_cashflow_monthly_close(household_id,month,revision,salary_income,"
            "ordinary_expenses,allowance_allocated,saving_transfers,investment_transfers,"
            "loan_principal_payments,allocated_income,unallocated_income,closed_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (member['household_id'], month,
             current_revision + 1, flow['salary_income'], flow['ordinary_expenses'],
             flow['allowance_allocated'], flow['saving_transfers'], flow['investment_transfers'],
             flow['loan_principal_payments'], flow['allocated_income'],
             flow['unallocated_income'], session['user_id']))
        if flow['salary_by_member']:
            db.executemany(
                "INSERT INTO family_cashflow_monthly_close_salary(close_id,member_user_id,"
                "member_name,amount) VALUES(?,?,?,?)",
                [(close_insert.lastrowid, item['member_user_id'], item['member_name'], item['amount'])
                 for item in flow['salary_by_member']])
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member())), 201


@bp.post('/api/family-assets/reconciliations')
@login_required
def family_asset_reconciliation_create():
    member = _member()
    if not member:
        return jsonify({'error': 'household_required'}), 409
    d = request.get_json(silent=True) or {}
    try:
        current_month = query("SELECT strftime('%Y-%m','now','+9 hours') month", one=True)['month']
        month = _clean_text(d.get('month') or current_month, '대조 월', required=True, limit=7)
        if month != current_month:
            raise ValueError('현재 월 실제잔액만 대조 가능')
        expected = d.get('expected_revision', 0)
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError('대조 revision 오류')
        raw_items = d.get('items')
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError('대조할 자산 필수')
        submitted = {}
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValueError('실제잔액 형식 오류')
            asset_id = raw.get('asset_id')
            asset_revision = raw.get('expected_asset_revision')
            if (isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id < 1 or
                    isinstance(asset_revision, bool) or not isinstance(asset_revision, int) or
                    asset_revision < 1):
                raise ValueError('대조 자산 revision 오류')
            if asset_id in submitted:
                raise ValueError('대조 자산 중복')
            submitted[asset_id] = {
                'actual_amount': _money(raw, 'actual_amount', '실제잔액', allow_zero=True),
                'asset_revision': asset_revision,
                'note': _clean_text(raw.get('note'), '차이 사유', limit=200),
            }
    except (ValueError, TypeError, OverflowError) as e:
        return jsonify({'error': str(e)}), 400

    db = get_db(); db.execute('BEGIN IMMEDIATE')
    try:
        assets = db.execute(
            "SELECT id,name,kind,amount,revision FROM family_asset_entry "
            "WHERE household_id=? AND kind!='income' ORDER BY id",
            (member['household_id'],)).fetchall()
        if {int(row['id']) for row in assets} != set(submitted):
            db.rollback(); return jsonify({'error': 'reconciliation_asset_set_changed'}), 409
        latest = db.execute(
            "SELECT COALESCE(MAX(revision),0) revision FROM family_asset_reconciliation "
            "WHERE household_id=? AND month=?", (member['household_id'], month)).fetchone()
        if int(latest['revision']) != expected:
            db.rollback(); return jsonify({'error': 'reconciliation_conflict'}), 409
        rows = []
        for asset in assets:
            item = submitted[int(asset['id'])]
            if int(asset['revision']) != item['asset_revision']:
                db.rollback(); return jsonify({'error': 'reconciliation_asset_changed'}), 409
            if int(asset['amount']) != item['actual_amount'] and not item['note']:
                db.rollback(); return jsonify({'error': 'reconciliation_note_required'}), 400
            rows.append((int(asset['id']), asset['name'], asset['kind'], int(asset['amount']),
                         item['actual_amount'], int(asset['revision']), item['note']))
        inserted = db.execute(
            "INSERT INTO family_asset_reconciliation(household_id,month,revision,reconciled_by) "
            "VALUES(?,?,?,?)", (member['household_id'], month, expected + 1, session['user_id']))
        db.executemany(
            "INSERT INTO family_asset_reconciliation_item(reconciliation_id,asset_id,asset_name,kind,"
            "book_amount,actual_amount,asset_revision,note) VALUES(?,?,?,?,?,?,?,?)",
            [(inserted.lastrowid, *row) for row in rows])
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member())), 201


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
    if member_user_id != session['user_id']:
        return jsonify({'error': 'private_expense_owner_required'}), 403
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
            "DELETE FROM family_allowance_expense WHERE id=? AND household_id=? AND budget_id IN "
            "(SELECT id FROM family_allowance_budget WHERE household_id=? AND member_user_id=?)",
            (expense_id, member['household_id'], member['household_id'], session['user_id']))
        if not cur.rowcount:
            db.rollback(); return jsonify({'error': 'not_found'}), 404
        db.commit()
    except Exception:
        db.rollback(); raise
    return jsonify(_snapshot(_member()))

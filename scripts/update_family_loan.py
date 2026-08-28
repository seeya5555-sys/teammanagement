#!/usr/bin/env python3
"""Maintain a family loan from actual lender payment notices.

Automatic amortization remains available for historical compatibility, but a
loan can be frozen into manual mode. Manual payments use the lender-provided
principal and interest without estimating either value.
"""

import argparse
import calendar
import datetime as dt
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


def _parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="instance/trmt.db")
    parser.add_argument("--username", required=True)
    parser.add_argument("--asset-id", type=int)
    sub = parser.add_subparsers(dest="action", required=True)
    configure = sub.add_parser("configure")
    configure.add_argument("--balance", type=int, required=True)
    configure.add_argument("--payment-amount", type=int, required=True)
    configure.add_argument("--annual-rate-bps", type=int, required=True)
    configure.add_argument("--due-day", type=int, required=True)
    configure.add_argument("--last-payment-date", required=True)
    configure.add_argument("--installment-no", type=int, required=True)
    apply_due = sub.add_parser("apply-due")
    apply_due.add_argument("--date", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--as-of-date", required=True)
    record = sub.add_parser("record-payment")
    record.add_argument("--date", required=True)
    record.add_argument("--installment-no", type=int, required=True)
    record.add_argument("--principal", type=int, required=True)
    record.add_argument("--interest", type=int, required=True)
    return parser


def _date(value):
    return dt.date.fromisoformat(value)


def _due_date(year, month, due_day):
    day = min(due_day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def _next_due(last_payment_date, due_day):
    if last_payment_date.month == 12:
        return _due_date(last_payment_date.year + 1, 1, due_day)
    return _due_date(last_payment_date.year, last_payment_date.month + 1, due_day)


def _member_and_loan(conn, username, asset_id):
    member = conn.execute(
        "SELECT m.household_id,m.user_id FROM family_asset_member m "
        "JOIN users u ON u.id=m.user_id WHERE lower(u.username)=lower(?)", (username,),
    ).fetchone()
    if not member:
        raise ValueError("family-asset member not found")
    if asset_id:
        loan = conn.execute(
            "SELECT id,name,amount,monthly_flow_amount,revision,monthly_flow_month FROM family_asset_entry "
            "WHERE id=? AND household_id=? AND kind='loan'", (asset_id, member[0]),
        ).fetchone()
    else:
        rows = conn.execute(
            "SELECT id,name,amount,monthly_flow_amount,revision,monthly_flow_month FROM family_asset_entry "
            "WHERE household_id=? AND kind='loan' ORDER BY id", (member[0],),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("exactly one loan required when asset-id is omitted")
        loan = rows[0]
    if not loan:
        raise ValueError("configured loan asset not found")
    return member, loan


def _snapshot(conn, household_id):
    assets = debt = 0
    for kind, amount in conn.execute(
            "SELECT kind,amount FROM family_asset_entry WHERE household_id=?", (household_id,)):
        if kind == "income":
            continue
        if kind == "loan":
            debt += int(amount)
        else:
            assets += int(amount)
    conn.execute(
        "INSERT INTO family_asset_monthly_snapshot(household_id,month,total_assets,total_debt,net_worth) "
        "VALUES(?,strftime('%Y-%m','now','+9 hours'),?,?,?) "
        "ON CONFLICT(household_id,month) DO UPDATE SET total_assets=excluded.total_assets,"
        "total_debt=excluded.total_debt,net_worth=excluded.net_worth,"
        "captured_at=datetime('now','+9 hours')", (household_id, assets, debt, assets - debt),
    )


def configure(conn, args):
    if not 0 <= args.annual_rate_bps <= 10_000:
        raise ValueError("annual-rate-bps outside guardrail")
    if not 1 <= args.due_day <= 31 or args.balance < 0 or args.payment_amount <= 0:
        raise ValueError("invalid loan configuration")
    last_payment = _date(args.last_payment_date)
    if args.installment_no < 0:
        raise ValueError("installment-no outside guardrail")
    conn.execute("BEGIN IMMEDIATE")
    try:
        member, loan = _member_and_loan(conn, args.username, args.asset_id)
        hid, uid = member
        latest = conn.execute(
            "SELECT due_date,installment_no,balance_after FROM family_asset_loan_payment "
            "WHERE asset_id=? ORDER BY due_date DESC LIMIT 1", (loan[0],),
        ).fetchone()
        if latest:
            latest_date = _date(latest[0])
            if last_payment < latest_date or args.installment_no < int(latest[1]):
                raise ValueError("configuration would rewind applied loan payments")
            if last_payment == latest_date and (args.installment_no != int(latest[1]) or
                                                args.balance != int(latest[2])):
                raise ValueError("configuration conflicts with latest applied payment")
        conn.execute(
            "UPDATE family_asset_entry SET amount=?,institution='한국주택금융공사',"
            "note=?,updated_by=?,revision=revision+1,updated_at=datetime('now','localtime') "
            "WHERE id=? AND household_id=?",
            (args.balance,
             f"보금자리론 자동상환 · 연 {args.annual_rate_bps / 100:.2f}% · "
             f"월 납입 {args.payment_amount:,}원 · 매월 {args.due_day}일(말일 보정)",
             uid, loan[0], hid),
        )
        conn.execute(
            "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
            "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
            "VALUES(?,?,'update',?,'loan',?,?,?,?,?)",
            (hid, loan[0], loan[1], loan[2], args.balance, loan[3], loan[3], uid),
        )
        conn.execute(
            "INSERT INTO family_asset_loan_schedule(asset_id,household_id,payment_amount,"
            "annual_rate_bps,due_day,installment_no,last_payment_date,active) VALUES(?,?,?,?,?,?,?,1) "
            "ON CONFLICT(asset_id) DO UPDATE SET household_id=excluded.household_id,"
            "payment_amount=excluded.payment_amount,annual_rate_bps=excluded.annual_rate_bps,"
            "due_day=excluded.due_day,installment_no=excluded.installment_no,"
            "last_payment_date=excluded.last_payment_date,active=1,"
            "updated_at=datetime('now','+9 hours')",
            (loan[0], hid, args.payment_amount, args.annual_rate_bps, args.due_day,
             args.installment_no, last_payment.isoformat()),
        )
        _snapshot(conn, hid)
        conn.commit()
        return {"ok": True, "action": "configured", "asset_id": loan[0], "balance": args.balance}
    except Exception:
        conn.rollback()
        raise


def apply_due(conn, args):
    run_date = _date(args.date)
    conn.execute("BEGIN IMMEDIATE")
    try:
        member, loan = _member_and_loan(conn, args.username, args.asset_id)
        hid, uid = member
        schedule = conn.execute(
            "SELECT payment_amount,annual_rate_bps,due_day,installment_no,last_payment_date,active "
            "FROM family_asset_loan_schedule WHERE asset_id=? AND household_id=?", (loan[0], hid),
        ).fetchone()
        if not schedule or not schedule[5]:
            raise ValueError("active loan schedule not found")
        last_payment = _date(schedule[4])
        due = _next_due(last_payment, schedule[2])
        if run_date < due:
            conn.rollback()
            return {"ok": True, "action": "skipped", "asset_id": loan[0], "next_due": due}
        balance = int(loan[2])
        prior_flow = int(loan[3])
        installment = int(schedule[3])
        applied_count = 0
        while due <= run_date and balance > 0:
            if applied_count >= 120:
                raise ValueError("more than 120 missed payments; manual reconciliation required")
            if conn.execute(
                    "SELECT 1 FROM family_asset_loan_payment WHERE asset_id=? AND due_date=?",
                    (loan[0], due.isoformat())).fetchone():
                raise ValueError("schedule cursor conflicts with an already applied payment")
            days = (due - last_payment).days
            # 주택금융공사 문자와 같은 원 단위 반올림(ROUND_HALF_UP), Actual/365.
            interest = int((Decimal(balance) * Decimal(schedule[1]) * Decimal(days) /
                            Decimal(10_000 * 365)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            total = min(int(schedule[0]), balance + interest)
            principal = total - interest
            if principal <= 0:
                raise ValueError("scheduled payment does not cover interest")
            after = balance - principal
            installment += 1
            conn.execute(
                "UPDATE family_asset_entry SET amount=?,monthly_flow_amount=?,"
                "monthly_flow_month=strftime('%Y-%m',?,'+9 hours'),updated_by=?,revision=revision+1,"
                "updated_at=datetime('now','localtime') WHERE id=? AND household_id=?",
                (after, principal, due.isoformat(), uid, loan[0], hid),
            )
            conn.execute(
                "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
                "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
                "VALUES(?,?,'update',?,'loan',?,?,?,?,?)",
                (hid, loan[0], loan[1], balance, after, prior_flow, total, uid),
            )
            conn.execute(
                "INSERT INTO family_asset_loan_payment(asset_id,household_id,installment_no,due_date,"
                "balance_before,principal,interest,total_payment,balance_after) VALUES(?,?,?,?,?,?,?,?,?)",
                (loan[0], hid, installment, due.isoformat(), balance, principal, interest, total, after),
            )
            conn.execute(
                "UPDATE family_asset_loan_schedule SET installment_no=?,last_payment_date=?,"
                "active=CASE WHEN ?=0 THEN 0 ELSE active END,updated_at=datetime('now','+9 hours') "
                "WHERE asset_id=?", (installment, due.isoformat(), after, loan[0]),
            )
            applied_count += 1
            balance, prior_flow, last_payment = after, total, due
            due = _next_due(last_payment, schedule[2])
        _snapshot(conn, hid)
        conn.commit()
        return {"ok": True, "action": "applied", "asset_id": loan[0],
                "installment_no": installment, "due_date": last_payment, "principal": principal,
                "interest": interest, "total": total, "balance": after,
                "applied_count": applied_count, "next_due": due}
    except Exception:
        conn.rollback()
        raise


def freeze(conn, args):
    """Disable estimated repayments while preserving the confirmed balance."""
    as_of = _date(args.as_of_date)
    conn.execute("BEGIN IMMEDIATE")
    try:
        member, loan = _member_and_loan(conn, args.username, args.asset_id)
        hid, uid = member
        conn.execute(
            "UPDATE family_asset_loan_schedule SET active=0,last_payment_date="
            "CASE WHEN last_payment_date < ? THEN ? ELSE last_payment_date END,"
            "updated_at=datetime('now','+9 hours') WHERE asset_id=? AND household_id=?",
            (as_of.isoformat(), as_of.isoformat(), loan[0], hid),
        )
        conn.execute(
            "UPDATE family_asset_entry SET institution='한국주택금융공사',note=?,updated_by=?,"
            "revision=revision+1,updated_at=datetime('now','localtime') "
            "WHERE id=? AND household_id=?",
            (f"보금자리론 체증식 · 실제 원금/이자 수동 반영 · "
             f"{as_of.isoformat()} 기준 잔액 {int(loan[2]):,}원", uid, loan[0], hid),
        )
        conn.execute(
            "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
            "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
            "VALUES(?,?,'update',?,'loan',?,?,?,?,?)",
            (hid, loan[0], loan[1], loan[2], loan[2], loan[3], loan[3], uid),
        )
        _snapshot(conn, hid)
        conn.commit()
        return {"ok": True, "action": "frozen", "asset_id": loan[0],
                "as_of_date": as_of, "balance": int(loan[2])}
    except Exception:
        conn.rollback()
        raise


def record_payment(conn, args):
    """Apply one lender-confirmed payment; only principal reduces the debt."""
    paid_on = _date(args.date)
    if args.installment_no <= 0 or args.principal <= 0 or args.interest < 0:
        raise ValueError("invalid manual payment")
    conn.execute("BEGIN IMMEDIATE")
    try:
        member, loan = _member_and_loan(conn, args.username, args.asset_id)
        hid, uid = member
        schedule = conn.execute(
            "SELECT installment_no,last_payment_date,active FROM family_asset_loan_schedule "
            "WHERE asset_id=? AND household_id=?", (loan[0], hid),
        ).fetchone()
        if not schedule:
            raise ValueError("loan schedule state not found")
        if schedule[2]:
            raise ValueError("automatic loan schedule is still active; freeze it first")
        existing = conn.execute(
            "SELECT installment_no,principal,interest,balance_after FROM family_asset_loan_payment "
            "WHERE asset_id=? AND due_date=?", (loan[0], paid_on.isoformat()),
        ).fetchone()
        if existing:
            expected_after = int(loan[2])
            if (int(existing[0]), int(existing[1]), int(existing[2]), int(existing[3])) == (
                    args.installment_no, args.principal, args.interest, expected_after):
                conn.rollback()
                return {"ok": True, "action": "skipped", "asset_id": loan[0],
                        "installment_no": args.installment_no, "balance": expected_after}
            raise ValueError("payment date already recorded with different values")
        if conn.execute(
                "SELECT 1 FROM family_asset_loan_payment WHERE asset_id=? AND installment_no=?",
                (loan[0], args.installment_no)).fetchone():
            raise ValueError("installment number already recorded")
        if paid_on <= _date(schedule[1]) or args.installment_no <= int(schedule[0]):
            raise ValueError("manual payment would rewind the loan ledger")
        balance = int(loan[2])
        if args.principal > balance:
            raise ValueError("principal exceeds current balance")
        after = balance - args.principal
        total = args.principal + args.interest
        prior_flow = int(loan[3])
        flow = (prior_flow + args.principal
                if loan[5] == paid_on.strftime("%Y-%m") else args.principal)
        conn.execute(
            "UPDATE family_asset_entry SET amount=?,monthly_flow_amount=?,"
            "monthly_flow_month=substr(?,1,7),updated_by=?,revision=revision+1,"
            "updated_at=datetime('now','localtime') WHERE id=? AND household_id=?",
            (after, flow, paid_on.isoformat(), uid, loan[0], hid),
        )
        conn.execute(
            "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
            "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
            "VALUES(?,?,'update',?,'loan',?,?,?,?,?)",
            (hid, loan[0], loan[1], balance, after, prior_flow, flow, uid),
        )
        payment_cur = conn.execute(
            "INSERT INTO family_asset_loan_payment(asset_id,household_id,installment_no,due_date,"
            "balance_before,principal,interest,total_payment,balance_after) VALUES(?,?,?,?,?,?,?,?,?)",
            (loan[0], hid, args.installment_no, paid_on.isoformat(), balance,
             args.principal, args.interest, total, after),
        )
        if args.interest > 0:
            conn.execute(
                "INSERT INTO family_cash_expense(household_id,category,name,amount,spent_on,"
                "source_type,source_id,created_by) VALUES(?, 'home_loan_interest', ?, ?, ?, ?, ?, ?)",
                (hid, f"{loan[1]} {args.installment_no}회차 이자", args.interest,
                 paid_on.isoformat(), 'loan_payment', payment_cur.lastrowid, uid),
            )
        conn.execute(
            "UPDATE family_asset_loan_schedule SET installment_no=?,last_payment_date=?,active=0,"
            "updated_at=datetime('now','+9 hours') WHERE asset_id=?",
            (args.installment_no, paid_on.isoformat(), loan[0]),
        )
        _snapshot(conn, hid)
        conn.commit()
        return {"ok": True, "action": "recorded", "asset_id": loan[0],
                "installment_no": args.installment_no, "date": paid_on,
                "principal": args.principal, "interest": args.interest,
                "total": total, "balance": after}
    except Exception:
        conn.rollback()
        raise


def main(argv=None):
    args = _parser().parse_args(argv)
    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(f"database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        if args.action == "configure":
            result = configure(conn, args)
        elif args.action == "apply-due":
            result = apply_due(conn, args)
        elif args.action == "freeze":
            result = freeze(conn, args)
        else:
            result = record_payment(conn, args)
    finally:
        conn.close()
    print(" ".join(f"{key}={value}" for key, value in result.items()))


if __name__ == "__main__":
    main()

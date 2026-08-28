#!/usr/bin/env python3
"""Apply one verified property valuation to the family-asset ledger.

The market-data lookup intentionally lives outside this script.  The caller must
collect and validate listing prices, then pass only the resulting amount and
audit metadata.  This keeps Naver page changes away from the accounting write.
"""

import argparse
import datetime as dt
import sqlite3
from pathlib import Path


MIN_PROPERTY_AMOUNT = 10_000_000
MAX_PROPERTY_AMOUNT = 10_000_000_000
MAX_SAMPLE_COUNT = 500


def _parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="instance/trmt.db")
    parser.add_argument("--username", required=True)
    parser.add_argument("--asset-id", type=int)
    parser.add_argument("--amount", type=int, required=True)
    parser.add_argument("--sample-count", type=int, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--complex-no", required=True)
    parser.add_argument("--complex-name", required=True)
    parser.add_argument("--building-name", required=True)
    parser.add_argument("--supply-area", required=True)
    parser.add_argument("--exclusive-area", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--allow-large-change", action="store_true")
    return parser


def _clean(value, field, limit=200):
    value = str(value).strip()
    if not value or len(value) > limit or any(ch in value for ch in "\r\n\x00"):
        raise ValueError(f"invalid {field}")
    return value


def _validate(args):
    if not MIN_PROPERTY_AMOUNT <= args.amount <= MAX_PROPERTY_AMOUNT:
        raise ValueError("amount outside property guardrail")
    if not 1 <= args.sample_count <= MAX_SAMPLE_COUNT:
        raise ValueError("sample-count outside guardrail")
    dt.date.fromisoformat(args.as_of)
    for field in ("username", "complex_no", "complex_name", "building_name",
                  "supply_area", "exclusive_area", "source_url"):
        setattr(args, field.replace("-", "_"), _clean(getattr(args, field.replace("-", "_")), field))
    if not args.source_url.startswith("https://new.land.naver.com/"):
        raise ValueError("source-url must be Naver Land HTTPS")


def _totals(conn, household_id):
    assets = debt = 0
    for kind, amount in conn.execute(
            "SELECT kind,amount FROM family_asset_entry WHERE household_id=?",
            (household_id,)):
        if kind == "income":
            continue
        if kind == "loan":
            debt += int(amount)
        else:
            assets += int(amount)
    return assets, debt, assets - debt


def apply_valuation(conn, args):
    _validate(args)
    conn.execute("BEGIN IMMEDIATE")
    try:
        member = conn.execute(
            "SELECT m.household_id,m.user_id FROM family_asset_member m "
            "JOIN users u ON u.id=m.user_id WHERE lower(u.username)=lower(?)",
            (args.username,),
        ).fetchone()
        if not member:
            raise ValueError("family-asset member not found")
        household_id, user_id = member
        existing = None
        if args.asset_id:
            existing = conn.execute(
                "SELECT id,kind,name,amount,monthly_flow_amount,revision "
                "FROM family_asset_entry WHERE id=? AND household_id=? AND kind='property'",
                (args.asset_id, household_id),
            ).fetchone()
            if not existing:
                raise ValueError("configured property asset not found")
        else:
            rows = conn.execute(
                "SELECT id,kind,name,amount,monthly_flow_amount,revision "
                "FROM family_asset_entry WHERE household_id=? AND kind='property' ORDER BY id",
                (household_id,),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("multiple property assets; asset-id required")
            existing = rows[0] if rows else None

        name = f"{args.complex_name} {args.building_name}동"
        note = (f"네이버 매물 평균 자동갱신 · 공급 {args.supply_area}㎡ / "
                f"전용 {args.exclusive_area}㎡ · 단지 {args.complex_no} · "
                f"표본 {args.sample_count}건 · 기준일 {args.as_of} · {args.source_url}")
        if existing:
            previous_amount = int(existing[3])
            if (previous_amount > 0 and not args.allow_large_change and
                    abs(args.amount - previous_amount) / previous_amount > 0.20):
                raise ValueError("valuation changed by more than 20%; manual confirmation required")
            asset_id = existing[0]
            conn.execute(
                "UPDATE family_asset_entry SET name=?,amount=?,institution='네이버 부동산',"
                "note=?,updated_by=?,revision=revision+1,"
                "updated_at=datetime('now','localtime') WHERE id=? AND household_id=?",
                (name, args.amount, note, user_id, asset_id, household_id),
            )
            conn.execute(
                "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
                "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
                "VALUES(?,?,'update',?,'property',?,?,?,?,?)",
                (household_id, asset_id, name, existing[3], args.amount,
                 existing[4], existing[4], user_id),
            )
            action = "updated"
        else:
            asset_id = conn.execute(
                "INSERT INTO family_asset_entry(household_id,kind,name,amount,owner_mode,"
                "owner_user_id,joint_share,institution,note,monthly_flow_amount,created_by,updated_by) "
                "VALUES(?,'property',?,?,'member',?,50,'네이버 부동산',?,0,?,?)",
                (household_id, name, args.amount, user_id, note, user_id, user_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO family_asset_history(household_id,asset_id,action,asset_name,kind,"
                "amount_before,amount_after,monthly_flow_before,monthly_flow_after,changed_by) "
                "VALUES(?,?,'create',?,'property',NULL,?,NULL,0,?)",
                (household_id, asset_id, name, args.amount, user_id),
            )
            action = "created"

        total_assets, total_debt, net_worth = _totals(conn, household_id)
        conn.execute(
            "INSERT INTO family_asset_monthly_snapshot(household_id,month,total_assets,total_debt,net_worth) "
            "VALUES(?,strftime('%Y-%m','now','+9 hours'),?,?,?) "
            "ON CONFLICT(household_id,month) DO UPDATE SET total_assets=excluded.total_assets,"
            "total_debt=excluded.total_debt,net_worth=excluded.net_worth,"
            "captured_at=datetime('now','+9 hours')",
            (household_id, total_assets, total_debt, net_worth),
        )
        conn.commit()
        return {"ok": True, "action": action, "asset_id": asset_id,
                "amount": args.amount, "sample_count": args.sample_count}
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
        result = apply_valuation(conn, args)
    finally:
        conn.close()
    print(" ".join(f"{key}={value}" for key, value in result.items()))


if __name__ == "__main__":
    main()

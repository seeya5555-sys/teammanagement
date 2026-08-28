import argparse
import sqlite3
import unittest
from pathlib import Path

from scripts.update_family_loan import apply_due, configure, freeze, record_payment


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(Path("schema.sql").read_text(encoding="utf-8"))
    uid = conn.execute(
        "INSERT INTO users(username,password_hash,display_name,role) VALUES('ysson','x','유석','admin')"
    ).lastrowid
    hid = conn.execute(
        "INSERT INTO family_asset_household(name,invite_code,created_by) VALUES('우리집','ABCDEFGH',?)",
        (uid,),
    ).lastrowid
    conn.execute(
        "INSERT INTO family_asset_member(household_id,user_id,display_name,role) VALUES(?,?,?,'owner')",
        (hid, uid, "유석"),
    )
    aid = conn.execute(
        "INSERT INTO family_asset_entry(household_id,kind,name,amount,owner_mode,owner_user_id,"
        "created_by,updated_by) VALUES(?,'loan','보금자리론',300000000,'member',?,?,?)",
        (hid, uid, uid, uid),
    ).lastrowid
    conn.commit()
    return conn, aid


class FamilyLoanUpdateTests(unittest.TestCase):
    def test_freeze_preserves_balance_and_manual_payment_uses_actual_values(self):
        conn, aid = _db()
        configure(conn, argparse.Namespace(
            username="ysson", asset_id=aid, balance=292_649_627, payment_amount=902_257,
            annual_rate_bps=273, due_day=31, last_payment_date="2026-07-31", installment_no=61,
        ))
        frozen = freeze(conn, argparse.Namespace(
            username="ysson", asset_id=aid, as_of_date="2026-08-24",
        ))
        self.assertEqual(292_649_627, frozen["balance"])
        self.assertEqual(0, conn.execute(
            "SELECT active FROM family_asset_loan_schedule WHERE asset_id=?", (aid,)
        ).fetchone()[0])
        with self.assertRaisesRegex(ValueError, "active loan schedule"):
            apply_due(conn, argparse.Namespace(username="ysson", asset_id=aid, date="2026-08-31"))
        recorded = record_payment(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-31", installment_no=62,
            principal=223_711, interest=678_546,
        ))
        self.assertEqual(292_425_916, recorded["balance"])
        self.assertEqual(902_257, recorded["total"])
        self.assertEqual((292_425_916, 902_257, "2026-08"), conn.execute(
            "SELECT amount,monthly_flow_amount,monthly_flow_month "
            "FROM family_asset_entry WHERE id=?", (aid,),
        ).fetchone())
        self.assertEqual(("home_loan_interest", 678_546, "2026-08-31"), conn.execute(
            "SELECT category,amount,spent_on FROM family_cash_expense WHERE household_id=1"
        ).fetchone())
        with self.assertRaisesRegex(ValueError, "rewind"):
            record_payment(conn, argparse.Namespace(
                username="ysson", asset_id=aid, date="2026-08-23", installment_no=63,
                principal=1, interest=0,
            ))
        conn.close()

    def test_manual_payment_rejects_rewind_and_is_idempotent(self):
        conn, aid = _db()
        configure(conn, argparse.Namespace(
            username="ysson", asset_id=aid, balance=292_649_627, payment_amount=902_257,
            annual_rate_bps=273, due_day=31, last_payment_date="2026-07-31", installment_no=61,
        ))
        freeze(conn, argparse.Namespace(
            username="ysson", asset_id=aid, as_of_date="2026-08-24",
        ))
        payment = argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-31", installment_no=62,
            principal=223_711, interest=678_546,
        )
        self.assertEqual("recorded", record_payment(conn, payment)["action"])
        self.assertEqual("skipped", record_payment(conn, payment)["action"])
        with self.assertRaisesRegex(ValueError, "rewind"):
            record_payment(conn, argparse.Namespace(
                username="ysson", asset_id=aid, date="2026-08-30", installment_no=61,
                principal=1, interest=1,
            ))
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM family_asset_loan_payment WHERE asset_id=?", (aid,)
        ).fetchone()[0])
        conn.close()

    def test_manual_payment_guards_and_same_month_extra_payment_accumulates_flow(self):
        conn, aid = _db()
        configure(conn, argparse.Namespace(
            username="ysson", asset_id=aid, balance=1_000_000, payment_amount=100_000,
            annual_rate_bps=273, due_day=31, last_payment_date="2026-07-31", installment_no=61,
        ))
        with self.assertRaisesRegex(ValueError, "freeze it first"):
            record_payment(conn, argparse.Namespace(
                username="ysson", asset_id=aid, date="2026-08-31", installment_no=62,
                principal=10_000, interest=1_000,
            ))
        freeze(conn, argparse.Namespace(
            username="ysson", asset_id=aid, as_of_date="2026-08-24",
        ))
        record_payment(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-31", installment_no=62,
            principal=10_000, interest=1_000,
        ))
        record_payment(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-09-01", installment_no=63,
            principal=5_000, interest=500,
        ))
        record_payment(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-09-15", installment_no=64,
            principal=2_000, interest=200,
        ))
        self.assertEqual((983_000, 7_700, "2026-09"), conn.execute(
            "SELECT amount,monthly_flow_amount,monthly_flow_month "
            "FROM family_asset_entry WHERE id=?", (aid,),
        ).fetchone())
        with self.assertRaisesRegex(ValueError, "exceeds current balance"):
            record_payment(conn, argparse.Namespace(
                username="ysson", asset_id=aid, date="2026-10-01", installment_no=65,
                principal=1_000_000, interest=0,
            ))
        conn.close()

    def test_hf_notice_calibrates_and_applies_exact_august_payment(self):
        conn, aid = _db()
        configured = configure(conn, argparse.Namespace(
            username="ysson", asset_id=aid, balance=292_649_627, payment_amount=902_257,
            annual_rate_bps=273, due_day=31, last_payment_date="2026-07-31", installment_no=61,
        ))
        self.assertEqual(292_649_627, configured["balance"])
        applied = apply_due(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-31",
        ))
        self.assertEqual(62, applied["installment_no"])
        self.assertEqual(678_546, applied["interest"])
        self.assertEqual(223_711, applied["principal"])
        self.assertEqual(292_425_916, applied["balance"])
        row = conn.execute(
            "SELECT amount,monthly_flow_amount,monthly_flow_month FROM family_asset_entry WHERE id=?", (aid,)
        ).fetchone()
        self.assertEqual((292_425_916, 902_257, "2026-08"), row)
        conn.close()

    def test_month_end_and_idempotent_skip(self):
        conn, aid = _db()
        configure(conn, argparse.Namespace(
            username="ysson", asset_id=aid, balance=292_649_627, payment_amount=902_257,
            annual_rate_bps=273, due_day=31, last_payment_date="2026-07-31", installment_no=61,
        ))
        self.assertEqual("skipped", apply_due(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-30"))["action"])
        self.assertEqual("applied", apply_due(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-31"))["action"])
        self.assertEqual("skipped", apply_due(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-08-31"))["action"])
        september = apply_due(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-09-30"))
        self.assertEqual("applied", september["action"])
        self.assertEqual("2026-09-30", september["due_date"].isoformat())
        conn.close()

    def test_multiple_missed_months_catch_up_atomically(self):
        conn, aid = _db()
        configure(conn, argparse.Namespace(
            username="ysson", asset_id=aid, balance=292_649_627, payment_amount=902_257,
            annual_rate_bps=273, due_day=31, last_payment_date="2026-07-31", installment_no=61,
        ))
        caught_up = apply_due(conn, argparse.Namespace(
            username="ysson", asset_id=aid, date="2026-10-31"))
        self.assertEqual(3, caught_up["applied_count"])
        self.assertEqual(64, caught_up["installment_no"])
        self.assertEqual("2026-10-31", caught_up["due_date"].isoformat())
        self.assertEqual(3, conn.execute(
            "SELECT COUNT(*) FROM family_asset_loan_payment WHERE asset_id=?", (aid,)
        ).fetchone()[0])
        conn.close()

    def test_reconfigure_cannot_rewind_applied_payment(self):
        conn, aid = _db()
        base = dict(username="ysson", asset_id=aid, balance=292_649_627,
                    payment_amount=902_257, annual_rate_bps=273, due_day=31,
                    last_payment_date="2026-07-31", installment_no=61)
        configure(conn, argparse.Namespace(**base))
        apply_due(conn, argparse.Namespace(username="ysson", asset_id=aid, date="2026-08-31"))
        with self.assertRaisesRegex(ValueError, "rewind"):
            configure(conn, argparse.Namespace(**base))
        self.assertEqual(292_425_916, conn.execute(
            "SELECT amount FROM family_asset_entry WHERE id=?", (aid,)
        ).fetchone()[0])
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

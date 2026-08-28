import argparse
import sqlite3
import unittest

from scripts.update_family_property import apply_valuation


SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE family_asset_member(household_id INTEGER,user_id INTEGER);
CREATE TABLE family_asset_entry(
 id INTEGER PRIMARY KEY AUTOINCREMENT,household_id INTEGER,kind TEXT,name TEXT,amount INTEGER,
 owner_mode TEXT,owner_user_id INTEGER,joint_share INTEGER,institution TEXT,note TEXT,
 monthly_flow_amount INTEGER DEFAULT 0,revision INTEGER DEFAULT 1,created_by INTEGER,updated_by INTEGER,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE family_asset_history(
 id INTEGER PRIMARY KEY,household_id INTEGER,asset_id INTEGER,action TEXT,asset_name TEXT,kind TEXT,
 amount_before INTEGER,amount_after INTEGER,monthly_flow_before INTEGER,monthly_flow_after INTEGER,
 changed_by INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE family_asset_monthly_snapshot(
 household_id INTEGER,month TEXT,total_assets INTEGER,total_debt INTEGER,net_worth INTEGER,
 captured_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(household_id,month));
"""


def args(**changes):
    values = dict(username="SS0094", asset_id=None, amount=550_000_000, sample_count=3,
                  as_of="2026-08-28", complex_no="104629", complex_name="래미안부평",
                  building_name="105", supply_area="79.72", exclusive_area="59.92",
                  source_url="https://new.land.naver.com/complexes/104629",
                  allow_large_change=False)
    values.update(changes)
    return argparse.Namespace(**values)


class PropertyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO users VALUES(5,'SS0094')")
        self.db.execute("INSERT INTO family_asset_member VALUES(1,5)")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_create_then_update_keeps_one_asset_and_audit(self):
        created = apply_valuation(self.db, args())
        updated = apply_valuation(self.db, args(asset_id=created["asset_id"], amount=560_000_000,
                                                  sample_count=4, as_of="2026-09-05"))
        self.assertEqual("created", created["action"])
        self.assertEqual("updated", updated["action"])
        self.assertEqual((1, 560_000_000, 2), self.db.execute(
            "SELECT COUNT(*),amount,revision FROM family_asset_entry").fetchone())
        self.assertEqual([("create", None, 550_000_000), ("update", 550_000_000, 560_000_000)],
                         self.db.execute("SELECT action,amount_before,amount_after "
                                         "FROM family_asset_history ORDER BY id").fetchall())
        self.assertEqual((560_000_000, 0, 560_000_000), self.db.execute(
            "SELECT total_assets,total_debt,net_worth FROM family_asset_monthly_snapshot").fetchone())

    def test_wrong_asset_or_bad_market_input_rolls_back(self):
        with self.assertRaisesRegex(ValueError, "configured property asset"):
            apply_valuation(self.db, args(asset_id=99))
        with self.assertRaisesRegex(ValueError, "guardrail"):
            apply_valuation(self.db, args(amount=1, sample_count=0))
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM family_asset_entry").fetchone()[0])

    def test_multiple_properties_requires_explicit_id(self):
        first = apply_valuation(self.db, args())["asset_id"]
        self.db.execute(
            "INSERT INTO family_asset_entry(household_id,kind,name,amount,owner_mode,owner_user_id,"
            "joint_share,institution,note,created_by,updated_by) "
            "VALUES(1,'property','다른 집',1,'member',5,50,'','',5,5)")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "asset-id required"):
            apply_valuation(self.db, args())
        self.assertEqual(first, self.db.execute("SELECT MIN(id) FROM family_asset_entry").fetchone()[0])

    def test_large_weekly_change_requires_manual_override(self):
        asset_id = apply_valuation(self.db, args())["asset_id"]
        with self.assertRaisesRegex(ValueError, "more than 20%"):
            apply_valuation(self.db, args(asset_id=asset_id, amount=800_000_000))
        self.assertEqual(550_000_000, self.db.execute(
            "SELECT amount FROM family_asset_entry WHERE id=?", (asset_id,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()

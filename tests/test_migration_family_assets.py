"""우리자산 legacy migration order, isolation, and data-preservation gates."""

import sqlite3
import unittest

import migration_steps


LEGACY_SCHEMA = """
CREATE TABLE family_asset_entry (
    id INTEGER PRIMARY KEY,
    amount INTEGER NOT NULL
);
CREATE TABLE family_asset_history (
    id INTEGER PRIMARY KEY,
    amount_before INTEGER,
    amount_after INTEGER
);
"""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class FamilyAssetMigrationTests(unittest.TestCase):
    def _legacy_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO family_asset_entry(id,amount) VALUES(1,500)")
        conn.execute("INSERT INTO family_asset_history(id,amount_before,amount_after) VALUES(2,100,500)")
        conn.commit()
        return conn

    def test_step_order_is_frozen(self):
        self.assertEqual(
            ("family_asset_entry.columns", "family_asset_history.columns", "family_asset_loan.tables",
             "family_cashflow.tables"),
            tuple(name for name, _step in migration_steps.FAMILY_ASSET_MIGRATIONS),
        )

    def test_upgrade_is_idempotent_and_preserves_rows(self):
        conn = self._legacy_connection()
        try:
            migration_steps.run_family_asset_migrations(conn)
            migration_steps.run_family_asset_migrations(conn)
            self.assertTrue({"revision", "monthly_flow_amount", "monthly_flow_month",
                             "evidence_image", "evidence_mime", "evidence_captured_at"}
                            <= _columns(conn, "family_asset_entry"))
            self.assertTrue({"monthly_flow_before", "monthly_flow_after"}
                            <= _columns(conn, "family_asset_history"))
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='family_asset_loan_schedule'"
            ).fetchone())
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='family_asset_loan_payment'"
            ).fetchone())
            for table in ("family_cash_expense", "family_allowance_budget", "family_allowance_expense",
                          "family_cashflow_monthly_input", "family_cashflow_monthly_close"):
                self.assertTrue(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone())
            self.assertEqual((1, 500, 1, 0, None, None, None, None), conn.execute(
                "SELECT id,amount,revision,monthly_flow_amount,monthly_flow_month,"
                "evidence_image,evidence_mime,evidence_captured_at "
                "FROM family_asset_entry").fetchone())
            self.assertEqual((2, 100, 500, None, None), conn.execute(
                "SELECT id,amount_before,amount_after,monthly_flow_before,monthly_flow_after "
                "FROM family_asset_history").fetchone())
        finally:
            conn.close()

    def test_entry_probe_failure_does_not_skip_history(self):
        conn = self._legacy_connection()

        class EntryPragmaFailure:
            def execute(self, sql, params=()):
                if "table_info(family_asset_entry)" in sql:
                    raise sqlite3.OperationalError("entry probe failed")
                return conn.execute(sql, params)

        try:
            migration_steps.run_family_asset_migrations(EntryPragmaFailure())
            self.assertNotIn("revision", _columns(conn, "family_asset_entry"))
            self.assertIn("monthly_flow_before", _columns(conn, "family_asset_history"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Foundation migration order, idempotency, and data-preservation gates."""

import sqlite3
import unittest

import migration_steps


LEGACY_SCHEMA = """
CREATE TABLE calendar_events (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, start_date TEXT NOT NULL
);
CREATE TABLE push_log (
    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL
);
CREATE TABLE automation_run (
    id INTEGER PRIMARY KEY, slug TEXT NOT NULL
);
"""


def _columns(conn, table):
    return tuple(row[1] for row in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall())


def _snapshot(conn):
    schema = tuple(conn.execute("""
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE name NOT LIKE 'sqlite_%'
         ORDER BY type, name
    """).fetchall())
    rows = (
        tuple(conn.execute("SELECT id,title,start_date,completed FROM calendar_events")),
        tuple(conn.execute("SELECT id,event_key,hidden_at FROM push_log")),
        tuple(conn.execute("SELECT id,slug,progress FROM automation_run")),
    )
    return schema, rows


class FoundationMigrationTests(unittest.TestCase):
    def _legacy_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO calendar_events (id,title,start_date) VALUES (1,'Call','2026-08-24')"
        )
        conn.execute("INSERT INTO push_log (id,event_key) VALUES (2,'push:2')")
        conn.execute("INSERT INTO automation_run (id,slug) VALUES (3,'runner')")
        conn.commit()
        return conn

    def test_step_order_is_frozen(self):
        self.assertEqual(
            (
                "calendar_events.completed",
                "push_log.hidden_at",
                "automation_run.progress",
            ),
            tuple(name for name, _step in migration_steps.FOUNDATION_MIGRATIONS),
        )

    def test_legacy_upgrade_is_idempotent_and_preserves_rows(self):
        conn = self._legacy_connection()
        try:
            migration_steps.run_foundation_migrations(conn)
            conn.commit()
            first = _snapshot(conn)
            migration_steps.run_foundation_migrations(conn)
            conn.commit()
            second = _snapshot(conn)

            self.assertEqual(first, second)
            self.assertIn("completed", _columns(conn, "calendar_events"))
            self.assertIn("hidden_at", _columns(conn, "push_log"))
            self.assertIn("progress", _columns(conn, "automation_run"))
            self.assertEqual((1, "Call", "2026-08-24", 0), first[1][0][0])
            self.assertEqual((2, "push:2", None), first[1][1][0])
            self.assertEqual((3, "runner", None), first[1][2][0])
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                    "AND name='idx_push_log_visible'"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_one_step_failure_does_not_skip_later_steps(self):
        conn = self._legacy_connection()

        class CalendarPragmaFailure:
            def execute(self, sql, params=()):
                if "table_info(calendar_events)" in sql:
                    raise sqlite3.OperationalError("calendar probe failed")
                return conn.execute(sql, params)

        try:
            migration_steps.run_foundation_migrations(CalendarPragmaFailure())
            self.assertNotIn("completed", _columns(conn, "calendar_events"))
            self.assertIn("hidden_at", _columns(conn, "push_log"))
            self.assertIn("progress", _columns(conn, "automation_run"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

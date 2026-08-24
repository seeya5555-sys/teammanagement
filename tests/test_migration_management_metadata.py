"""Management metadata migration order and replay contracts."""

import sqlite3
import unittest

import migration_steps


LEGACY_SCHEMA = """
CREATE TABLE class_status (id INTEGER PRIMARY KEY, vessel_id INTEGER);
CREATE TABLE class_status_items (id INTEGER PRIMARY KEY, description TEXT);
CREATE TABLE vessels (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE mail_card (id INTEGER PRIMARY KEY, card_status TEXT);
"""


def _columns(conn, table):
    return tuple(row[1] for row in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall())


class ManagementMetadataMigrationTests(unittest.TestCase):
    def _legacy_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO class_status VALUES (1, 10)")
        conn.execute("INSERT INTO class_status_items VALUES (2, 'open')")
        conn.execute("INSERT INTO vessels VALUES (3, 'Vessel')")
        conn.execute("INSERT INTO mail_card VALUES (4, 'open')")
        conn.commit()
        return conn

    def test_order_is_frozen(self):
        self.assertEqual(
            (
                "class_status.source_path",
                "class_status_items.action_taken",
                "vessels.management",
                "mail_card.columns",
            ),
            tuple(name for name, _step in migration_steps.MANAGEMENT_METADATA_MIGRATIONS),
        )

    def test_upgrade_is_idempotent_and_preserves_rows(self):
        conn = self._legacy_connection()
        try:
            migration_steps.run_management_metadata_migrations(conn)
            conn.commit()
            first_schema = tuple(conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ))
            migration_steps.run_management_metadata_migrations(conn)
            conn.commit()
            second_schema = tuple(conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ))

            self.assertEqual(first_schema, second_schema)
            self.assertIn("source_path", _columns(conn, "class_status"))
            self.assertIn("action_taken", _columns(conn, "class_status_items"))
            self.assertIn("manager", _columns(conn, "vessels"))
            self.assertIn("manager_supervisor", _columns(conn, "vessels"))
            self.assertIn("card_category", _columns(conn, "mail_card"))
            self.assertEqual((1, 10, None), conn.execute("SELECT * FROM class_status").fetchone())
            self.assertEqual("", conn.execute(
                "SELECT action_taken FROM class_status_items WHERE id=2"
            ).fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                "AND name='idx_mail_card_thread'"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_first_step_failure_does_not_skip_later_steps(self):
        conn = self._legacy_connection()

        class FirstPragmaFailure:
            def execute(self, sql, params=()):
                if "table_info(class_status)" in sql:
                    raise sqlite3.OperationalError("probe failed")
                return conn.execute(sql, params)

        try:
            migration_steps.run_management_metadata_migrations(FirstPragmaFailure())
            self.assertNotIn("source_path", _columns(conn, "class_status"))
            self.assertIn("action_taken", _columns(conn, "class_status_items"))
            self.assertIn("manager", _columns(conn, "vessels"))
            self.assertIn("pending", _columns(conn, "mail_card"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

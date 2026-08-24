"""Small, ordered, idempotent migration steps used during application boot.

Each step owns its own failure boundary: one legacy table that cannot be
inspected or altered must not prevent later independent repairs from running.
The order tuple is an executable contract and is covered by regression tests.
"""


def _calendar_completed(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(calendar_events)"
        ).fetchall()]
        if columns and "completed" not in columns:
            conn.execute(
                "ALTER TABLE calendar_events ADD COLUMN "
                "completed INTEGER NOT NULL DEFAULT 0"
            )
            print("[auto_migrate] calendar_events.completed 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] calendar_events.completed 점검 건너뜀: {exc}")


def _push_log_hidden(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(push_log)"
        ).fetchall()]
        if columns and "hidden_at" not in columns:
            conn.execute("ALTER TABLE push_log ADD COLUMN hidden_at TEXT")
            print("[auto_migrate] push_log.hidden_at 추가됨")
        if columns:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_log_visible "
                "ON push_log(id DESC) WHERE hidden_at IS NULL"
            )
    except Exception as exc:
        print(f"[auto_migrate] push_log.hidden_at 점검 건너뜀: {exc}")


def _automation_progress(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(automation_run)"
        ).fetchall()]
        if columns and "progress" not in columns:
            conn.execute("ALTER TABLE automation_run ADD COLUMN progress TEXT")
            print("[auto_migrate] automation_run.progress 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] automation_run.progress 점검 건너뜀: {exc}")


FOUNDATION_MIGRATIONS = (
    ("calendar_events.completed", _calendar_completed),
    ("push_log.hidden_at", _push_log_hidden),
    ("automation_run.progress", _automation_progress),
)


def run_foundation_migrations(conn):
    for _name, migrate in FOUNDATION_MIGRATIONS:
        migrate(conn)

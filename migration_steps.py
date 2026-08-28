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


def _family_asset_entry_columns(conn):
    try:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(family_asset_entry)"
        ).fetchall()}
        if columns and "revision" not in columns:
            conn.execute(
                "ALTER TABLE family_asset_entry ADD COLUMN "
                "revision INTEGER NOT NULL DEFAULT 1"
            )
            print("[auto_migrate] family_asset_entry.revision 추가됨")
        if columns and "monthly_flow_amount" not in columns:
            conn.execute(
                "ALTER TABLE family_asset_entry ADD COLUMN monthly_flow_amount "
                "INTEGER NOT NULL DEFAULT 0 CHECK(monthly_flow_amount >= 0)"
            )
            print("[auto_migrate] family_asset_entry.monthly_flow_amount 추가됨")
        if columns and "monthly_flow_month" not in columns:
            conn.execute(
                "ALTER TABLE family_asset_entry ADD COLUMN monthly_flow_month TEXT"
            )
            print("[auto_migrate] family_asset_entry.monthly_flow_month 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] family_asset_entry 컬럼 점검 건너뜀: {exc}")


def _family_asset_history_columns(conn):
    try:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(family_asset_history)"
        ).fetchall()}
        if columns and "monthly_flow_before" not in columns:
            conn.execute(
                "ALTER TABLE family_asset_history ADD COLUMN monthly_flow_before INTEGER"
            )
            print("[auto_migrate] family_asset_history.monthly_flow_before 추가됨")
        if columns and "monthly_flow_after" not in columns:
            conn.execute(
                "ALTER TABLE family_asset_history ADD COLUMN monthly_flow_after INTEGER"
            )
            print("[auto_migrate] family_asset_history.monthly_flow_after 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] family_asset_history 컬럼 점검 건너뜀: {exc}")


FAMILY_ASSET_MIGRATIONS = (
    ("family_asset_entry.columns", _family_asset_entry_columns),
    ("family_asset_history.columns", _family_asset_history_columns),
)


def run_family_asset_migrations(conn):
    """Keep legacy 우리자산 tables additive without duplicating boot logic."""
    for _name, migrate in FAMILY_ASSET_MIGRATIONS:
        migrate(conn)


def _class_status_source_path(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(class_status)"
        ).fetchall()]
        if columns and "source_path" not in columns:
            conn.execute("ALTER TABLE class_status ADD COLUMN source_path TEXT")
            print("[auto_migrate] class_status.source_path 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] class_status.source_path 점검 건너뜀: {exc}")


def _class_status_action_taken(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(class_status_items)"
        ).fetchall()]
        if columns and "action_taken" not in columns:
            conn.execute(
                "ALTER TABLE class_status_items ADD COLUMN "
                "action_taken TEXT NOT NULL DEFAULT ''"
            )
            print("[auto_migrate] class_status_items.action_taken 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] class_status_items.action_taken 점검 건너뜀: {exc}")


def _vessel_management_columns(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(vessels)"
        ).fetchall()]
        if columns and "manager" not in columns:
            conn.execute("ALTER TABLE vessels ADD COLUMN manager TEXT")
            print("[auto_migrate] vessels.manager 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] vessels.manager 점검 건너뜀: {exc}")

    # Keep the historical independent failure boundary: a failed manager ALTER
    # must not prevent manager_supervisor from being inspected.
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(vessels)"
        ).fetchall()]
        if columns and "manager_supervisor" not in columns:
            conn.execute(
                "ALTER TABLE vessels ADD COLUMN "
                "manager_supervisor TEXT NOT NULL DEFAULT ''"
            )
            print("[auto_migrate] vessels.manager_supervisor 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] vessels.manager_supervisor 점검 건너뜀: {exc}")


def _mail_card_columns(conn):
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(mail_card)"
        ).fetchall()]
        if columns and "pending" not in columns:
            conn.execute(
                "ALTER TABLE mail_card ADD COLUMN pending INTEGER NOT NULL DEFAULT 0"
            )
            print("[auto_migrate] mail_card.pending 추가됨")
        if columns and "thread_summary_ko" not in columns:
            conn.execute("ALTER TABLE mail_card ADD COLUMN thread_summary_ko TEXT")
            print("[auto_migrate] mail_card.thread_summary_ko 추가됨")
        if columns and "body_en" not in columns:
            conn.execute("ALTER TABLE mail_card ADD COLUMN body_en TEXT")
            print("[auto_migrate] mail_card.body_en 추가됨")
        if columns and "thread_key" not in columns:
            conn.execute("ALTER TABLE mail_card ADD COLUMN thread_key TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mail_card_thread "
                "ON mail_card(thread_key, card_status)"
            )
            print("[auto_migrate] mail_card.thread_key 추가됨")
        if columns and "action_summary" not in columns:
            conn.execute("ALTER TABLE mail_card ADD COLUMN action_summary TEXT")
            print("[auto_migrate] mail_card.action_summary 추가됨")
        if columns and "category_seed" not in columns:
            conn.execute(
                "ALTER TABLE mail_card ADD COLUMN category_seed INTEGER NOT NULL DEFAULT 0"
            )
            print("[auto_migrate] mail_card.category_seed 추가됨")
        if columns and "card_category" not in columns:
            conn.execute("ALTER TABLE mail_card ADD COLUMN card_category TEXT")
            print("[auto_migrate] mail_card.card_category 추가됨")
    except Exception as exc:
        print(f"[auto_migrate] mail_card.pending 점검 건너뜀: {exc}")


MANAGEMENT_METADATA_MIGRATIONS = (
    ("class_status.source_path", _class_status_source_path),
    ("class_status_items.action_taken", _class_status_action_taken),
    ("vessels.management", _vessel_management_columns),
    ("mail_card.columns", _mail_card_columns),
)


def run_management_metadata_migrations(conn):
    for _name, migrate in MANAGEMENT_METADATA_MIGRATIONS:
        migrate(conn)

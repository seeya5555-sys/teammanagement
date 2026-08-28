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
        if columns and "evidence_image" not in columns:
            conn.execute("ALTER TABLE family_asset_entry ADD COLUMN evidence_image BLOB")
            print("[auto_migrate] family_asset_entry.evidence_image 추가됨")
        if columns and "evidence_mime" not in columns:
            conn.execute("ALTER TABLE family_asset_entry ADD COLUMN evidence_mime TEXT")
            print("[auto_migrate] family_asset_entry.evidence_mime 추가됨")
        if columns and "evidence_captured_at" not in columns:
            conn.execute("ALTER TABLE family_asset_entry ADD COLUMN evidence_captured_at TEXT")
            print("[auto_migrate] family_asset_entry.evidence_captured_at 추가됨")
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


def _family_asset_loan_tables(conn):
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS family_asset_loan_schedule (
                asset_id INTEGER PRIMARY KEY REFERENCES family_asset_entry(id) ON DELETE CASCADE,
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                payment_amount INTEGER NOT NULL CHECK(payment_amount > 0),
                annual_rate_bps INTEGER NOT NULL CHECK(annual_rate_bps BETWEEN 0 AND 10000),
                due_day INTEGER NOT NULL CHECK(due_day BETWEEN 1 AND 31),
                installment_no INTEGER NOT NULL DEFAULT 0 CHECK(installment_no >= 0),
                last_payment_date TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours'))
            );
            CREATE TABLE IF NOT EXISTS family_asset_loan_payment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL REFERENCES family_asset_entry(id) ON DELETE CASCADE,
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                installment_no INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                balance_before INTEGER NOT NULL,
                principal INTEGER NOT NULL,
                interest INTEGER NOT NULL,
                total_payment INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
                UNIQUE(asset_id,due_date)
            );
            CREATE INDEX IF NOT EXISTS idx_family_asset_loan_payment_asset
                ON family_asset_loan_payment(asset_id,due_date DESC);
        """)
    except Exception as exc:
        print(f"[auto_migrate] family_asset_loan 표 점검 건너뜀: {exc}")


def _family_cashflow_tables(conn):
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS family_cash_expense (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                category TEXT NOT NULL CHECK(category IN ('living','utilities','home_loan_interest','car_loan_interest','insurance','education','medical','other')),
                name TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                spent_on TEXT NOT NULL,
                source_type TEXT,
                source_id INTEGER,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
                UNIQUE(household_id,source_type,source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_family_cash_expense_household
                ON family_cash_expense(household_id,spent_on DESC,id DESC);
            CREATE TABLE IF NOT EXISTS family_allowance_budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                member_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                month TEXT NOT NULL,
                allocated_amount INTEGER NOT NULL CHECK(allocated_amount >= 0),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_by INTEGER NOT NULL REFERENCES users(id),
                updated_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
                UNIQUE(household_id,member_user_id,month)
            );
            CREATE TABLE IF NOT EXISTS family_allowance_expense (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_id INTEGER NOT NULL REFERENCES family_allowance_budget(id) ON DELETE CASCADE,
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                spent_on TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours'))
            );
            CREATE INDEX IF NOT EXISTS idx_family_allowance_expense_budget
                ON family_allowance_expense(budget_id,spent_on DESC,id DESC);
            CREATE TABLE IF NOT EXISTS family_cashflow_monthly_input (
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                month TEXT NOT NULL,
                salary_income INTEGER NOT NULL CHECK(salary_income >= 0),
                saving_transfers INTEGER NOT NULL CHECK(saving_transfers >= 0),
                investment_transfers INTEGER NOT NULL CHECK(investment_transfers >= 0),
                loan_principal_payments INTEGER NOT NULL CHECK(loan_principal_payments >= 0),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                updated_by INTEGER NOT NULL REFERENCES users(id),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
                PRIMARY KEY(household_id,month)
            );
            CREATE TABLE IF NOT EXISTS family_cashflow_monthly_close (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
                month TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                salary_income INTEGER NOT NULL,
                ordinary_expenses INTEGER NOT NULL,
                allowance_allocated INTEGER NOT NULL,
                saving_transfers INTEGER NOT NULL,
                investment_transfers INTEGER NOT NULL,
                loan_principal_payments INTEGER NOT NULL,
                allocated_income INTEGER NOT NULL,
                unallocated_income INTEGER NOT NULL,
                closed_by INTEGER NOT NULL REFERENCES users(id),
                closed_at TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
                UNIQUE(household_id,month,revision)
            );
            CREATE INDEX IF NOT EXISTS idx_family_cashflow_close_household
                ON family_cashflow_monthly_close(household_id,month DESC,revision DESC);
        """)
    except Exception as exc:
        print(f"[auto_migrate] family cashflow 표 점검 건너뜀: {exc}")


FAMILY_ASSET_MIGRATIONS = (
    ("family_asset_entry.columns", _family_asset_entry_columns),
    ("family_asset_history.columns", _family_asset_history_columns),
    ("family_asset_loan.tables", _family_asset_loan_tables),
    ("family_cashflow.tables", _family_cashflow_tables),
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

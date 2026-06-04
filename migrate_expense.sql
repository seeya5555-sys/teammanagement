-- 출장 경비 기능 — 기존 운영 DB에 신규 테이블만 추가 (데이터 보존, 재실행 안전)
-- 적용:  sqlite3 instance/trmt.db < migrate_expense.sql

CREATE TABLE IF NOT EXISTS biz_trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id   INTEGER,
    title           TEXT NOT NULL,
    trip_start      TEXT,
    trip_end        TEXT,
    corp_cards      TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','settled')),
    created_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_biz_trips_sup     ON biz_trips(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_biz_trips_status  ON biz_trips(status);
CREATE INDEX IF NOT EXISTS idx_biz_trips_updated ON biz_trips(updated_at DESC);

CREATE TABLE IF NOT EXISTS biz_receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         INTEGER NOT NULL,
    image_filename  TEXT,
    image_url       TEXT,
    vendor          TEXT,
    cost_type       TEXT,
    use_type        TEXT,
    occur_date      TEXT,
    card_no         TEXT,
    remark          TEXT,
    currency        TEXT,
    amount          REAL,
    extracted_raw   TEXT,
    display_order   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (trip_id) REFERENCES biz_trips(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_biz_receipts_trip ON biz_receipts(trip_id, display_order, id);

#!/usr/bin/env python3
"""CLASS STATUS 탭용 테이블 생성 (기존 DB 무손실).

서버에서 1회 실행:
    cd ~/app && venv/bin/python3 migrate_class_status.py
그 후:
    sudo systemctl restart trmt
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'instance', 'trmt.db')

DDL = """
CREATE TABLE IF NOT EXISTS class_status (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id        INTEGER,
    vessel_name_raw  TEXT,
    class_society    TEXT,
    report_date      TEXT,
    source_filename  TEXT,
    uploaded_by      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (vessel_id),
    FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_class_status_vessel ON class_status(vessel_id);

CREATE TABLE IF NOT EXISTS class_status_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cs_id        INTEGER NOT NULL,
    category     TEXT NOT NULL CHECK (category IN ('COC','STATUTORY')),
    no           INTEGER NOT NULL DEFAULT 0,
    issued_date  TEXT,
    description  TEXT,
    due_date     TEXT,
    remark       TEXT,
    importance   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (cs_id) REFERENCES class_status(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_class_status_items_cs ON class_status_items(cs_id, category, no);
"""


def main():
    if not os.path.exists(DATABASE):
        print(f'[ERR] DB 없음: {DATABASE}')
        return
    conn = sqlite3.connect(DATABASE)
    try:
        conn.executescript(DDL)
        conn.commit()
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('class_status','class_status_items')").fetchall()]
        print('[OK] 생성/확인된 테이블:', ', '.join(tabs))
    finally:
        conn.close()


if __name__ == '__main__':
    main()

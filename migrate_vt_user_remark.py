#!/usr/bin/env python3
"""vt_findings 에 user_remark 컬럼 추가 (Vetting Remark 자율입력 칸).

서버에서 1회 실행:
    cd ~/app && venv/bin/python3 migrate_vt_user_remark.py
그 후:
    sudo systemctl restart trmt
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'instance', 'trmt.db')


def main():
    if not os.path.exists(DATABASE):
        print(f'[ERR] DB 없음: {DATABASE}')
        return
    conn = sqlite3.connect(DATABASE)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(vt_findings)").fetchall()]
        if 'user_remark' in cols:
            print('[SKIP] user_remark 컬럼이 이미 존재합니다.')
            return
        conn.execute("ALTER TABLE vt_findings ADD COLUMN user_remark TEXT NOT NULL DEFAULT ''")
        conn.commit()
        print('[OK] vt_findings.user_remark 컬럼 추가 완료.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()

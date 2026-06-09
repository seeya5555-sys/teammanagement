#!/usr/bin/env python3
"""vettings 테이블에 sire_type / valid 컬럼 추가 (테이블 재구성 없음, 재실행 안전).

- sire_type: Idle / Bunkering / Discharge
- valid:     Valid / Invalid
- 기존 operation 값을 sire_type 으로 매핑 복사:
    Loading -> Bunkering, Discharging -> Discharge, Idle -> Idle

사용법 (서버):
    cd ~/app
    ./venv/bin/python3 migrate_sire_type.py
"""
import os
import sqlite3

# app.config 의 DB 경로와 동일하게 탐색
CANDIDATES = [
    os.environ.get('TRMT_DB'),
    os.path.join(os.path.dirname(__file__), 'instance', 'trmt.db'),
    '/home/opc/app/instance/trmt.db',
]
DB = next((p for p in CANDIDATES if p and os.path.exists(p)), None)
if not DB:
    raise SystemExit('trmt.db 를 찾을 수 없습니다. TRMT_DB 환경변수로 경로를 지정하세요.')

print(f'· DB: {DB}')
con = sqlite3.connect(DB)
cur = con.cursor()

cols = {r[1] for r in cur.execute('PRAGMA table_info(vettings)').fetchall()}

if 'sire_type' not in cols:
    cur.execute('ALTER TABLE vettings ADD COLUMN sire_type TEXT')
    print('  + sire_type 컬럼 추가')
else:
    print('  = sire_type 이미 존재')

if 'valid' not in cols:
    cur.execute('ALTER TABLE vettings ADD COLUMN valid TEXT')
    print('  + valid 컬럼 추가')
else:
    print('  = valid 이미 존재')

# 기존 operation -> sire_type 매핑 (sire_type 이 비어있는 행만)
if 'operation' in cols:
    n = cur.execute("""
        UPDATE vettings
           SET sire_type = CASE operation
                 WHEN 'Loading'     THEN 'Bunkering'
                 WHEN 'Discharging' THEN 'Discharge'
                 WHEN 'Idle'        THEN 'Idle'
                 ELSE NULL END
         WHERE (sire_type IS NULL OR sire_type = '')
           AND operation IS NOT NULL AND operation <> ''
    """).rowcount
    print(f'  · operation -> sire_type 매핑: {n}행')

con.commit()
con.close()
print('완료.')

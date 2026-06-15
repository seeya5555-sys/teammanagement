"""
TRMT3 Ship Management System
────────────────────────────────────────────────────────────────
Flask 메인 (DD Manager 스타일 — 단일 파일, 순수 SQL, ORM 없음)

로컬 실행        :  python app.py
DB 재초기화     :  python app.py --init-db
"""
import os
import sys
import uuid
import json
import sqlite3
import secrets
from functools import wraps
from datetime import timedelta, date

from flask import (
    Flask, g, request, jsonify, session, render_template,
    redirect, url_for, send_from_directory, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ═════════════════════════════════════════════════════════════════
#  Config
# ═════════════════════════════════════════════════════════════════
BASE_DIR     = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
UPLOAD_DIR   = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR,   exist_ok=True)

DATABASE        = os.path.join(INSTANCE_DIR, 'trmt.db')
SCHEMA_FILE     = os.path.join(BASE_DIR, 'schema.sql')
SEED_FILE       = os.path.join(BASE_DIR, 'seed.sql')
SECRET_KEY_FILE = os.path.join(INSTANCE_DIR, '.secret_key')

ALLOWED_EXT = {
    'jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'webp', 'bmp',
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'csv'
}

def _load_or_create_secret_key():
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'rb') as f:
            return f.read()
    key = secrets.token_bytes(32)
    with open(SECRET_KEY_FILE, 'wb') as f:
        f.write(key)
    return key

app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_or_create_secret_key(),
    DATABASE=DATABASE,
    UPLOAD_FOLDER=UPLOAD_DIR,
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,          # 핸드폰 사진 대비 20MB
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    JSON_AS_ASCII=False,
    SESSION_COOKIE_SAMESITE='Lax',
    SEND_FILE_MAX_AGE_DEFAULT=0,                   # static(css/js) 매번 재검증 — 모바일 캐시 stale 방지
)


# static(css/js) URL에 파일 수정시각을 ?v= 로 자동 부착 — 파일 변경 시 URL이 바뀌어
# 브라우저(특히 iOS Safari) 캐시를 강제 무효화. 템플릿 수정 불필요(모든 url_for('static') 적용).
@app.url_defaults
def _add_static_version(endpoint, values):
    if endpoint == 'static' and values.get('filename'):
        try:
            fp = os.path.join(app.static_folder, values['filename'])
            values['v'] = int(os.path.getmtime(fp))
        except OSError:
            pass


# ═════════════════════════════════════════════════════════════════
#  DB helpers
# ═════════════════════════════════════════════════════════════════
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows

def execute(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    last_id = cur.lastrowid
    cur.close()
    return last_id


def execute_rc(sql, params=()):
    """UPDATE/DELETE 영향 행수 반환 — 조건부(낙관적 락) 갱신 race 판정용."""
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    rc = cur.rowcount
    cur.close()
    return rc

def init_db(drop=False):
    """schema + seed 실행, 기본 admin 계정 자동 생성.

    재실행 안전: 이미 데이터가 있어도 schema는 IF NOT EXISTS 라 무해.
    옛 priority 값(Critical/High/Low)이 남아있으면 새 분류로 자동 마이그레이션.
    """
    if drop and os.path.exists(DATABASE):
        os.remove(DATABASE)
        print(f'  · 기존 DB 삭제: {DATABASE}')

    fresh = not os.path.exists(DATABASE)
    conn = sqlite3.connect(DATABASE)
    try:
        # ── 마이그레이션 단계 ──
        # SQLite는 CHECK 제약을 ALTER TABLE 로 못 바꿈.
        # 옛 CHECK가 박혀있는 테이블이면 새 스키마로 재구축하면서
        # 데이터를 새 분류로 정규화.
        # 또한 ALTER TABLE RENAME 시 다른 테이블의 FK 참조가 자동 추적되는
        # 동작 때문에 attachments의 FK가 깨질 수 있음 → legacy_alter_table 사용.
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='issues'"
        ).fetchone()
        if existing:
            ddl_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='issues'"
            ).fetchone()
            ddl = ddl_row[0] if ddl_row else ''
            # 새 분류 키워드 4개 모두 포함하는지 확인
            needs_rebuild = ('Next DD' not in ddl)
            if needs_rebuild:
                old_vals = [r[0] for r in conn.execute(
                    "SELECT DISTINCT priority FROM issues "
                    "WHERE priority NOT IN ('Normal','Urgent','COC & Flag','Next DD')"
                ).fetchall()]
                if old_vals:
                    print(f'  · priority 마이그레이션: {old_vals}')
                print('  · issues 테이블 CHECK 제약 갱신 중...')

                # legacy_alter_table=ON: RENAME 시 다른 테이블의 FK 참조가
                # 자동으로 따라가지 않도록 해서 attachments FK 보호
                conn.execute('PRAGMA legacy_alter_table=ON')
                conn.execute('PRAGMA foreign_keys=OFF')
                conn.execute('ALTER TABLE issues RENAME TO issues_old')
                # 새 스키마 CREATE
                with open(SCHEMA_FILE, encoding='utf-8') as f:
                    conn.executescript(f.read())
                # 데이터 복원하면서 priority 정규화 (Critical → COC & Flag, 그 외 → Normal)
                conn.execute("""
                    INSERT INTO issues
                        (id, supervisor_id, vessel_id, issue_date, due_date,
                         item_topic, description, actions, priority, status,
                         created_by, created_at, updated_at)
                    SELECT
                         id, supervisor_id, vessel_id, issue_date, due_date,
                         item_topic, description, COALESCE(actions, '[]'),
                         CASE
                             WHEN priority IN ('Normal','Urgent','COC & Flag','Next DD')
                                 THEN priority
                             WHEN priority = 'Critical' THEN 'COC & Flag'
                             ELSE 'Normal'
                         END,
                         status, created_by,
                         COALESCE(created_at, CURRENT_TIMESTAMP),
                         COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
                    FROM issues_old
                """)
                conn.execute('DROP TABLE issues_old')
                conn.execute('PRAGMA legacy_alter_table=OFF')
                conn.execute('PRAGMA foreign_keys=ON')
                conn.commit()
                print('  · CHECK 제약 갱신 완료')

            # ── attachments FK 무결성 검증 + 자동 복원 ──
            # 과거 마이그레이션 사고로 깨졌을 수 있는 attachments FK 보정
            att_ddl_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='attachments'"
            ).fetchone()
            if att_ddl_row and 'issues_old' in (att_ddl_row[0] or ''):
                print('  · attachments FK 깨짐 감지 → 복원 중...')
                rows = conn.execute('SELECT * FROM attachments').fetchall()
                cols = [r[1] for r in conn.execute('PRAGMA table_info(attachments)').fetchall()]
                conn.execute('PRAGMA foreign_keys=OFF')
                conn.execute('ALTER TABLE attachments RENAME TO attachments_broken')
                conn.execute("""
                    CREATE TABLE attachments (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        issue_id    INTEGER NOT NULL,
                        filename    TEXT    NOT NULL,
                        stored_name TEXT    NOT NULL UNIQUE,
                        file_size   INTEGER,
                        mime_type   TEXT,
                        uploaded_by TEXT,
                        uploaded_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                        FOREIGN KEY (issue_id) REFERENCES issues(id) ON DELETE CASCADE
                    )
                """)
                if rows:
                    placeholders = ','.join(['?'] * len(cols))
                    conn.executemany(
                        f'INSERT INTO attachments ({",".join(cols)}) VALUES ({placeholders})',
                        rows,
                    )
                conn.execute('DROP TABLE attachments_broken')
                conn.execute('PRAGMA foreign_keys=ON')
                conn.commit()
                print(f'  · attachments {len(rows)}건 복원 완료')

        # ── 일반 init ──
        with open(SCHEMA_FILE, encoding='utf-8') as f:
            conn.executescript(f.read())
        print('  · 스키마 적용 완료')

        # cs_surveys 에 manual_*_count 컬럼이 없으면 추가 (기존 DB 보강)
        cs_cols = [r[1] for r in conn.execute('PRAGMA table_info(cs_surveys)').fetchall()]
        if cs_cols:  # cs_surveys 테이블이 존재할 때만
            for col in ('manual_defect_count', 'manual_observation_count', 'manual_close_count'):
                if col not in cs_cols:
                    conn.execute(f'ALTER TABLE cs_surveys ADD COLUMN {col} INTEGER')
                    print(f'  · cs_surveys.{col} 컬럼 추가')
            conn.commit()

        # cs_findings 에 item 컬럼이 없으면 추가
        cf_cols = [r[1] for r in conn.execute('PRAGMA table_info(cs_findings)').fetchall()]
        if cf_cols and 'item' not in cf_cols:
            conn.execute('ALTER TABLE cs_findings ADD COLUMN item TEXT')
            print('  · cs_findings.item 컬럼 추가')
            conn.commit()

        # issues 에 Outlook 매칭용 컬럼 추가 (메일 dedup)
        iss_cols = [r[1] for r in conn.execute('PRAGMA table_info(issues)').fetchall()]
        if iss_cols:
            for _c in ('email_subject_norm', 'email_conv_id'):
                if _c not in iss_cols:
                    conn.execute(f'ALTER TABLE issues ADD COLUMN {_c} TEXT')
                    print(f'  - issues.{_c} column added')
            conn.commit()


        # cs_surveys.vendor CHECK 제약 제거 (AALMAR/IDWAL 외 자유 입력 허용)
        try:
            sql_def = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='cs_surveys'",
            ).fetchone()
            if sql_def and "CHECK (vendor IN" in (sql_def[0] or ''):
                conn.executescript("""
                    PRAGMA foreign_keys = OFF;
                    BEGIN;
                    CREATE TABLE cs_surveys_new (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        vessel_id       INTEGER NOT NULL,
                        year            INTEGER NOT NULL,
                        quarter         INTEGER NOT NULL CHECK (quarter IN (1,2,3,4)),
                        vendor          TEXT,
                        management      TEXT,
                        inspection_date TEXT,
                        overall_remark  TEXT,
                        manual_defect_count      INTEGER,
                        manual_observation_count INTEGER,
                        manual_close_count       INTEGER,
                        created_by      TEXT,
                        created_at      TEXT DEFAULT (datetime('now','localtime')),
                        updated_at      TEXT DEFAULT (datetime('now','localtime')),
                        UNIQUE (vessel_id, year, quarter),
                        FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
                    );
                    INSERT INTO cs_surveys_new
                      SELECT id, vessel_id, year, quarter, vendor, management,
                             inspection_date, overall_remark,
                             manual_defect_count, manual_observation_count, manual_close_count,
                             created_by, created_at, updated_at
                      FROM cs_surveys;
                    DROP TABLE cs_surveys;
                    ALTER TABLE cs_surveys_new RENAME TO cs_surveys;
                    CREATE INDEX IF NOT EXISTS idx_cs_surveys_vessel_year ON cs_surveys(vessel_id, year);
                    COMMIT;
                    PRAGMA foreign_keys = ON;
                """)
                print('  · cs_surveys.vendor CHECK 제약 제거 (자유 입력 허용)')
        except Exception as e:
            print(f'  · cs_surveys vendor 마이그레이션 스킵: {e}')

        # 자동화 모음(자동화 실행 큐+상태+audit)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS automation_run (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT NOT NULL,
                task          TEXT NOT NULL,
                mode          TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'queued',
                requested_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                requested_by  TEXT,
                started_at    TEXT,
                finished_at   TEXT,
                exit_code     INTEGER,
                summary       TEXT
            )
        """)

        # AOR(Technical) 검토→상신 draft 큐 (prep 엔진이 ingest, 사람이 /aor 탭서 승인→맥이 SVMS 상신)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS aor_draft (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                aor_cd           TEXT NOT NULL,                       -- SVMS 문서번호(dedup 키)
                vsl_cd           TEXT,
                vsl_nm           TEXT,
                subj             TEXT,
                amt              REAL,                                -- AOR 금액(SVMS)
                cur_cd           TEXT,
                req_user_nm      TEXT,                                -- 요청자(관리사)
                cost_proposed    REAL,                               -- 이메일서 추출한 제안비용
                cost_match       INTEGER,                            -- 1=일치 0=불일치 NULL=미상
                match_conf       INTEGER,                            -- 이메일 매칭 신뢰도 0-100
                email_subj       TEXT,                               -- 매칭된 메일 제목
                proposed_comment TEXT,                               -- Comment 3단 초안
                approval_app_no  TEXT,                               -- 추천 결재라인 APP_NO
                approval_line    TEXT,                               -- 결재자 표시용 JSON(이름)
                attach_files     TEXT,                               -- 첨부 견적서 파일명 JSON 배열
                raw_row          TEXT,                               -- SP_GET_AOR 행 전체 JSON(상신때 재사용)
                status           TEXT NOT NULL DEFAULT 'pending',    -- pending/approved/submitting/submitted/failed/rejected
                decided_at       TEXT,
                decided_by       TEXT,
                submitted_at     TEXT,
                submit_result    TEXT,
                reject_reason    TEXT,
                created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aor_draft_status ON aor_draft(status)")

        # 비용청구(Fund Request) 2단게이트 draft 큐 (review 엔진 ingest → 사람이 /fundreq 탭서 승인/리젝 결정 → 맥이 SVMS 상신/리젝+통보메일)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fundreq_draft (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                opex_cd       TEXT NOT NULL,                       -- SVMS Fund Request 문서번호(dedup 키)
                vsl_cd        TEXT,
                vsl_nm        TEXT,
                subj          TEXT,
                amt           REAL,                                -- Cost(청구비용)
                cur_cd        TEXT,
                tp            TEXT,                                -- A=AOR / P=Pre-delivery / O=OPEX
                ref_no        TEXT,                                -- 연동 AOR 문서번호
                ref_amt       REAL,                                -- 연동 AOR 금액
                dn            TEXT,                                -- 첨부 DN/인보이스 판독 결과(금액+통화)
                diff          REAL,                                -- AOR차액(cost-ref_amt)
                verdict       TEXT,                                -- 검토결과 pass/escalate/mismatch/flag
                why           TEXT,                                -- 미상신 사유(검토)
                raw_row       TEXT,                                -- SP_GET_OPEX 행 전체 JSON(상신/리젝때 재조회 키만 사용)
                status        TEXT NOT NULL DEFAULT 'pending',     -- pending/approved/submitting/submitted/rejecting/rejected/failed/reject_failed
                reject_reason TEXT,
                decided_at    TEXT,
                decided_by    TEXT,
                done_at       TEXT,
                result        TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fundreq_draft_status ON fundreq_draft(status)")

        # reqgen: 입거 requisition 엑셀 → SVMS 구매청구(PKG_PC_REQ.SP_SET_REQ_INFO) DRAFT 자동작성 큐
        #   사람이 /reqgen 탭서 엑셀 업로드 → 시트별 카드 적재(파싱) → Voyage/Port/Date 입력+승인 →
        #   맥 러너(reqgen_save)가 SVMS NEW→SP_SET_REQ_INFO 로 DRAFT 저장(상신은 사람이 SVMS서 직접)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reqgen_draft (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                batch       TEXT,                              -- 업로드 묶음 id
                doc_type    TEXT NOT NULL DEFAULT 'PC',        -- PC=구매청구(S/ST) / MA=수리신청(R)
                sheet       TEXT NOT NULL,                     -- S1/ST1/R17 등 (dedup: batch+sheet)
                vsl_cd      TEXT,
                vsl_nm      TEXT,
                part_tp     TEXT,                              -- 0=Spare Part / 1=Consumable(Store)
                kind_nm     TEXT,
                equipment   TEXT,                              -- CATE_NM=EQ_NM (자유텍스트)
                subj        TEXT,                              -- [DOCK] ...
                line_cnt    INTEGER,
                exp_cd      TEXT,                              -- 대표 Exp code(첫 라인)
                header_json TEXT,                              -- SP_SET_REQ_INFO PARAM(헤더)
                lines_json  TEXT,                              -- CURSOR.P_IC 라인 배열
                voyage      TEXT,                              -- 카드 입력(승인 전 필수)
                port        TEXT,                              -- 항구코드
                port_nm     TEXT,
                req_dt      TEXT,                              -- YYYYMMDD
                stock       TEXT DEFAULT 'service',            -- 수리 Stock of Spare: service/owner (카드별)
                status      TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/saving/saved/failed
                req_no      TEXT,                              -- SVMS 저장 후 채번된 REQ_NO
                result      TEXT,
                decided_at  TEXT,
                decided_by  TEXT,
                done_at     TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reqgen_draft_status ON reqgen_draft(status)")
        try:                                            # 기존 DB 마이그레이션(doc_type 추가)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(reqgen_draft)").fetchall()]
            if 'doc_type' not in cols:
                conn.execute("ALTER TABLE reqgen_draft ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'PC'")
            if 'stock' not in cols:
                conn.execute("ALTER TABLE reqgen_draft ADD COLUMN stock TEXT DEFAULT 'service'")
        except Exception:
            pass

        if fresh and os.path.exists(SEED_FILE):
            with open(SEED_FILE, encoding='utf-8') as f:
                conn.executescript(f.read())
            print('  · 시드 데이터 로드 완료')

        # 기본 admin 계정 자동 생성
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute(
                'INSERT INTO users (username, password_hash, display_name, role) '
                'VALUES (?, ?, ?, ?)',
                ('admin', generate_password_hash('admin0424'),
                 'Administrator', 'admin'),
            )
            print('  · 기본 관리자 생성: admin / admin0424')
        conn.commit()
        print(f'[OK] DB 초기화 완료: {DATABASE}')
    finally:
        conn.close()


def _seed_issues(conn):
    """예시 이슈들 — actions 배열로 여러 팔로우업 entry 포함."""
    SEED = [
        dict(supervisor='손차장', vessel='KUWAIT PROSPERITY',
             issue_date='2026-04-24', due_date='2026-04-26',
             item_topic='Job 40.1 WBT Pipe Renewal 추가견적 Tariff 오류',
             description='1. YiuLian 추가견적 분석 결과 Tariff 적용 오류 발견.\n'
                         '2. 할인율 재적용 시 약 USD 16,000 절감 가능.\n'
                         '3. 정정 견적 필요 — Ch.40 WBT Plug 기준.',
             actions=[
                 {'date': '2026-04-24', 'progress': 'Tariff 오류 분석 완료. 정정견적 공식 요청 메일 발송.', 'important': False},
                 {'date': '2026-04-25', 'progress': 'Xue Jing Gang 측 중간 회신 — 내부 검토 중.', 'important': False},
                 {'date': '2026-04-26', 'progress': '정정 견적 회신 기한. 미회신 시 상부 보고.', 'important': True},
             ],
             priority='COC & Flag', status='Open'),

        dict(supervisor='이과장', vessel='ATLANTIC PIONEER',
             issue_date='2026-04-24', due_date='2026-04-24',
             item_topic='Pre-docking Meeting Agenda 회신 누락',
             description='1. Will (CSM SG) 측 회신 미도착.\n'
                         '2. 손차장 작성분 Agenda 수정본 공유 필요.',
             actions=[
                 {'date': '2026-04-23', 'progress': 'CSM Singapore 앞 Agenda 초안 송부.', 'important': False},
                 {'date': '2026-04-24', 'progress': '금일 중 Will 에게 재요청 콜.', 'important': True},
             ],
             priority='Urgent', status='Open'),

        dict(supervisor='김과장', vessel='SAUDI EXPORT',
             issue_date='2026-04-23', due_date='2026-04-25',
             item_topic='No.2 Aux Boiler 간헐 Flame Failure',
             description='1. 항차 중 기관장 보고 — 3회 발생.\n'
                         '2. 수동 재점화로 복귀, 운항 영향 없음.\n'
                         '3. Flame rod / Photocell 부품 조달 검토.',
             actions=[
                 {'date': '2026-04-23', 'progress': '기관장 최초 보고 접수. 운항 지장 없음 확인.', 'important': False},
                 {'date': '2026-04-24', 'progress': 'Miura 부산대리점 앞 기술지원 요청.', 'important': False},
                 {'date': '2026-04-25', 'progress': '대리점 회신 기한. 부품 Q\'ty / 단가 확정.', 'important': True},
             ],
             priority='Urgent', status='Open'),

        dict(supervisor='손차장', vessel='KUWAIT PROSPERITY',
             issue_date='2026-04-22', due_date='2026-04-28',
             item_topic='Main Engine Maker/Model 스펙 불일치',
             description='1. DD Spec 과 YiuLian 견적서 상 M/E 메이커 기재 상이.\n'
                         '2. Turbocharger, Governor, Alternator 동일 이슈.\n'
                         '3. Pre-docking meeting 공식 안건 상정.',
             actions=[
                 {'date': '2026-04-22', 'progress': '견적서 상 메이커 기재 오류 발견 — 내부 공유.', 'important': False},
                 {'date': '2026-04-23', 'progress': 'YiuLian 측 구두 확인 — 오기재 인정. 정정 약속.', 'important': False},
                 {'date': '2026-04-28', 'progress': 'Pre-docking meeting 에서 공식 정정본 수령 예정.', 'important': True},
             ],
             priority='COC & Flag', status='InProgress'),

        dict(supervisor='이과장', vessel='ATLANTIC PIONEER',
             issue_date='2026-04-22', due_date='2026-04-30',
             item_topic='Vetting 지적 Close-out 증빙자료 취합',
             description='1. 본선 현장 사진 2건 회신 대기.\n'
                         '2. SIRE 2.0 기준 CAR 2건, CR 1건.',
             actions=[
                 {'date': '2026-04-22', 'progress': '본선 Master 앞 현장 사진 요청 메일 발송.', 'important': False},
                 {'date': '2026-04-24', 'progress': '사진 2건 수령. Close-out 보고서 초안 작성.', 'important': False},
                 {'date': '2026-04-30', 'progress': 'Close-out 제출 기한.', 'important': True},
             ],
             priority='Urgent', status='InProgress'),

        dict(supervisor='손차장', vessel='KUWAIT GLORY',
             issue_date='2026-04-18', due_date=None,
             item_topic='IG Scrubber Nozzle 세정 완료 보고',
             description='1. Service Station 방문 — 세정 / 기능 테스트 완료.\n'
                         '2. Class 입회 불요, 본선 성적서 수령.',
             actions=[
                 {'date': '2026-04-16', 'progress': 'Service Station 방문. 세정 작업 진행.', 'important': False},
                 {'date': '2026-04-18', 'progress': 'Service Report 수령 완료. 선적 보관.', 'important': False},
             ],
             priority='Normal', status='Closed'),

        # 지난 달 이슈 — 월별 접기 샘플
        dict(supervisor='손차장', vessel='KUWAIT PROSPERITY',
             issue_date='2026-03-28', due_date=None,
             item_topic='DD Specification Final Review',
             description='1. Chapter 1~44 전체 검토 완료.\n'
                         '2. Add Spec 23건 반영.',
             actions=[
                 {'date': '2026-03-28', 'progress': 'Final review 완료. CSM 공유.', 'important': False},
             ],
             priority='Normal', status='Closed'),

        dict(supervisor='김과장', vessel='SAUDI EXPORT',
             issue_date='2026-03-15', due_date=None,
             item_topic='Annual Crew Survey 완료',
             description='Master 이하 주요 포지션 Annual Survey 완료.',
             actions=[
                 {'date': '2026-03-15', 'progress': 'Survey 완료. 특이사항 없음.', 'important': False},
             ],
             priority='Normal', status='Closed'),
    ]

    for i in SEED:
        conn.execute('''
            INSERT INTO issues
                (supervisor_id, vessel_id, issue_date, due_date,
                 item_topic, description, actions, priority, status, created_by)
            VALUES (
                (SELECT id FROM supervisors WHERE name=?),
                (SELECT id FROM vessels     WHERE name=?),
                ?, ?, ?, ?, ?, ?, ?, 'seed'
            )
        ''', (
            i['supervisor'], i['vessel'], i['issue_date'], i['due_date'],
            i['item_topic'], i['description'],
            json.dumps(i['actions'], ensure_ascii=False),
            i['priority'], i['status']
        ))


# ═════════════════════════════════════════════════════════════════
#  Auth decorators
# ═════════════════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized'}), 401
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapped

def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        if session.get('role') != 'admin':
            return jsonify({'error': 'forbidden'}), 403
        return f(*args, **kwargs)
    return wrapped


# ═════════════════════════════════════════════════════════════════
#  Pages
# ═════════════════════════════════════════════════════════════════
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if 'user_id' in session:
            return redirect(url_for('dashboard'))
        return render_template('login.html')

    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    u = query('SELECT * FROM users WHERE username=? AND active=1',
              (username,), one=True)
    if not u or not check_password_hash(u['password_hash'], password):
        return render_template(
            'login.html',
            error='아이디 또는 비밀번호가 올바르지 않습니다.',
            username=username,
        ), 401

    session.clear()
    session.permanent = True
    session['user_id']       = u['id']
    session['username']      = u['username']
    session['display_name']  = u['display_name'] or u['username']
    session['role']          = u['role']
    session['supervisor_id'] = u['supervisor_id']
    execute('UPDATE users SET last_login_at=datetime("now","localtime") WHERE id=?',
            (u['id'],))

    nxt = request.args.get('next') or url_for('dashboard')
    # 외부 URL 리다이렉트 방지
    if not nxt.startswith('/'):
        nxt = url_for('dashboard')
    return redirect(nxt)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    """주요 현황 한눈에 — 카드 클릭 시 해당 탭 이동.
    로그인 사용자가 감독에 연결돼 있으면(supervisor_id) 그 감독 담당선박 기준으로 집계,
    아니면(미연결 admin 등) 전체 기준."""
    today   = date.today().isoformat()
    horizon = (date.today() + timedelta(days=30)).isoformat()
    cal_end = (date.today() + timedelta(days=7)).isoformat()
    is_admin = (session.get('role') == 'admin')

    sup_id = session.get('supervisor_id')
    scoped = bool(sup_id)
    sup_name = None
    vessel_ids = []
    if scoped:
        srow = query("SELECT name FROM supervisors WHERE id=?", (sup_id,), one=True)
        sup_name = srow['name'] if srow else None
        vessel_ids = [r['vessel_id'] for r in
                      query("SELECT vessel_id FROM supervisor_vessels WHERE supervisor_id=?", (sup_id,))]

    def vin(col):
        """담당선박 IN 절. 미연결=전체(1=1), 연결+선박없음=0건(0=1)."""
        if not scoped:
            return ("1=1", [])
        if not vessel_ids:
            return ("0=1", [])
        return (f"{col} IN ({','.join('?' * len(vessel_ids))})", list(vessel_ids))

    # 1) 현안 요약 — 감독 연결 시 그 감독 이슈만(issues.supervisor_id)
    iss_where = "WHERE supervisor_id=?" if scoped else ""
    iss_params = (sup_id,) if scoped else ()
    iss = query(
        "SELECT "
        "SUM(CASE WHEN status!='Closed' THEN 1 ELSE 0 END) open_cnt, "
        "SUM(CASE WHEN status!='Closed' AND priority='Urgent' THEN 1 ELSE 0 END) urgent_cnt, "
        "SUM(CASE WHEN status!='Closed' AND priority='COC & Flag' THEN 1 ELSE 0 END) coc_cnt, "
        "SUM(CASE WHEN status!='Closed' AND priority='Next DD' THEN 1 ELSE 0 END) dd_cnt "
        f"FROM issues {iss_where}", iss_params, one=True)

    # 2) Class 만기 임박 (due_date D-30, 담당선박)
    cvf, cvp = vin("cs.vessel_id")
    class_due = query(
        "SELECT COUNT(*) c FROM class_status_items i JOIN class_status cs ON cs.id=i.cs_id "
        "WHERE i.due_date IS NOT NULL AND i.due_date != '' "
        f"AND i.due_date >= ? AND i.due_date <= ? AND {cvf}",
        (today, horizon, *cvp), one=True)['c']

    # 3) Vetting 미해결 (Open observation, 담당선박)
    vvf, vvp = vin("vt.vessel_id")
    vrow = query(
        "SELECT "
        "SUM(CASE WHEN f.status='Open' THEN 1 ELSE 0 END) open_cnt, "
        "SUM(CASE WHEN f.status='Open' AND f.priority=1 THEN 1 ELSE 0 END) pri_cnt "
        "FROM vt_findings f JOIN vettings vt ON vt.id=f.vetting_id "
        f"WHERE {vvf}", (*vvp,), one=True)

    # 4) 다가오는 일정 (7일) — 담당선박/본인/공용
    if scoped:
        evf, evp = vin("vessel_id")
        events = query(
            "SELECT title, start_date, category, color FROM calendar_events "
            "WHERE start_date >= ? AND start_date <= ? "
            f"AND (supervisor_id=? OR supervisor_id IS NULL OR {evf}) "
            "ORDER BY start_date ASC, COALESCE(start_time,'') ASC LIMIT 8",
            (today, cal_end, sup_id, *evp))
    else:
        events = query(
            "SELECT title, start_date, category, color FROM calendar_events "
            "WHERE start_date >= ? AND start_date <= ? "
            "ORDER BY start_date ASC, COALESCE(start_time,'') ASC LIMIT 8",
            (today, cal_end))

    stats = {
        'issues_open':   (iss['open_cnt']   or 0) if iss else 0,
        'issues_urgent': (iss['urgent_cnt'] or 0) if iss else 0,
        'issues_coc':    (iss['coc_cnt']    or 0) if iss else 0,
        'issues_dd':     (iss['dd_cnt']     or 0) if iss else 0,
        'class_due':     class_due,
        'vetting_open':  (vrow['open_cnt'] or 0) if vrow else 0,
        'vetting_pri':   (vrow['pri_cnt']  or 0) if vrow else 0,
        'aor_pending':   0,
        'aor_crew_submitted': 0,
        'mail_active':   0,
    }
    # 자동화 위젯은 admin 만 (탭 자체가 admin 전용) — 전사 큐라 감독 스코프 무관
    if is_admin:
        ap = query("SELECT COUNT(*) c FROM aor_draft WHERE status='pending'", one=True)
        stats['aor_pending'] = ap['c'] if ap else 0
        mc = query("SELECT COUNT(*) c FROM mail_card WHERE card_status='active'", one=True)
        stats['mail_active'] = mc['c'] if mc else 0
        try:
            r = query("SELECT v FROM api_settings WHERE k='aor_crew_submitted'", one=True)
            stats['aor_crew_submitted'] = int(r['v'] or 0) if r else 0
        except sqlite3.Error:
            pass

    return render_template('dashboard.html', stats=stats, events=events, is_admin=is_admin,
                           scoped=scoped, sup_name=sup_name)


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/condition-survey')
@login_required
def condition_survey():
    return render_template('condition_survey.html')


@app.route('/vetting-status')
@login_required
def vetting_status():
    return render_template('vetting_status.html')


@app.route('/class-status')
@login_required
def class_status_page():
    return render_template('class_status.html')


@app.route('/calendar')
@login_required
def calendar_page():
    return render_template('calendar.html')


@app.route('/dry-dock')
@login_required
def dry_dock_page():
    return render_template('dry_dock.html')


@app.route('/dry-dock/<int:rid>/edit')
@login_required
def dry_dock_edit_page(rid):
    r = query('SELECT id FROM dock_reports WHERE id=?', (rid,), one=True)
    if not r:
        abort(404)
    return render_template('dry_dock_edit.html', report_id=rid)


@app.route('/boarding')
@login_required
def boarding_page():
    return render_template('boarding.html')


@app.route('/boarding/<int:rid>/edit')
@login_required
def boarding_edit_page(rid):
    r = query('SELECT id FROM boarding_reports WHERE id=?', (rid,), one=True)
    if not r:
        abort(404)
    return render_template('boarding_edit.html', report_id=rid)


# ═════════════════════════════════════════════════════════════════
#  API — me / password
# ═════════════════════════════════════════════════════════════════
@app.route('/api/me')
@login_required
def api_me():
    return jsonify({
        'user_id':       session['user_id'],
        'username':      session['username'],
        'display_name':  session.get('display_name'),
        'role':          session.get('role'),
        'supervisor_id': session.get('supervisor_id'),
    })

@app.route('/api/me/password', methods=['POST'])
@login_required
def api_me_password():
    d = request.get_json(silent=True) or {}
    old = d.get('old_password') or ''
    new = d.get('new_password') or ''
    if len(new) < 6:
        return jsonify({'error': '신규 비밀번호는 최소 6자 이상이어야 합니다.'}), 400
    u = query('SELECT * FROM users WHERE id=?',
              (session['user_id'],), one=True)
    if not check_password_hash(u['password_hash'], old):
        return jsonify({'error': '기존 비밀번호가 일치하지 않습니다.'}), 400
    execute('UPDATE users SET password_hash=? WHERE id=?',
            (generate_password_hash(new), session['user_id']))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — supervisors
# ═════════════════════════════════════════════════════════════════
@app.route('/api/supervisors')
@login_required
def api_supervisors():
    rows = query('''
        SELECT
            s.id, s.name, s.color, s.display_order, s.email,
            (SELECT COUNT(*) FROM issues i WHERE i.supervisor_id = s.id)
                AS total,
            (SELECT COUNT(*) FROM issues i WHERE i.supervisor_id = s.id AND i.status='Open')
                AS open_count,
            (SELECT COUNT(*) FROM issues i WHERE i.supervisor_id = s.id AND i.status='InProgress')
                AS progress_count,
            (SELECT COUNT(*) FROM issues i WHERE i.supervisor_id = s.id AND i.status='Closed')
                AS closed_count,
            (SELECT GROUP_CONCAT(v.name, ', ')
                FROM supervisor_vessels sv
                JOIN vessels v ON v.id = sv.vessel_id
               WHERE sv.supervisor_id = s.id) AS vessels
          FROM supervisors s
         WHERE s.active = 1
         ORDER BY s.display_order, s.id
    ''')
    return jsonify([dict(r) for r in rows])


# ═════════════════════════════════════════════════════════════════
#  API — vessels
# ═════════════════════════════════════════════════════════════════
@app.route('/api/vessels')
@login_required
def api_vessels():
    sup = request.args.get('supervisor_id', type=int)
    if sup:
        rows = query('''
            SELECT v.* FROM vessels v
              JOIN supervisor_vessels sv ON sv.vessel_id = v.id
             WHERE sv.supervisor_id = ? AND v.active = 1
             ORDER BY v.name
        ''', (sup,))
    else:
        rows = query('SELECT * FROM vessels WHERE active=1 ORDER BY name')
    return jsonify([dict(r) for r in rows])


# 선박별 활성(Open + InProgress) 이슈 수 — Daily 필터 드롭다운용
#   · 다른 화면 필터(감독, 검색, 우선순위, 선종)는 적용
#   · 선박 필터 자체는 무시 (드롭다운 라벨용이므로)
@app.route('/api/vessels/active-counts')
@login_required
def api_vessel_active_counts():
    conds = ["i.status IN ('Open', 'InProgress')"]
    params = []

    sup = request.args.get('supervisor_id')
    if sup:
        conds.append('i.supervisor_id = ?')
        params.append(sup)

    q = request.args.get('q')
    if q:
        like = f'%{q}%'
        conds.append('(i.item_topic LIKE ? OR i.description LIKE ? OR i.actions LIKE ?)')
        params += [like, like, like]

    vt = request.args.get('vessel_type')
    if vt:
        conds.append('v.vessel_type = ?')
        params.append(vt)

    pri = request.args.get('priority')
    if pri:
        conds.append('i.priority = ?')
        params.append(pri)

    sql = f'''
        SELECT i.vessel_id, COUNT(*) AS cnt
          FROM issues i
          JOIN vessels v ON v.id = i.vessel_id
         WHERE {' AND '.join(conds)}
         GROUP BY i.vessel_id
    '''
    rows = query(sql, params)
    return jsonify({str(r['vessel_id']): r['cnt'] for r in rows})


# ═════════════════════════════════════════════════════════════════
#  API — issues (list / get / create / update / delete)
# ═════════════════════════════════════════════════════════════════
@app.route('/api/issues')
@login_required
def api_issue_list():
    conds, params = ['1=1'], []
    for key, col in [('supervisor_id', 'i.supervisor_id'),
                     ('vessel_id',     'i.vessel_id'),
                     ('status',        'i.status'),
                     ('priority',      'i.priority')]:
        val = request.args.get(key)
        if val:
            conds.append(f'{col} = ?')
            params.append(val)

    q = request.args.get('q')
    if q:
        like = f'%{q}%'
        conds.append('(i.item_topic LIKE ? OR i.description LIKE ? OR i.actions LIKE ?)')
        params += [like, like, like]

    # 제목(ITEM) 정확 일치 — 요약 링크에서 해당 이슈만 보기 위함
    item_exact = request.args.get('item_topic')
    if item_exact:
        conds.append('i.item_topic = ?')
        params.append(item_exact)

    # 선종 필터 (vessels.vessel_type JOIN 기준)
    vt = request.args.get('vessel_type')
    if vt:
        conds.append('v.vessel_type = ?')
        params.append(vt)

    sql = f'''
        SELECT i.*,
               s.name       AS supervisor_name,
               s.color      AS supervisor_color,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               (SELECT COUNT(*) FROM attachments a WHERE a.issue_id = i.id) AS att_count
          FROM issues i
          JOIN supervisors s ON s.id = i.supervisor_id
          JOIN vessels     v ON v.id = i.vessel_id
         WHERE {' AND '.join(conds)}
         ORDER BY i.issue_date ASC, i.id ASC
    '''
    rows = [_issue_to_dict(r) for r in query(sql, params)]
    return jsonify(rows)


def _issue_to_dict(row):
    d = dict(row)
    try:
        d['actions'] = json.loads(d['actions']) if d.get('actions') else []
    except Exception:
        d['actions'] = []
    return d


# ─────────────────────────────────────────────────────────────────
#  Daily 업무관리 — Excel 추출 (정형 템플릿)
#   · 화면 구조 그대로 재현: 감독 시트 → 제목 → 컬럼 헤더 →
#     월 그룹 헤더 → 일 그룹 헤더 → 데이터 행
#   · Excel의 행 그룹(outline) 기능으로 월·일 단위 접기/펼치기 가능
#   · 컬럼 헤더 행에 AutoFilter 적용 → 선박명 등 자유롭게 필터
#   · 현재 화면 필터(상태/우선순위/선박/선종/검색어/서브탭) 그대로 반영
# ─────────────────────────────────────────────────────────────────
@app.route('/api/issues/export')
@login_required
def api_issue_export():
    from io import BytesIO
    from datetime import datetime
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({'error': 'openpyxl 미설치 — 서버에 pip install openpyxl 필요'}), 500
    from flask import send_file

    # ── 1) 화면 필터와 동일한 조건 ──────────────────────────────
    conds, params = ['1=1'], []
    for key, col in [('supervisor_id', 'i.supervisor_id'),
                     ('vessel_id',     'i.vessel_id'),
                     ('status',        'i.status'),
                     ('priority',      'i.priority')]:
        val = request.args.get(key)
        if val:
            conds.append(f'{col} = ?')
            params.append(val)

    status_in = request.args.get('status_in')
    if status_in:
        vals = [v.strip() for v in status_in.split(',') if v.strip()]
        if vals:
            placeholders = ','.join('?' for _ in vals)
            conds.append(f'i.status IN ({placeholders})')
            params += vals

    q = request.args.get('q')
    if q:
        like = f'%{q}%'
        conds.append('(i.item_topic LIKE ? OR i.description LIKE ? OR i.actions LIKE ?)')
        params += [like, like, like]

    vt = request.args.get('vessel_type')
    if vt:
        conds.append('v.vessel_type = ?')
        params.append(vt)

    sql = f'''
        SELECT i.*,
               s.id            AS sv_id,
               s.name          AS supervisor_name,
               s.display_order AS sv_order,
               v.name          AS vessel_name
          FROM issues i
          JOIN supervisors s ON s.id = i.supervisor_id
          JOIN vessels     v ON v.id = i.vessel_id
         WHERE {' AND '.join(conds)}
         ORDER BY s.display_order ASC, s.id ASC,
                  i.issue_date ASC, i.id ASC
    '''
    rows = [_issue_to_dict(r) for r in query(sql, params)]

    EN = (request.args.get('lang') == 'en')
    if EN:
        _translate_rows_en(rows)

    # ── 2) 감독 → 월 → 일 → 이슈 (4단 그룹핑) ───────────────────
    sv_map  = {}   # sv_name -> {'order': sv_order, 'months': OrderedDict}
    sv_seq  = []
    for r in rows:
        sn = r['supervisor_name']
        if sn not in sv_map:
            sv_map[sn] = {'order': r.get('sv_order') or 0, 'months': {}}
            sv_seq.append(sn)
        d = r.get('issue_date') or ''
        ym = d[:7] if len(d) >= 7 else '날짜 미정'
        months = sv_map[sn]['months']
        if ym not in months:
            months[ym] = {}
        days = months[ym]
        dkey = d if d else '날짜 미정'
        if dkey not in days:
            days[dkey] = []
        days[dkey].append(r)

    # ── 3) 스타일 정의 ──────────────────────────────────────────
    HEADERS = (['NO.', 'Issue Date', 'Due Date', 'Vessel', 'ITEM',
                'DESCRIPTION', 'ACTION PLAN', 'Priority', 'Status', 'Prepared By']
               if EN else
               ['NO.', '작성일', '마감일', '선박명', 'ITEM',
                'DESCRIPTION', 'ACTION PLAN', '우선순위', '상태', '작성자'])
    COL_WIDTHS = [5, 12, 12, 22, 28, 38, 42, 13, 11, 11]
    N_COLS = len(HEADERS)

    F = 'Malgun Gothic'   # Windows 환경의 한글 폰트, macOS도 대체 잘 됨
    title_font   = Font(name=F, size=14, bold=True, color='FFFFFF')
    sub_font     = Font(name=F, size=10, color='ECF0F1', italic=True)
    title_fill   = PatternFill('solid', start_color='1F3A5F')   # 짙은 네이비
    sub_fill     = PatternFill('solid', start_color='2C5282')

    col_hdr_font = Font(name=F, size=10, bold=True, color='FFFFFF')
    col_hdr_fill = PatternFill('solid', start_color='34495E')   # 슬레이트

    month_font   = Font(name=F, size=11, bold=True, color='FFFFFF')
    month_fill   = PatternFill('solid', start_color='7F8C8D')   # 미디엄 그레이

    day_font     = Font(name=F, size=10, bold=True, color='2C3E50')
    day_fill     = PatternFill('solid', start_color='D5DBDB')   # 라이트 그레이

    body_font    = Font(name=F, size=10)
    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    body_align   = Alignment(horizontal='left',   vertical='top',    wrap_text=True)
    cent_top     = Alignment(horizontal='center', vertical='top',    wrap_text=True)
    left_mid     = Alignment(horizontal='left',   vertical='center', wrap_text=False)

    thin = Side(style='thin',   color='BDC3C7')
    med  = Side(style='medium', color='34495E')
    border_thin = Border(left=thin, right=thin, top=thin, bottom=thin)

    PRI_FILL = {
        'COC & Flag': PatternFill('solid', start_color='F8CECC'),
        'Urgent':     PatternFill('solid', start_color='FFE6CC'),
        'Next DD':    PatternFill('solid', start_color='FFF2CC'),
        'Normal':     None,
    }
    PRI_FONT = {
        'COC & Flag': Font(name=F, size=10, bold=True, color='B71C1C'),
        'Urgent':     Font(name=F, size=10, bold=True, color='E65100'),
        'Next DD':    Font(name=F, size=10, bold=True, color='6D4C0F'),
        'Normal':     Font(name=F, size=10, color='5D6D7E'),
    }
    STAT_FILL = {
        'Open':       PatternFill('solid', start_color='E1F5FE'),
        'InProgress': PatternFill('solid', start_color='FFF9C4'),
        'Closed':     PatternFill('solid', start_color='E8F5E9'),
    }
    STAT_FONT = {
        'Open':       Font(name=F, size=10, bold=True, color='0277BD'),
        'InProgress': Font(name=F, size=10, bold=True, color='F57F17'),
        'Closed':     Font(name=F, size=10, bold=True, color='2E7D32'),
    }
    STAT_LABEL = ({'Open': 'Open', 'InProgress': 'In Progress', 'Closed': 'Closed'}
                  if EN else
                  {'Open': 'Open', 'InProgress': '진행중', 'Closed': 'Closed'})

    def _sheet_safe(name):
        bad = '[]:*?/\\'
        out = ''.join('_' if c in bad else c for c in name)
        return (out[:31] or 'Sheet')

    def _fmt_actions(acts):
        if not acts:
            return ''
        lines = []
        for a in acts:
            d = (a.get('date') or '').strip()
            p = (a.get('progress') or '').strip()
            mark = '★ ' if a.get('important') else ''
            if d and p:   lines.append(f'{mark}[{d}] {p}')
            elif d:       lines.append(f'{mark}[{d}]')
            elif p:       lines.append(f'{mark}{p}')
        return '\n'.join(lines)

    def _ko_month(ym):
        if ym == '날짜 미정':
            return 'Date TBD' if EN else '날짜 미정'
        try:
            y, m = ym.split('-')
            if EN:
                import calendar
                return f'{calendar.month_abbr[int(m)]} {y}'
            return f'{y}년 {int(m)}월'
        except Exception:
            return ym

    # ── 4) Workbook 생성 ────────────────────────────────────────
    wb = Workbook()
    wb.remove(wb.active)

    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    # 현재 사용자 (서명용)
    me = session.get('display_name') or session.get('username') or ''

    # 화면 필터 요약 (제목 영역에 노출)
    sub_chips = []
    if status_in:
        sub_chips.append(('Filter: ' if EN else '필터: ') + status_in.replace(',', ' / '))
    elif request.args.get('status'):
        sub_chips.append(('Status: ' if EN else '상태: ') + request.args.get('status'))
    if request.args.get('priority'):
        sub_chips.append(('Priority: ' if EN else '우선순위: ') + request.args.get('priority'))
    if request.args.get('vessel_type'):
        sub_chips.append(('Vessel Type: ' if EN else '선종: ') + request.args.get('vessel_type'))
    if request.args.get('vessel_id'):
        vname = query('SELECT name FROM vessels WHERE id=?',
                      (request.args.get('vessel_id'),), one=True)
        if vname: sub_chips.append(('Vessel: ' if EN else '선박: ') + vname['name'])
    if request.args.get('q'):
        sub_chips.append(('Search: ' if EN else '검색: ') + request.args.get('q'))
    sub_text = ' | '.join(sub_chips) if sub_chips else ('All items' if EN else '전체 항목')

    if not sv_seq:
        ws = wb.create_sheet('데이터 없음')
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N_COLS)
        c = ws.cell(row=1, column=1, value='Daily 업무관리 — 데이터 없음')
        c.font = title_font
        c.fill = title_fill
        c.alignment = center_align
        ws.cell(row=3, column=1,
                value='필터 조건에 해당하는 이슈가 없습니다.').font = Font(name=F, size=11, italic=True)
        for idx, w in enumerate(COL_WIDTHS, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w
    else:
        for sn in sv_seq:
            ws = wb.create_sheet(_sheet_safe(sn))
            months = sv_map[sn]['months']

            # 컬럼 너비
            for idx, w in enumerate(COL_WIDTHS, start=1):
                ws.column_dimensions[get_column_letter(idx)].width = w

            # ── 4-1) 제목 영역 (행1-2) ──
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N_COLS)
            c1 = ws.cell(row=1, column=1,
                         value=(f'Daily Work Log   |   {sn}' if EN else f'Daily 업무관리   |   {sn}'))
            c1.font = title_font
            c1.fill = title_fill
            c1.alignment = Alignment(horizontal='left', vertical='center', indent=1)
            ws.row_dimensions[1].height = 30

            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=N_COLS)
            total_cnt = sum(len(v) for m in months.values() for v in m.values())
            if EN:
                sub_msg = f'Exported: {today_str}    │    Total {total_cnt}    │    {sub_text}'
                if me:
                    sub_msg += f'    │    By: {me}'
            else:
                sub_msg = f'추출일: {today_str}    │    총 {total_cnt}건    │    {sub_text}'
                if me:
                    sub_msg += f'    │    출력: {me}'
            c2 = ws.cell(row=2, column=1, value=sub_msg)
            c2.font = sub_font
            c2.fill = sub_fill
            c2.alignment = Alignment(horizontal='left', vertical='center', indent=1)
            ws.row_dimensions[2].height = 20

            # 행 3: 빈 줄 (시각적 분리)
            ws.row_dimensions[3].height = 6

            # ── 4-2) 컬럼 헤더 (행4) — AutoFilter 시작점 ──
            HDR_ROW = 4
            for col_idx, h in enumerate(HEADERS, start=1):
                c = ws.cell(row=HDR_ROW, column=col_idx, value=h)
                c.font = col_hdr_font
                c.fill = col_hdr_fill
                c.alignment = center_align
                c.border = Border(left=thin, right=thin, top=med, bottom=med)
            ws.row_dimensions[HDR_ROW].height = 26

            # ── 4-3) 본문: 월 → 일 → 데이터 ──
            cur_row = HDR_ROW + 1
            no = 0
            # 월 키 정렬 (날짜 미정은 맨 뒤)
            month_keys = sorted([k for k in months.keys() if k != '날짜 미정'])
            if '날짜 미정' in months:
                month_keys.append('날짜 미정')

            for ym in month_keys:
                days = months[ym]
                m_cnt = sum(len(v) for v in days.values())

                # 월 헤더 행
                ws.merge_cells(start_row=cur_row, start_column=1,
                               end_row=cur_row, end_column=N_COLS)
                mc = ws.cell(row=cur_row, column=1,
                             value=f'▼  {_ko_month(ym)}    ({m_cnt} item{"s" if m_cnt > 1 else ""})')
                mc.font = month_font
                mc.fill = month_fill
                mc.alignment = left_mid
                ws.row_dimensions[cur_row].height = 22
                # 월 헤더 자체에도 outline level 0 (접기 기준점)
                cur_row += 1

                day_keys = sorted([k for k in days.keys() if k != '날짜 미정'])
                if '날짜 미정' in days:
                    day_keys.append('날짜 미정')

                for dkey in day_keys:
                    items = days[dkey]
                    dlabel = ('Date TBD' if EN else '날짜 미정') if dkey == '날짜 미정' else dkey
                    # 일 헤더 행
                    ws.merge_cells(start_row=cur_row, start_column=1,
                                   end_row=cur_row, end_column=N_COLS)
                    dc = ws.cell(row=cur_row, column=1,
                                 value=f'   ▸  {dlabel}   ({len(items)} item{"s" if len(items)>1 else ""})')
                    dc.font = day_font
                    dc.fill = day_fill
                    dc.alignment = left_mid
                    ws.row_dimensions[cur_row].height = 19
                    # 일 헤더는 outline level 1 (월 단위로 접으면 같이 사라짐)
                    ws.row_dimensions[cur_row].outline_level = 1
                    cur_row += 1

                    # 데이터 행
                    for r in items:
                        no += 1
                        vals = [
                            no,
                            r.get('issue_date') or '',
                            r.get('due_date') or '',
                            r.get('vessel_name') or '',
                            r.get('item_topic') or '',
                            r.get('description') or '',
                            _fmt_actions(r.get('actions')),
                            r.get('priority') or '',
                            STAT_LABEL.get(r.get('status'), r.get('status') or ''),
                            r.get('created_by') or '',
                        ]
                        for col_idx, v in enumerate(vals, start=1):
                            c = ws.cell(row=cur_row, column=col_idx, value=v)
                            c.font = body_font
                            c.border = border_thin
                            if col_idx in (1, 2, 3, 10):
                                c.alignment = cent_top
                            elif col_idx == 4:
                                c.alignment = Alignment(horizontal='left',
                                                        vertical='top', wrap_text=True)
                            elif col_idx in (8, 9):
                                c.alignment = center_align
                            else:
                                c.alignment = body_align

                        # 우선순위 / 상태 색상
                        pri = r.get('priority')
                        pf = PRI_FILL.get(pri)
                        if pf:
                            ws.cell(row=cur_row, column=8).fill = pf
                        if pri in PRI_FONT:
                            ws.cell(row=cur_row, column=8).font = PRI_FONT[pri]

                        st = r.get('status')
                        sf = STAT_FILL.get(st)
                        if sf:
                            ws.cell(row=cur_row, column=9).fill = sf
                        if st in STAT_FONT:
                            ws.cell(row=cur_row, column=9).font = STAT_FONT[st]

                        # 데이터 행은 outline level 2 (일/월 단위 접기 모두에 영향)
                        ws.row_dimensions[cur_row].outline_level = 2
                        cur_row += 1

            # ── 4-4) AutoFilter — 컬럼 헤더부터 마지막 데이터까지 ──
            last_col = get_column_letter(N_COLS)
            last_row = cur_row - 1
            if last_row > HDR_ROW:
                ws.auto_filter.ref = f'A{HDR_ROW}:{last_col}{last_row}'

            # ── 4-5) Freeze panes — 컬럼 헤더 행 아래 고정 ──
            ws.freeze_panes = f'A{HDR_ROW + 1}'

            # outline 방향: 요약(부모) 행이 위에 있으므로 summary_below=False
            ws.sheet_properties.outlinePr.summaryBelow = False
            ws.sheet_properties.outlinePr.summaryRight = False

            # 인쇄 설정
            ws.print_options.horizontalCentered = True
            ws.page_setup.orientation = 'landscape'
            ws.page_setup.fitToWidth  = 1
            ws.page_setup.fitToHeight = 0
            ws.sheet_properties.pageSetUpPr.fitToPage = True
            ws.print_title_rows = f'{HDR_ROW}:{HDR_ROW}'  # 컬럼 헤더는 매 페이지 반복

    # ── 5) 파일명 ──
    today = now.strftime('%Y%m%d')
    suffix = '_EN' if EN else ''
    if len(sv_seq) == 1:
        fname = f'TRMT_Daily_{_sheet_safe(sv_seq[0])}_{today}{suffix}.xlsx'
    else:
        fname = f'TRMT_Daily_{today}{suffix}.xlsx'

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(
        bio,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=fname,
    )


def _gen_summary_rows(supervisor_id=None):
    """해당 스코프(특정 감독 또는 전체)의 모든 이슈(진행중+완료)를 Gemini 요약하여
    [{no, vessel_name, issue, priority, status}] 반환."""
    conds, params = ['1=1'], []
    if supervisor_id:
        conds.append('i.supervisor_id = ?'); params.append(supervisor_id)
    sql = f'''
        SELECT i.*, s.display_order AS sv_order, v.name AS vessel_name,
               v.vessel_type AS vessel_type
          FROM issues i
          JOIN supervisors s ON s.id = i.supervisor_id
          JOIN vessels     v ON v.id = i.vessel_id
         WHERE {' AND '.join(conds)}
         ORDER BY s.display_order ASC, s.id ASC, i.issue_date ASC, i.id ASC
    '''
    rows = [_issue_to_dict(r) for r in query(sql, params)]
    payload = [{'i': idx,
                'description': r.get('description') or '',
                'action': _latest_action_progress(r.get('actions'))}
               for idx, r in enumerate(rows)]
    summaries = _gen_issue_summaries(payload)
    STAT = {'Open': 'Open', 'InProgress': '진행중', 'Closed': 'Closed'}
    out = []
    for idx, r in enumerate(rows):
        s = summaries.get(idx, {})
        desc = s.get('desc') or (r.get('description') or '').strip().split('\n')[0]
        ad, araw = _latest_action(r.get('actions'))
        action = s.get('action') or araw
        head = f"{_md_label(r.get('issue_date') or '')} {r.get('item_topic') or ''}".strip()
        lines = [head]
        if desc:
            lines.append(f'1) {desc}')
        if action:
            md = _md_label(ad)
            lines.append(f'2) {md} {action}'.strip() if md else f'2) {action}')
        out.append({'no': idx + 1,
                    'issue_id': r.get('id'),
                    'item': r.get('item_topic') or '',
                    'supervisor_id': r.get('supervisor_id'),
                    'vessel_id': r.get('vessel_id'),
                    'vessel_name': r.get('vessel_name') or '',
                    'vessel_type': r.get('vessel_type') or '',
                    'issue': '\n'.join(lines),
                    'priority': r.get('priority') or '',
                    'status_raw': r.get('status') or '',
                    'status': STAT.get(r.get('status'), r.get('status') or '')})
    return out


def _ensure_summary_table():
    execute("""CREATE TABLE IF NOT EXISTS issue_summaries (
                 scope TEXT PRIMARY KEY, data TEXT, generated_at TEXT )""")


def _summary_scope():
    sid = request.args.get('supervisor_id')
    return str(sid) if sid else 'all'


@app.route('/api/issues/summary', methods=['GET'])
@login_required
def api_issue_summary_get():
    _ensure_summary_table()
    row = query('SELECT data, generated_at FROM issue_summaries WHERE scope=?',
                (_summary_scope(),), one=True)
    if not row:
        return jsonify({'rows': [], 'generated_at': None, 'count': 0})
    try:
        rows = json.loads(row['data'])
    except Exception:
        rows = []
    return jsonify({'rows': rows, 'generated_at': row['generated_at'], 'count': len(rows)})


def _run_summary_generate(sid=None):
    """업무요약 생성+저장 코어 (UI 버튼·API키 스케줄러 공용). (rows, gen_at, counts) 반환."""
    from datetime import datetime
    _ensure_summary_table()
    rows = _gen_summary_rows(sid)
    gen_at = datetime.now().strftime('%Y-%m-%d %H:%M')

    def _save(scope, scope_rows):
        # scope 내에서 No. 재넘버링
        renum = []
        for i, r in enumerate(scope_rows, start=1):
            rr = dict(r); rr['no'] = i; renum.append(rr)
        execute("INSERT OR REPLACE INTO issue_summaries (scope, data, generated_at) VALUES (?, ?, ?)",
                (scope, json.dumps(renum, ensure_ascii=False), gen_at))
        return len(renum)

    counts = {}
    if sid:
        counts[str(sid)] = _save(str(sid), rows)
    else:
        counts['all'] = _save('all', rows)
        # 감독별로 분리 저장 (각 감독 탭의 요약도 동시 갱신)
        by_sv = {}
        for r in rows:
            by_sv.setdefault(r.get('supervisor_id'), []).append(r)
        all_sv = [s['id'] for s in query('SELECT id FROM supervisors')]
        for sv_id in all_sv:
            counts[str(sv_id)] = _save(str(sv_id), by_sv.get(sv_id, []))
    return rows, gen_at, counts


@app.route('/api/issues/summary-generate', methods=['POST'])
@login_required
def api_issue_summary_generate():
    sid = request.args.get('supervisor_id') or None
    rows, gen_at, counts = _run_summary_generate(sid)
    return jsonify({'rows': rows, 'generated_at': gen_at, 'counts': counts})


@app.route('/api/issues/summary-counts', methods=['GET'])
@login_required
def api_issue_summary_counts():
    _ensure_summary_table()
    out = {}
    for r in query('SELECT scope, data FROM issue_summaries'):
        try:
            out[r['scope']] = len(json.loads(r['data']))
        except Exception:
            out[r['scope']] = 0
    return jsonify(out)


@app.route('/api/issues/summary-export')
@login_required
def api_issue_summary_export():
    """현재 탭(대분류)의 저장된 요약(요약 탭 내용)을 엑셀로 추출 — AI 미사용."""
    from io import BytesIO
    from datetime import datetime
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({'error': 'openpyxl 미설치'}), 500
    from flask import send_file

    # 저장된 요약(요약 탭 내용)을 그대로 사용
    _ensure_summary_table()
    srow = query('SELECT data FROM issue_summaries WHERE scope=?',
                 (_summary_scope(),), one=True)
    rows = []
    if srow:
        try:
            rows = json.loads(srow['data'])
        except Exception:
            rows = []

    def build_cell(idx, r):
        return r.get('issue') or ''

    # ── Workbook ──
    wb = Workbook(); ws = wb.active; ws.title = '업무 요약'
    F = 'Malgun Gothic'
    HEADERS = ['No.', 'Vessel Name', '현안업무', 'Priority', 'Status']
    WIDTHS = [6, 24, 85, 13, 12]
    for idx, w in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    title_fill = PatternFill('solid', start_color='1F3A5F')
    sub_fill   = PatternFill('solid', start_color='2C5282')
    hdr_fill   = PatternFill('solid', start_color='34495E')
    thin = Side(style='thin', color='BBBBBB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    now = datetime.now()
    ws.merge_cells('A1:E1')
    c = ws.cell(row=1, column=1, value='Daily 업무 요약')
    c.font = Font(name=F, size=14, bold=True, color='FFFFFF'); c.fill = title_fill
    c.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.row_dimensions[1].height = 28

    ws.merge_cells('A2:E2')
    me = session.get('display_name') or session.get('username') or ''
    c = ws.cell(row=2, column=1,
                value=f"추출일: {now.strftime('%Y-%m-%d')}    │    총 {len(rows)}건"
                      + (f"    │    {me}" if me else ''))
    c.font = Font(name=F, size=10, italic=True, color='ECF0F1'); c.fill = sub_fill
    c.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.row_dimensions[2].height = 18
    ws.row_dimensions[3].height = 6

    HDR = 4
    for ci, h in enumerate(HEADERS, start=1):
        cc = ws.cell(row=HDR, column=ci, value=h)
        cc.font = Font(name=F, size=11, bold=True, color='FFFFFF'); cc.fill = hdr_fill
        cc.alignment = Alignment(horizontal='center', vertical='center')
        cc.border = border
    ws.row_dimensions[HDR].height = 24

    body = Font(name=F, size=10)
    top_wrap = Alignment(horizontal='left', vertical='top', wrap_text=True)
    center = Alignment(horizontal='center', vertical='center')
    STAT_LABEL = {'Open': 'Open', 'InProgress': '진행중', 'Closed': 'Closed'}
    r_idx = HDR + 1
    for n, r in enumerate(rows, start=1):
        ws.cell(row=r_idx, column=1, value=n).alignment = center
        ws.cell(row=r_idx, column=1).font = body
        ws.cell(row=r_idx, column=2, value=r.get('vessel_name') or '')
        ws.cell(row=r_idx, column=2).alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
        ws.cell(row=r_idx, column=2).font = body
        cell = ws.cell(row=r_idx, column=3, value=build_cell(n - 1, r))
        cell.alignment = top_wrap; cell.font = body
        # D열 Priority, E열 Status
        pc = ws.cell(row=r_idx, column=4, value=r.get('priority') or '')
        pc.alignment = center; pc.font = body
        sc = ws.cell(row=r_idx, column=5,
                     value=STAT_LABEL.get(r.get('status'), r.get('status') or ''))
        sc.alignment = center; sc.font = body
        for ci in range(1, 6):
            ws.cell(row=r_idx, column=ci).border = border
        # 줄 수에 맞춰 행 높이 살짝 키움
        n_lines = (build_cell(n - 1, r).count('\n') + 1)
        ws.row_dimensions[r_idx].height = max(34, 15 * n_lines + 6)
        r_idx += 1

    ws.freeze_panes = f'A{HDR + 1}'
    if r_idx - 1 > HDR:
        ws.auto_filter.ref = f'A{HDR}:E{r_idx - 1}'
    ws.print_options.horizontalCentered = True
    ws.page_setup.orientation = 'portrait'
    ws.page_setup.fitToWidth = 1; ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f'{HDR}:{HDR}'

    scope = _summary_scope()
    tag = ''
    if scope != 'all':
        sv = query('SELECT name FROM supervisors WHERE id=?', (scope,), one=True)
        if sv:
            tag = '_' + _safe_filename(sv['name'])
    fname = f"TRMT_업무요약{tag}_{now.strftime('%Y%m%d')}.xlsx"
    bio = BytesIO(); wb.save(bio); bio.seek(0)
    return send_file(
        bio,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True, download_name=fname)


@app.route('/api/issues/<int:iid>')
@login_required
def api_issue_get(iid):
    r = query('''
        SELECT i.*,
               s.name       AS supervisor_name,
               s.color      AS supervisor_color,
               v.name       AS vessel_name,
               v.short_name AS vessel_short
          FROM issues i
          JOIN supervisors s ON s.id = i.supervisor_id
          JOIN vessels     v ON v.id = i.vessel_id
         WHERE i.id = ?
    ''', (iid,), one=True)
    if not r:
        abort(404)
    out = _issue_to_dict(r)
    out['attachments'] = [dict(a) for a in query(
        'SELECT id, filename, stored_name, file_size, mime_type, uploaded_at '
        'FROM attachments WHERE issue_id=? ORDER BY id', (iid,))]
    return jsonify(out)


@app.route('/api/issues', methods=['POST'])
@login_required
def api_issue_create():
    d = request.get_json(silent=True) or {}
    for k in ('supervisor_id', 'vessel_id', 'issue_date', 'item_topic'):
        if not d.get(k):
            return jsonify({'error': f'필수 항목 누락: {k}'}), 400

    actions = d.get('actions') or []
    if not isinstance(actions, list):
        actions = []
    actions_json = json.dumps(actions, ensure_ascii=False)

    iid = execute('''
        INSERT INTO issues
            (supervisor_id, vessel_id, issue_date, due_date,
             item_topic, description, actions,
             priority, status, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        d['supervisor_id'], d['vessel_id'], d['issue_date'],
        d.get('due_date') or None,
        d['item_topic'],
        d.get('description') or '',
        actions_json,
        d.get('priority') or 'Normal',
        d.get('status')   or 'Open',
        session.get('username'),
    ))
    return jsonify({'id': iid}), 201


@app.route('/api/issues/<int:iid>', methods=['PUT'])
@login_required
def api_issue_update(iid):
    if not query('SELECT id FROM issues WHERE id=?', (iid,), one=True):
        abort(404)
    d = request.get_json(silent=True) or {}
    fields = ['supervisor_id', 'vessel_id', 'issue_date', 'due_date',
              'item_topic',    'description', 'actions',
              'priority',      'status']
    sets, params = [], []
    for f in fields:
        if f in d:
            val = d[f]
            if f == 'actions':
                if not isinstance(val, list):
                    val = []
                val = json.dumps(val, ensure_ascii=False)
            elif val == '':
                val = None
            sets.append(f'{f} = ?')
            params.append(val)
    if not sets:
        return jsonify({'error': '수정할 필드가 없습니다.'}), 400
    sets.append('updated_at = datetime("now","localtime")')
    params.append(iid)
    execute(f'UPDATE issues SET {", ".join(sets)} WHERE id = ?', params)
    return jsonify({'id': iid})


@app.route('/api/issues/<int:iid>', methods=['DELETE'])
@login_required
def api_issue_delete(iid):
    atts = query('SELECT stored_name FROM attachments WHERE issue_id=?', (iid,))
    for a in atts:
        p = os.path.join(UPLOAD_DIR, a['stored_name'])
        if os.path.exists(p):
            os.remove(p)
    execute('DELETE FROM issues WHERE id=?', (iid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — admin: supervisors / vessels / users
# ═════════════════════════════════════════════════════════════════

# ----- 감독 (CREATE / UPDATE / DELETE) -----
@app.route('/api/supervisors', methods=['POST'])
@admin_required
def api_supervisor_create():
    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': '감독명은 필수입니다.'}), 400
    if query('SELECT id FROM supervisors WHERE name=?', (name,), one=True):
        return jsonify({'error': '이미 존재하는 감독명입니다.'}), 400
    max_order = query('SELECT COALESCE(MAX(display_order),0)+1 AS n FROM supervisors',
                      one=True)['n']
    sid = execute('''
        INSERT INTO supervisors (name, color, display_order, email, active)
        VALUES (?, ?, ?, ?, 1)
    ''', (name, d.get('color') or 'blue',
          d.get('display_order') or max_order,
          d.get('email') or ''))
    return jsonify({'id': sid}), 201


@app.route('/api/supervisors/<int:sid>', methods=['PUT'])
@admin_required
def api_supervisor_update(sid):
    if not query('SELECT id FROM supervisors WHERE id=?', (sid,), one=True):
        abort(404)
    d = request.get_json(silent=True) or {}
    sets, params = [], []
    for f in ('name', 'color', 'display_order', 'email', 'active'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f])
    if not sets:
        return jsonify({'error': '수정할 필드 없음'}), 400
    params.append(sid)
    execute(f'UPDATE supervisors SET {", ".join(sets)} WHERE id = ?', params)
    return jsonify({'id': sid})


@app.route('/api/supervisors/<int:sid>', methods=['DELETE'])
@admin_required
def api_supervisor_delete(sid):
    # 이슈 있으면 soft delete 만 수행
    n = query('SELECT COUNT(*) AS n FROM issues WHERE supervisor_id=?',
              (sid,), one=True)['n']
    if n > 0:
        execute('UPDATE supervisors SET active=0 WHERE id=?', (sid,))
        return jsonify({'ok': True, 'soft_delete': True, 'issues': n})
    # Hard delete: FK 해제 먼저
    execute('UPDATE users SET supervisor_id=NULL WHERE supervisor_id=?', (sid,))
    execute('DELETE FROM supervisor_vessels WHERE supervisor_id=?', (sid,))
    execute('DELETE FROM supervisors WHERE id=?', (sid,))
    return jsonify({'ok': True})


# ----- 선박 (CREATE / UPDATE / DELETE / 전체 조회) -----
@app.route('/api/vessels/all')
@login_required
def api_vessels_all():
    """관리 UI용 — 담당 감독 함께."""
    rows = query('''
        SELECT v.*,
          (SELECT GROUP_CONCAT(s.name, ', ')
             FROM supervisor_vessels sv
             JOIN supervisors s ON s.id = sv.supervisor_id
            WHERE sv.vessel_id = v.id) AS supervisor_names,
          (SELECT GROUP_CONCAT(s.id)
             FROM supervisor_vessels sv
             JOIN supervisors s ON s.id = sv.supervisor_id
            WHERE sv.vessel_id = v.id) AS supervisor_ids_csv
          FROM vessels v
         ORDER BY v.active DESC, v.name
    ''')
    out = []
    for r in rows:
        d = dict(r)
        d['supervisor_ids'] = [int(x) for x in (d.pop('supervisor_ids_csv') or '').split(',') if x]
        out.append(d)
    return jsonify(out)


@app.route('/api/vessels', methods=['POST'])
@login_required
def api_vessel_create():
    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': '선박명은 필수입니다.'}), 400
    if query('SELECT id FROM vessels WHERE name=?', (name,), one=True):
        return jsonify({'error': '이미 존재하는 선박명입니다.'}), 400

    sids = [int(x) for x in (d.get('supervisor_ids') or [])]

    # 일반 사용자(member) 권한 제약:
    #   - 반드시 본인의 감독 1명에게만 연결 가능
    #   - 다른 감독이나 복수 감독, 미할당은 불가
    if session.get('role') != 'admin':
        my_sup = session.get('supervisor_id')
        if not my_sup:
            return jsonify({'error': '담당 감독이 연결되지 않은 계정입니다. 관리자에게 요청하세요.'}), 403
        if sids != [my_sup]:
            return jsonify({'error': '본인 담당 감독으로만 선박을 추가할 수 있습니다.'}), 403

    vid = execute('''
        INSERT INTO vessels (name, short_name, vessel_type, imo, class_society, active)
        VALUES (?, ?, ?, ?, ?, 1)
    ''', (name,
          (d.get('short_name') or name[:12]).strip(),
          d.get('vessel_type') or '',
          d.get('imo') or '',
          d.get('class_society') or ''))
    for sid in sids:
        execute('INSERT OR IGNORE INTO supervisor_vessels (vessel_id, supervisor_id) VALUES (?, ?)',
                (vid, sid))
    return jsonify({'id': vid}), 201


@app.route('/api/vessels/<int:vid>', methods=['PUT'])
@login_required
def api_vessel_update(vid):
    if not query('SELECT id FROM vessels WHERE id=?', (vid,), one=True):
        abort(404)
    d = request.get_json(silent=True) or {}

    # 일반 사용자(member) 권한 제약:
    #   - 본인 담당 감독에 연결된 선박만 수정 가능
    #   - 담당 감독 변경(supervisor_ids), 비활성화(active) 는 불가
    if session.get('role') != 'admin':
        my_sup = session.get('supervisor_id')
        if not my_sup:
            return jsonify({'error': '담당 감독이 연결되지 않은 계정입니다.'}), 403
        owned = query(
            'SELECT 1 FROM supervisor_vessels WHERE vessel_id=? AND supervisor_id=?',
            (vid, my_sup), one=True,
        )
        if not owned:
            return jsonify({'error': '본인 담당 선박만 수정할 수 있습니다.'}), 403
        # 민감 필드는 서버에서 무시 (이중 방어)
        d.pop('supervisor_ids', None)
        d.pop('active', None)

    sets, params = [], []
    for f in ('name', 'short_name', 'vessel_type', 'imo', 'class_society', 'active'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f])
    if sets:
        params.append(vid)
        execute(f'UPDATE vessels SET {", ".join(sets)} WHERE id = ?', params)
    # supervisor 매핑 갱신 (admin만 가능 — member는 위에서 pop됨)
    if 'supervisor_ids' in d:
        execute('DELETE FROM supervisor_vessels WHERE vessel_id = ?', (vid,))
        for sid in (d.get('supervisor_ids') or []):
            execute('INSERT OR IGNORE INTO supervisor_vessels (vessel_id, supervisor_id) VALUES (?, ?)',
                    (vid, int(sid)))
    return jsonify({'id': vid})


@app.route('/api/vessels/<int:vid>', methods=['DELETE'])
@login_required
def api_vessel_delete(vid):
    if not query('SELECT id FROM vessels WHERE id=?', (vid,), one=True):
        abort(404)

    # 일반 사용자(member) 권한 제약:
    #   - 본인 담당 선박만 삭제 가능
    #   - 다른 감독에게도 공유된 선박 → 본인 담당만 제거 (선박 자체는 유지)
    #   - 본인만 담당 → 아래 공통 로직으로 진행 (이슈 있으면 soft, 없으면 hard)
    if session.get('role') != 'admin':
        my_sup = session.get('supervisor_id')
        if not my_sup:
            return jsonify({'error': '담당 감독이 연결되지 않은 계정입니다.'}), 403
        owned = query(
            'SELECT 1 FROM supervisor_vessels WHERE vessel_id=? AND supervisor_id=?',
            (vid, my_sup), one=True,
        )
        if not owned:
            return jsonify({'error': '본인 담당 선박만 삭제할 수 있습니다.'}), 403
        # 다른 감독도 담당하는지?
        other = query(
            'SELECT COUNT(*) AS n FROM supervisor_vessels WHERE vessel_id=? AND supervisor_id<>?',
            (vid, my_sup), one=True,
        )
        if other['n'] > 0:
            # 본인 담당만 해제하고 종료
            execute('DELETE FROM supervisor_vessels WHERE vessel_id=? AND supervisor_id=?',
                    (vid, my_sup))
            return jsonify({'ok': True, 'unassigned_only': True})

    # 이슈가 있으면 soft delete
    n = query('SELECT COUNT(*) AS n FROM issues WHERE vessel_id=?',
              (vid,), one=True)['n']
    if n > 0:
        execute('UPDATE vessels SET active=0 WHERE id=?', (vid,))
        return jsonify({'ok': True, 'soft_delete': True, 'issues': n})
    execute('DELETE FROM supervisor_vessels WHERE vessel_id=?', (vid,))
    execute('DELETE FROM vessels WHERE id=?', (vid,))
    return jsonify({'ok': True})


# ----- 사용자 (admin 전용 CRUD) -----
@app.route('/api/users')
@admin_required
def api_users_list():
    rows = query('''
        SELECT u.id, u.username, u.display_name, u.role, u.supervisor_id, u.active,
               u.created_at, u.last_login_at,
               s.name AS supervisor_name
          FROM users u
          LEFT JOIN supervisors s ON s.id = u.supervisor_id
         ORDER BY u.active DESC, u.role DESC, u.id
    ''')
    return jsonify([dict(r) for r in rows])


@app.route('/api/users', methods=['POST'])
@admin_required
def api_user_create():
    d = request.get_json(silent=True) or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    if not username:
        return jsonify({'error': '사용자명은 필수입니다.'}), 400
    if len(password) < 6:
        return jsonify({'error': '비밀번호는 6자 이상이어야 합니다.'}), 400
    if query('SELECT id FROM users WHERE username=?', (username,), one=True):
        return jsonify({'error': '이미 사용 중인 사용자명입니다.'}), 400
    role = d.get('role') or 'member'
    if role not in ('admin', 'member'):
        role = 'member'
    uid = execute('''
        INSERT INTO users (username, password_hash, display_name, role, supervisor_id, active)
        VALUES (?, ?, ?, ?, ?, 1)
    ''', (username, generate_password_hash(password),
          d.get('display_name') or username,
          role,
          d.get('supervisor_id') or None))
    return jsonify({'id': uid}), 201


@app.route('/api/users/<int:uid>', methods=['PUT'])
@admin_required
def api_user_update(uid):
    if not query('SELECT id FROM users WHERE id=?', (uid,), one=True):
        abort(404)
    d = request.get_json(silent=True) or {}
    sets, params = [], []
    for f in ('display_name', 'role', 'supervisor_id', 'active'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f])
    if not sets:
        return jsonify({'error': '수정할 필드 없음'}), 400
    params.append(uid)
    execute(f'UPDATE users SET {", ".join(sets)} WHERE id = ?', params)
    return jsonify({'id': uid})


@app.route('/api/users/<int:uid>', methods=['DELETE'])
@admin_required
def api_user_delete(uid):
    if uid == session.get('user_id'):
        return jsonify({'error': '자기 자신은 삭제할 수 없습니다.'}), 400
    # admin 계정이 하나만 남을 땐 삭제 금지
    u = query('SELECT role FROM users WHERE id=?', (uid,), one=True)
    if not u:
        abort(404)
    if u['role'] == 'admin':
        n = query("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND id<>?",
                  (uid,), one=True)['n']
        if n == 0:
            return jsonify({'error': '최소 1명의 관리자 계정은 유지되어야 합니다.'}), 400
    execute('UPDATE users SET active=0 WHERE id=?', (uid,))
    return jsonify({'ok': True})


@app.route('/api/users/<int:uid>/password', methods=['POST'])
@admin_required
def api_user_reset_password(uid):
    d = request.get_json(silent=True) or {}
    new = d.get('new_password') or ''
    if len(new) < 6:
        return jsonify({'error': '비밀번호는 6자 이상이어야 합니다.'}), 400
    if not query('SELECT id FROM users WHERE id=?', (uid,), one=True):
        abort(404)
    execute('UPDATE users SET password_hash=? WHERE id=?',
            (generate_password_hash(new), uid))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — Condition Survey
# ═════════════════════════════════════════════════════════════════

def _cs_survey_with_counts(s):
    """단일 survey에 카운트 컬럼들 포함시켜 반환 (dict).
    manual_*_count 가 NULL이 아니면 수동 입력값을 우선."""
    sid = s['id']
    rows = query("""
        SELECT category, status, COUNT(*) AS n
          FROM cs_findings
         WHERE survey_id = ?
         GROUP BY category, status
    """, (sid,))
    def_open = def_closed = obs_open = obs_closed = 0
    for r in rows:
        if r['category'] == 'Defect':
            if r['status'] == 'Closed': def_closed = r['n']
            else: def_open = r['n']
        else:
            if r['status'] == 'Closed': obs_closed = r['n']
            else: obs_open = r['n']
    auto_def   = def_open + def_closed
    auto_obs   = obs_open + obs_closed
    auto_close = def_closed + obs_closed

    d = dict(s)
    # 수동 override가 있으면 그 값을, 없으면 자동 카운트
    d['defect_count']      = s['manual_defect_count']      if s['manual_defect_count']      is not None else auto_def
    d['observation_count'] = s['manual_observation_count'] if s['manual_observation_count'] is not None else auto_obs
    d['close_count']       = s['manual_close_count']       if s['manual_close_count']       is not None else auto_close
    d['total_count']       = d['defect_count'] + d['observation_count']
    # Open 카운트는 항상 자동 (전체 - 완료)
    d['open_count']        = max(0, d['total_count'] - d['close_count'])
    # manual flag (UI에서 자동/수동 구분)
    d['defect_manual']      = s['manual_defect_count']      is not None
    d['observation_manual'] = s['manual_observation_count'] is not None
    d['close_manual']       = s['manual_close_count']       is not None
    # 첨부 카운트
    ar = query('SELECT COUNT(*) AS n FROM cs_attachments WHERE survey_id=?',
               (sid,), one=True)
    d['attach_count'] = ar['n'] if ar else 0
    return d


@app.route('/api/cs/surveys')
@login_required
def api_cs_surveys_list():
    """연도 + (선택)감독별 모든 선박의 분기별 서베이 목록.
    응답 구조: [{vessel: {...}, surveys: {1: {...}, 2: {...}}}]"""
    year = int(request.args.get('year') or 2026)
    sup_id = request.args.get('supervisor_id')

    # 선박 목록 — 감독 필터 적용
    if sup_id and sup_id != 'all':
        vessels = query("""
            SELECT v.* FROM vessels v
              JOIN supervisor_vessels sv ON sv.vessel_id = v.id
             WHERE v.active = 1 AND sv.supervisor_id = ?
             ORDER BY v.name
        """, (sup_id,))
    else:
        vessels = query('SELECT * FROM vessels WHERE active=1 ORDER BY name')

    # 해당 연도의 모든 서베이 한번에
    surveys = query('SELECT * FROM cs_surveys WHERE year = ?', (year,))

    # 한번에 findings 모두 가져와서 survey_id 별로 매핑 (N+1 회피)
    sids = [s['id'] for s in surveys]
    findings_by_sid = {sid: [] for sid in sids}
    if sids:
        placeholders = ','.join('?' * len(sids))
        all_findings = query(
            f'SELECT * FROM cs_findings WHERE survey_id IN ({placeholders}) ORDER BY survey_id, category, no',
            tuple(sids),
        )
        for f in all_findings:
            findings_by_sid[f['survey_id']].append(dict(f))

    by_vessel = {}
    for s in surveys:
        d = _cs_survey_with_counts(s)
        d['findings'] = findings_by_sid.get(s['id'], [])
        by_vessel.setdefault(s['vessel_id'], {})[s['quarter']] = d

    # 선박별 last_updated (해당 선박의 모든 surveys 중 가장 최근 updated_at)
    last_by_vessel = {}
    for s in surveys:
        u = s['updated_at']
        if u and (s['vessel_id'] not in last_by_vessel or u > last_by_vessel[s['vessel_id']]):
            last_by_vessel[s['vessel_id']] = u

    out = []
    for v in vessels:
        out.append({
            'vessel': dict(v),
            'surveys': by_vessel.get(v['id'], {}),
            'last_updated': last_by_vessel.get(v['id']),
        })
    return jsonify(out)


@app.route('/api/cs/surveys', methods=['POST'])
@login_required
def api_cs_survey_create():
    """헤더(분기 셀) 생성 또는 upsert."""
    d = request.get_json(silent=True) or {}
    vid = d.get('vessel_id'); year = d.get('year'); q = d.get('quarter')
    if not (vid and year and q in (1,2,3,4)):
        return jsonify({'error': 'vessel_id, year, quarter 필수'}), 400
    if not query('SELECT id FROM vessels WHERE id=?', (vid,), one=True):
        return jsonify({'error': '선박 없음'}), 404

    existing = query(
        'SELECT id FROM cs_surveys WHERE vessel_id=? AND year=? AND quarter=?',
        (vid, year, q), one=True,
    )
    if existing:
        return jsonify({'id': existing['id'], 'existed': True})

    sid = execute("""
        INSERT INTO cs_surveys
            (vessel_id, year, quarter, vendor, management, inspection_date,
             overall_remark, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (vid, year, q,
          d.get('vendor') or None,
          d.get('management') or None,
          d.get('inspection_date') or None,
          d.get('overall_remark') or None,
          session.get('username')))
    return jsonify({'id': sid}), 201


@app.route('/api/cs/surveys/<int:sid>', methods=['GET'])
@login_required
def api_cs_survey_get(sid):
    s = query('SELECT * FROM cs_surveys WHERE id=?', (sid,), one=True)
    if not s: abort(404)
    d = _cs_survey_with_counts(s)
    findings = query(
        "SELECT * FROM cs_findings WHERE survey_id=? ORDER BY category, no",
        (sid,),
    )
    d['findings'] = [dict(f) for f in findings]
    return jsonify(d)


@app.route('/api/cs/surveys/<int:sid>', methods=['PUT'])
@login_required
def api_cs_survey_update(sid):
    if not query('SELECT id FROM cs_surveys WHERE id=?', (sid,), one=True):
        abort(404)
    d = request.get_json(silent=True) or {}
    sets, params = [], []
    for f in ('vendor','management','inspection_date','overall_remark',
              'manual_defect_count','manual_observation_count','manual_close_count'):
        if f in d:
            sets.append(f'{f} = ?')
            v = d[f]
            # 빈 문자열은 NULL로 저장 (자동 카운트로 복귀)
            params.append(None if v == '' else v)
    if not sets:
        return jsonify({'error': '수정할 필드 없음'}), 400
    sets.append("updated_at = datetime('now','localtime')")
    params.append(sid)
    execute(f'UPDATE cs_surveys SET {", ".join(sets)} WHERE id = ?', params)
    return jsonify({'id': sid})


@app.route('/api/cs/surveys/<int:sid>', methods=['DELETE'])
@login_required
def api_cs_survey_delete(sid):
    execute('DELETE FROM cs_surveys WHERE id=?', (sid,))
    return jsonify({'ok': True})


# ----- Findings (세부 항목) -----

def _next_finding_no(survey_id, category):
    r = query(
        'SELECT COALESCE(MAX(no), 0) + 1 AS n FROM cs_findings WHERE survey_id=? AND category=?',
        (survey_id, category), one=True,
    )
    return r['n']


@app.route('/api/cs/surveys/<int:sid>/findings', methods=['POST'])
@login_required
def api_cs_finding_create(sid):
    """단건 또는 배치(엑셀 붙여넣기) 추가.
    body: { category: 'Defect'|'Observation', items: [{description,remark,status},...] }
    또는 단건: { category, description, remark, status }
    """
    if not query('SELECT id FROM cs_surveys WHERE id=?', (sid,), one=True):
        abort(404)
    d = request.get_json(silent=True) or {}
    cat = d.get('category')
    if cat not in ('Defect','Observation'):
        return jsonify({'error': "category는 Defect 또는 Observation"}), 400

    items = d.get('items')
    if items is None:
        items = [{
            'item':        d.get('item'),
            'description': d.get('description'),
            'remark':      d.get('remark'),
            'status':      d.get('status') or 'Open',
        }]

    next_no = _next_finding_no(sid, cat)
    created_ids = []
    for it in items:
        st = it.get('status') or 'Open'
        if st not in ('Open','Closed'): st = 'Open'
        fid = execute("""
            INSERT INTO cs_findings (survey_id, category, no, item, description, remark, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sid, cat, next_no,
              it.get('item') or '',
              it.get('description') or '',
              it.get('remark') or '',
              st))
        created_ids.append(fid)
        next_no += 1
    return jsonify({'ids': created_ids, 'count': len(created_ids)}), 201


@app.route('/api/cs/findings/<int:fid>', methods=['PUT'])
@login_required
def api_cs_finding_update(fid):
    cur = query('SELECT survey_id, status FROM cs_findings WHERE id=?', (fid,), one=True)
    if not cur:
        abort(404)
    d = request.get_json(silent=True) or {}
    sets, params = [], []
    for f in ('item','description','remark','status'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f])
    if not sets:
        return jsonify({'error': '수정할 필드 없음'}), 400
    sets.append("updated_at = datetime('now','localtime')")
    params.append(fid)
    execute(f'UPDATE cs_findings SET {", ".join(sets)} WHERE id = ?', params)

    # status 변경 시 cs_surveys.updated_at 갱신 (선박 헤더의 Last update에 반영)
    if 'status' in d and d['status'] != cur['status']:
        execute(
            "UPDATE cs_surveys SET updated_at = datetime('now','localtime') WHERE id=?",
            (cur['survey_id'],),
        )
    return jsonify({'id': fid})


@app.route('/api/cs/findings/<int:fid>', methods=['DELETE'])
@login_required
def api_cs_finding_delete(fid):
    f = query('SELECT survey_id, category, no FROM cs_findings WHERE id=?', (fid,), one=True)
    if not f: abort(404)
    execute('DELETE FROM cs_findings WHERE id=?', (fid,))
    # No 재정렬: 같은 survey + category 내에서
    rows = query(
        'SELECT id FROM cs_findings WHERE survey_id=? AND category=? ORDER BY no, id',
        (f['survey_id'], f['category']),
    )
    for idx, r in enumerate(rows, 1):
        execute('UPDATE cs_findings SET no=? WHERE id=?', (idx, r['id']))
    return jsonify({'ok': True})


# ─── 보고서 → 항목 자동 추출 (Gemini + 엑셀 파서) ─────────────
def _findings_workbook(title, subtitle, headers, rows, wrap_cols, widths):
    """검사 findings → 스타일된 1시트 워크북 BytesIO 반환."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook(); ws = wb.active; ws.title = 'List'
    F = 'Malgun Gothic'
    N = len(headers)
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    title_fill = PatternFill('solid', start_color='1F3A5F')
    sub_fill   = PatternFill('solid', start_color='2C5282')
    hdr_fill   = PatternFill('solid', start_color='34495E')
    def_fill   = PatternFill('solid', start_color='FCE8E6')   # Defect 행 연한 적색
    thin = Side(style='thin', color='BBBBBB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N)
    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(name=F, size=14, bold=True, color='FFFFFF'); c.fill = title_fill
    c.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=N)
    c = ws.cell(row=2, column=1, value=subtitle)
    c.font = Font(name=F, size=10, italic=True, color='ECF0F1'); c.fill = sub_fill
    c.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws.row_dimensions[2].height = 18
    ws.row_dimensions[3].height = 6

    HDR = 4
    for ci, h in enumerate(headers, start=1):
        cc = ws.cell(row=HDR, column=ci, value=h)
        cc.font = Font(name=F, size=11, bold=True, color='FFFFFF'); cc.fill = hdr_fill
        cc.alignment = Alignment(horizontal='center', vertical='center'); cc.border = border
    ws.row_dimensions[HDR].height = 24

    body = Font(name=F, size=10)
    top_wrap = Alignment(horizontal='left', vertical='top', wrap_text=True)
    center = Alignment(horizontal='center', vertical='top')
    r_idx = HDR + 1
    for row in rows:
        max_len = 1
        for ci, val in enumerate(row, start=1):
            cell = ws.cell(row=r_idx, column=ci, value=val)
            cell.font = body; cell.border = border
            cell.alignment = top_wrap if ci in wrap_cols else center
            if ci in wrap_cols and val:
                w = widths[ci - 1]
                max_len = max(max_len, sum((len(ln) // max(int(w / 1.6), 1)) + 1
                                           for ln in str(val).split('\n')))
        # Defect 행 살짝 음영
        if 'Category' in headers:
            cat_col = headers.index('Category') + 1
            if ws.cell(row=r_idx, column=cat_col).value == 'Defect':
                for ci in range(1, N + 1):
                    ws.cell(row=r_idx, column=ci).fill = def_fill
        ws.row_dimensions[r_idx].height = max(20, min(120, 15 * max_len + 4))
        r_idx += 1

    ws.freeze_panes = f'A{HDR + 1}'
    if r_idx - 1 > HDR:
        ws.auto_filter.ref = f'A{HDR}:{get_column_letter(N)}{r_idx - 1}'
    ws.print_options.horizontalCentered = True
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1; ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f'{HDR}:{HDR}'

    bio = BytesIO(); wb.save(bio); bio.seek(0)
    return bio


def _gemini_call_json(parts, model=None):
    """parts(list) → Gemini generateContent → 파싱된 JSON dict 또는 {'error':...}."""
    if not GEMINI_API_KEY:
        return {'error': 'NO_API_KEY'}
    import urllib.request, urllib.error
    mdl = model or GEMINI_MODEL
    body = {'contents': [{'parts': parts}],
            'generationConfig': {'response_mime_type': 'application/json'}}
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{mdl}:generateContent')
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'),
        headers={'content-type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as he:
        try:
            detail = he.read().decode('utf-8')[:300]
        except Exception:
            detail = str(he)
        return {'error': 'API_CALL_FAILED', 'detail': detail}
    except Exception as e:
        return {'error': 'API_CALL_FAILED', 'detail': str(e)}
    text = ''
    try:
        cands = data.get('candidates') or []
        if not cands:
            return {'error': 'API_CALL_FAILED', 'detail': json.dumps(data)[:300]}
        for part in (cands[0].get('content', {}).get('parts') or []):
            if isinstance(part.get('text'), str):
                text += part['text']
    except Exception as e:
        return {'error': 'PARSE_FAILED', 'raw': str(e)}
    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text[:4].lower() == 'json':
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        return {'error': 'PARSE_FAILED', 'raw': text[:300]}


def _coerce_translation_items(res):
    """Gemini 응답을 [{'i':int,'en':str}] 리스트로 정규화. list/dict/다양한 키 모두 수용."""
    if isinstance(res, dict):
        if res.get('error'):
            return None  # 호출 자체 실패
        arr = (res.get('translations') or res.get('items')
               or res.get('results') or res.get('data'))
        if arr is None:
            # 단일 객체이거나 {i:en} 매핑일 수 있음
            if 'i' in res and ('en' in res or 'text' in res):
                arr = [res]
            else:
                arr = []
    elif isinstance(res, list):
        arr = res
    else:
        arr = []
    return arr if isinstance(arr, list) else []


def _translate_batch_en(texts, group):
    """group(인덱스 리스트) 한 묶음 번역 → {원본인덱스: 영문}. 실패 시 None."""
    payload = json.dumps([{'i': i, 'text': texts[i]} for i in group], ensure_ascii=False)
    prompt = (
        "너는 선박 기술 감독(ship superintendent)이다. 아래 JSON 배열의 각 한국어(또는 한영 혼용) "
        "텍스트를 선박 관리 현업에서 자연스럽게 쓰는 영어로 번역하라.\n"
        "- 장비명·약어·단위·수치(예: BRG, RPM, S/W pump, LT cooler, EGCS, °C, kts)는 그대로 둔다.\n"
        "- 줄바꿈과 번호 매김(1. 2. ...) 구조를 그대로 보존한다.\n"
        "- 이미 영어인 부분은 그대로 둔다. 의미를 바꾸거나 내용을 덧붙이지 마라.\n"
        "반드시 {\"translations\":[...]} 형태의 JSON 객체로만 답하라. 입력의 i를 그대로 사용하라.\n"
        '형식: {"translations":[{"i":0,"en":"..."}]}\n\n[입력]\n' + payload)
    res = _gemini_call_json([{'text': prompt}], model=_model_for('translate'))
    arr = _coerce_translation_items(res)
    if arr is None:
        return None  # API 호출 실패 → 상위에서 분할 재시도
    out = {}
    for tr in arr:
        if not isinstance(tr, dict):
            continue
        try:
            i = int(tr.get('i'))
        except (TypeError, ValueError):
            continue
        en = tr.get('en') if isinstance(tr.get('en'), str) else tr.get('text')
        if isinstance(en, str) and en.strip():
            out[i] = en
    return out


def _gen_issue_summaries(payload_items):
    """payload_items: [{'i':int,'description':str,'action':str}] →
    {i: {'desc':str, 'action':str}} (한국어 요약). 키 없음/실패 시 빈 dict 부분 반환."""
    result = {}
    if not GEMINI_API_KEY or not payload_items:
        return result

    def run(group, depth=0):
        if not group:
            return
        sub = [payload_items[k] for k in group]
        prompt = (
            "너는 선박 기술 감독(ship superintendent)이다. 아래 JSON 배열의 각 업무 항목에 대해 "
            "두 가지를 한국어로 작성하라.\n"
            "- desc: description의 핵심 문제를 1문장(최대 2문장)으로 짧게 요약\n"
            "- action: action(최신 조치내용)을 한 줄로 짧게 요약 (내용 없으면 빈 문자열)\n"
            "■ 매우 중요: 요약은 원문(description/action)에 실제로 쓰인 단어와 표현을 그대로 사용해 "
            "압축하라. 동의어로 바꾸거나 새 표현을 지어내지 말고, 불필요한 부분만 덜어내라. "
            "원문에 있는 장비명·기술용어·약어·표현(예: EGCS, Pump, Auto mode, Maker Trouble Shooting, BRG, RPM, LT cooler)은 "
            "그대로 보존한다. 과장/추측/내용 추가 금지.\n"
            "입력의 i를 그대로 사용해 JSON 객체로만 답하라.\n"
            '형식: {"items":[{"i":0,"desc":"...","action":"..."}]}\n\n[입력]\n'
            + json.dumps(sub, ensure_ascii=False))
        res = _gemini_call_json([{'text': prompt}], model=_model_for('summary'))
        arr = _coerce_translation_items(res)  # translations/items/results/data 모두 수용
        if arr is None:
            if len(group) > 1 and depth < 6:
                mid = len(group) // 2
                run(group[:mid], depth + 1); run(group[mid:], depth + 1)
            return
        got = set()
        for o in arr:
            if not isinstance(o, dict):
                continue
            try:
                i = int(o.get('i'))
            except (TypeError, ValueError):
                continue
            result[i] = {
                'desc':   (o.get('desc') or o.get('desc_summary') or '').strip(),
                'action': (o.get('action') or o.get('action_summary') or '').strip(),
            }
            got.add(i)
        missing = [k for k in group if k not in got]
        if missing and len(group) > 1 and depth < 6:
            mid = max(1, len(missing) // 2)
            run(missing[:mid], depth + 1); run(missing[mid:], depth + 1)

    CHUNK = 12
    idxs = list(range(len(payload_items)))
    for s in range(0, len(idxs), CHUNK):
        run(idxs[s:s + CHUNK])
    return result


def _latest_action_progress(acts):
    if not acts:
        return ''
    try:
        best = sorted(acts, key=lambda a: (a.get('date') or ''))[-1]
    except Exception:
        best = acts[-1]
    return (best.get('progress') or '').strip()


def _latest_action(acts):
    """최신 action(날짜 최댓값)의 (date, progress) 반환."""
    if not acts:
        return '', ''
    try:
        best = sorted(acts, key=lambda a: (a.get('date') or ''))[-1]
    except Exception:
        best = acts[-1]
    return (best.get('date') or '').strip(), (best.get('progress') or '').strip()


def _md_label(d):
    try:
        y, m, dd = d.split('-')
        return f'[{int(m)}/{int(dd)}]'
    except Exception:
        return f'[{d}]' if d else ''


def _translate_texts_en(texts):
    """한국어(한영 혼용) 문자열 리스트 → 선박 감독 현업 영어. 키 없음/실패 시 원문 유지.
    묶음 실패 시 절반→1:1로 분할 재시도하여 '일부 누락'을 방지."""
    if not GEMINI_API_KEY:
        return list(texts)
    out = list(texts)
    idxs = [i for i, t in enumerate(texts) if t and str(t).strip()]

    def run(group, depth=0):
        if not group:
            return
        res = _translate_batch_en(texts, group)
        if res is None:
            # 호출 실패 → 분할 재시도
            if len(group) > 1 and depth < 6:
                mid = len(group) // 2
                run(group[:mid], depth + 1)
                run(group[mid:], depth + 1)
            return
        missing = [i for i in group if i not in res]
        for i, en in res.items():
            out[i] = en
        # 일부만 응답에 빠진 경우도 분할 재시도
        if missing and len(group) > 1 and depth < 6:
            mid = max(1, len(missing) // 2)
            run(missing[:mid], depth + 1)
            run(missing[mid:], depth + 1)

    CHUNK = 12
    for s in range(0, len(idxs), CHUNK):
        run(idxs[s:s + CHUNK])
    return out


def _translate_rows_en(rows):
    """이슈 행들의 item_topic/description/actions[].progress 를 영문으로 치환(제자리)."""
    bucket, texts = [], []
    for r in rows:
        if r.get('item_topic'):
            bucket.append((r, 'item_topic', None)); texts.append(r['item_topic'])
        if r.get('description'):
            bucket.append((r, 'description', None)); texts.append(r['description'])
        for ai, a in enumerate(r.get('actions') or []):
            if a.get('progress'):
                bucket.append((r, 'actions', ai)); texts.append(a['progress'])
    if not texts:
        return
    tr = _translate_texts_en(texts)
    for (r, field, ai), en in zip(bucket, tr):
        if field == 'actions':
            r['actions'][ai]['progress'] = en
        else:
            r[field] = en


_MARITIME_TERMS = (
    " 요약은 선박 현업(감독/기관부) 용어로 옮긴다. 일반어 → 현업어 매핑: "
    "repair=수리(※'보수'로 쓰지 말 것), cleaning/clean=소제, replace/renew/renewal=신환, "
    "install/fitting=설치, overhaul=O/H(분해점검), inspection/survey=수검, maintenance=정비, "
    "check/verify=확인, adjust/adjustment=조정, calibration=교정, test=시험, crack=균열, "
    "corrosion/rust=부식, leak/leakage=누설(누유/누수), wear/weardown=마모, deformation=변형, "
    "spare parts=예비품, weld/welding=용접, coating/painting=도장, submit=제출, "
    "place onboard=본선 비치. "
    "목록에 없어도 선박에서 통용되는 자연스러운 표현을 우선 사용한다. "
)


def _findings_prompt(kind):
    if kind == 'cs':
        return (
            "다음은 선박 컨디션 서베이(상태검사) 보고서다. 보고서에 적힌 지적/관찰 항목을 "
            "빠짐없이 추출해 지정한 JSON으로만 답하라. 각 항목 필드:\n"
            "- category: 'Defect' 또는 'Observation' (시정이 필요한 지적은 Defect, 권고/관찰사항은 Observation)\n"
            "- item: 짧은 제목 한 줄 (예: 'Main deck 부식')\n"
            "- description: 지적 상세 내용을 원문 그대로 복사한다(영문이면 영문 그대로). 요약·변형 금지.\n"
            "- remark: description의 핵심 지적사항을 한국어로 1~2문장으로 간결하게 요약한다(전체 직역 금지). 문장은 '~함/~됨/~음' 형태의 음슴체(개조식)로 끝맺는다. "
            "기술 명칭·장비명·약어(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm 등)는 번역하지 말고 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
            "없는 내용을 지어내지 말 것. 항목이 하나도 없으면 items를 빈 배열로.\n"
            '형식: {"items":[{"category":"Defect","item":"","description":"","remark":""}]}'
        )
    return (
        "다음은 선박 SIRE 2.0 점검 보고서다. 지적(결함) 사항만 추출한다.\n"
        "■ 포함 대상: 'Observable or detectable deficiency' 또는 'Not as expected'로 표시된 부정적 지적 "
        "(보고서에서 빨간색 글씨로 적힌 항목). 또한 'Photograph' 분류의 지적(예: 'Photo not representative', "
        "'Photograph supplied: ...' 아래 빨간 이탤릭 설명)처럼 사진 증빙이 부적절·불일치하다는 지적도 반드시 포함한다.\n"
        "■ 제외 대상: 'Exceeded normal expectation' 등 칭찬/긍정 평가(초록색 글씨)는 절대 포함하지 마라.\n"
        "각 지적 항목의 필드:\n"
        "- item: 항목 왼쪽에 표시된 분류 라벨을 괄호로 먼저 붙이고, 그 뒤에 굵게 표시된 "
        "지적 제목을 그대로 이어 붙인다. 분류 라벨은 보고서에 나온 그대로 쓴다 — "
        "Hardware · Human · Photograph · Process · Other 등 무엇이든. 예: "
        "'(Hardware)Misc Nautical Equipment – Maintenance deferred, awaiting spares', "
        "'(Human)Senior Engineer Officer – Not as expected', '(Photograph)Photo not representative'.\n"
        "- description: 제목 아래의 상세 본문(설명/이탤릭 문장 포함)을 영어 원문 그대로 복사한다. 요약·변형 금지.\n"
        "- remark: description의 핵심 지적사항을 한국어로 1~2문장으로 간결하게 요약한다(전체 직역 금지). 문장은 '~함/~됨/~음' 형태의 음슴체(개조식)로 끝맺는다. "
        "기술 명칭·장비명·약어(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm, turn table 등)는 번역하지 말고 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "없는 내용을 지어내지 말 것. 지적이 하나도 없으면 items를 빈 배열로.\n"
        '형식: {"items":[{"item":"","description":"","remark":""}]}'
    )


def _normalize_findings(parsed, kind):
    out = []
    if isinstance(parsed, list):
        arr = parsed
    elif isinstance(parsed, dict):
        arr = parsed.get('items') or parsed.get('findings') or []
    else:
        arr = []
    for it in (arr or []):
        if not isinstance(it, dict):
            continue
        rec = {
            'item':        (it.get('item') or '').strip(),
            'description': (it.get('description') or '').strip(),
            'remark':      (it.get('remark') or '').strip(),
        }
        if kind == 'cs':
            cat = it.get('category')
            rec['category'] = cat if cat in ('Defect', 'Observation') else 'Observation'
        if rec['item'] or rec['description']:
            out.append(rec)
    return out


def _xlsx_extract(raw_bytes, kind):
    """엑셀: 헤더가 명확하면 직접 매핑(AI 불필요), 자유양식이면 텍스트화 후 Gemini.
    반환: ('items', [...])  또는  ('text', '<탭구분 텍스트>')."""
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for r in ws.iter_rows(values_only=True):
        rows.append(['' if c is None else str(c).strip() for c in r])
    if not rows:
        return ('items', [])

    KEY = {
        'category':    ['category', '구분', '분류', 'type', 'def/obs'],
        'item':        ['item', '항목', 'title', 'subject', '제목', 'short gen name', 'gen name', 'short name'],
        'description': ['description', 'detail', 'details', '내용', '상세', 'finding', 'observation', 'remarks/finding'],
        'remark':      ['remark', 'remarks', '비고', 'note', 'notes', 'comment', 'action', '조치'],
    }
    header_idx, colmap = None, {}
    for i, row in enumerate(rows[:6]):
        m = {}
        for ci, cell in enumerate(row):
            lc = cell.lower()
            for field, keys in KEY.items():
                if field in m:
                    continue
                if any(k == lc or k in lc for k in keys):
                    m[field] = ci
        if 'description' in m or ('item' in m and len(m) >= 2):
            header_idx, colmap = i, m
            break

    if header_idx is not None:
        items = []
        for row in rows[header_idx + 1:]:
            if not any(row):
                continue
            def g(f):
                ci = colmap.get(f)
                return row[ci] if ci is not None and ci < len(row) else ''
            rec = {'item': g('item'), 'description': g('description'), 'remark': g('remark')}
            if kind == 'cs':
                cat = (g('category') or '').strip().lower()
                rec['category'] = 'Defect' if cat.startswith('def') or '지적' in cat else 'Observation'
            if not rec['description'] and rec['item']:
                rec['description'] = rec['item']
            if rec['item'] or rec['description']:
                items.append(rec)
        return ('items', items)

    # 자유 양식 → 텍스트(TSV)로 변환
    lines = ['\t'.join(r) for r in rows if any(r)]
    return ('text', '\n'.join(lines[:400]))


def _summarize_remarks(items, kind):
    """엑셀 직접매핑 항목들의 remark를, 각 description의 한글 요약으로 채운다(배치 1회 호출).
    GEMINI 키 없거나 실패 시 기존 remark 값을 그대로 유지."""
    if not GEMINI_API_KEY or not items:
        return items
    payload = json.dumps(
        [{'i': idx, 'description': (it.get('description') or '')} for idx, it in enumerate(items)],
        ensure_ascii=False)
    prompt = (
        "아래는 선박 점검 지적 항목들의 description 목록(JSON 배열)이다. 각 항목의 description을 "
        "한국어로 1~2문장으로 간결하게 요약하라(전체 직역 금지). 문장은 '~함/~됨/~음' 형태의 음슴체(개조식)로 끝맺어라. 기술 명칭·장비명·약어"
        "(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm 등)는 번역하지 말고 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "입력의 i 값을 그대로 사용해 JSON으로만 답하라.\n"
        '형식: {"summaries":[{"i":0,"remark":"요약문"}]}\n\n[입력]\n' + payload)
    res = _gemini_call_json([{'text': prompt}], model=_model_for('remark'))
    if isinstance(res, dict):
        if res.get('error'):
            return items
        arr = res.get('summaries') or res.get('items') or res.get('translations') or []
    elif isinstance(res, list):
        arr = res
    else:
        arr = []
    by_i = {}
    for s in arr:
        if not isinstance(s, dict):
            continue
        try:
            by_i[int(s.get('i'))] = (s.get('remark') or s.get('en') or '').strip()
        except (TypeError, ValueError):
            pass
    for idx, it in enumerate(items):
        if by_i.get(idx):
            it['remark'] = by_i[idx]
    return items


def _extract_findings_from_upload(f, kind):
    """업로드 FileStorage → 항목 리스트. (items, err) 반환."""
    name = (f.filename or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)

    if ext in ('xlsx', 'xls'):
        try:
            mode, data = _xlsx_extract(raw, kind)
        except Exception as e:
            return None, {'reason': 'XLSX_PARSE_FAILED', 'message': f'엑셀을 읽지 못했습니다: {e}'}
        if mode == 'items':
            return _summarize_remarks(data, kind), None
        parsed = _gemini_call_json([{'text': _findings_prompt(kind) + '\n\n[보고서 표 내용]\n' + data}], model=_model_for('findings'))
    elif ext == 'pdf':
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'PDF가 너무 큽니다({size_mb:.1f}MB). 15MB 이하로 줄이거나 페이지를 나눠 올려주세요.'}
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': 'application/pdf', 'data': b64}},
            {'text': _findings_prompt(kind)},
        ], model=_model_for('findings'))
    elif ext in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'):
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'이미지가 너무 큽니다({size_mb:.1f}MB).'}
        import mimetypes
        media = mimetypes.guess_type(name)[0] or 'image/jpeg'
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': media, 'data': b64}},
            {'text': _findings_prompt(kind)},
        ], model=_model_for('findings'))
    else:
        return None, {'reason': 'BAD_TYPE', 'message': 'PDF, 이미지, 엑셀(xlsx) 파일만 지원합니다.'}

    if isinstance(parsed, dict) and parsed.get('error') == 'NO_API_KEY':
        return None, {'reason': 'no_api_key', 'message': 'AI 자동추출이 설정되지 않았습니다(키 미설정).'}
    if isinstance(parsed, dict) and parsed.get('error'):
        return None, {'reason': parsed['error'], 'message': '자동 추출에 실패했습니다.',
                      'detail': parsed.get('detail') or parsed.get('raw')}
    return _normalize_findings(parsed, kind), None


@app.route('/api/cs/surveys/<int:sid>/extract-report', methods=['POST'])
@login_required
def api_cs_extract_report(sid):
    if not query('SELECT id FROM cs_surveys WHERE id=?', (sid,), one=True):
        abort(404)
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'ok': False, 'message': '파일이 없습니다.'}), 400
    items, err = _extract_findings_from_upload(request.files['file'], 'cs')
    if err:
        return jsonify({'ok': False, **err}), 200
    return jsonify({'ok': True, 'items': items, 'count': len(items)})


@app.route('/api/cs/surveys/<int:sid>/export')
@login_required
def api_cs_survey_export(sid):
    from flask import send_file
    s = query('''SELECT cs.*, v.name AS vessel_name
                   FROM cs_surveys cs JOIN vessels v ON v.id = cs.vessel_id
                  WHERE cs.id=?''', (sid,), one=True)
    if not s:
        abort(404)
    fr = query('''SELECT category, no, item, description, remark, status
                    FROM cs_findings WHERE survey_id=?
                   ORDER BY CASE category WHEN 'Defect' THEN 0 ELSE 1 END, no, id''', (sid,))
    rows = [[r['category'], r['no'], r['item'] or '', r['description'] or '',
             r['remark'] or '', r['status'] or ''] for r in fr]
    vessel = s['vessel_name']
    title = f"Condition Survey — {vessel}  {s['year']} Q{s['quarter']}"
    sub_bits = [f"수검일: {s['inspection_date'] or '-'}", f"Vendor: {s['vendor'] or '-'}",
                f"총 {len(rows)}건 (Defect {sum(1 for r in fr if r['category']=='Defect')} / "
                f"Observation {sum(1 for r in fr if r['category']=='Observation')})"]
    headers = ['Category', 'No.', 'ITEM', 'DESCRIPTION', 'REMARK', 'STATUS']
    bio = _findings_workbook(title, '   │   '.join(sub_bits), headers, rows,
                             wrap_cols={3, 4, 5}, widths=[12, 6, 28, 50, 40, 10])
    fname = f"CS_{_safe_filename(vessel)}_{s['year']}Q{s['quarter']}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ----- CS 첨부파일 -----

@app.route('/api/cs/surveys/<int:sid>/attachments', methods=['GET'])
@login_required
def api_cs_attachments_list(sid):
    rows = query(
        'SELECT * FROM cs_attachments WHERE survey_id=? ORDER BY id DESC',
        (sid,),
    )
    return jsonify([dict(r) for r in rows])


@app.route('/api/cs/surveys/<int:sid>/attachments', methods=['POST'])
@login_required
def api_cs_attachment_upload(sid):
    if not query('SELECT id FROM cs_surveys WHERE id=?', (sid,), one=True):
        abort(404)
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 없습니다.'}), 400

    ext = os.path.splitext(f.filename)[1]
    stored = f"cs_{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(UPLOAD_DIR, stored)
    f.save(save_path)
    size = os.path.getsize(save_path)

    aid = execute("""
        INSERT INTO cs_attachments
            (survey_id, filename, stored_name, file_size, mime_type, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (sid, f.filename, stored, size, f.mimetype, session.get('username')))
    return jsonify({'id': aid, 'filename': f.filename, 'file_size': size}), 201


@app.route('/api/cs/attachments/<int:aid>', methods=['GET'])
@login_required
def api_cs_attachment_get(aid):
    a = query('SELECT * FROM cs_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    inline = request.args.get('inline')
    return send_from_directory(
        UPLOAD_DIR, a['stored_name'],
        as_attachment=not inline,
        download_name=a['filename'],
    )


@app.route('/api/cs/attachments/<int:aid>', methods=['DELETE'])
@login_required
def api_cs_attachment_delete(aid):
    a = query('SELECT * FROM cs_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    p = os.path.join(UPLOAD_DIR, a['stored_name'])
    if os.path.exists(p):
        try: os.remove(p)
        except OSError: pass
    execute('DELETE FROM cs_attachments WHERE id=?', (aid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — Vetting Status (비정기, 선박당 0~N건, CNTR 제외)
# ═════════════════════════════════════════════════════════════════
VETTING_TYPES = ('VLCC', 'AFRAMAX', 'LR', 'MR')


def _vetting_with_counts(v):
    """vetting dict에 카운트 추가. manual override 적용."""
    vid = v['id']
    rows = query("""
        SELECT status, COUNT(*) AS n
          FROM vt_findings
         WHERE vetting_id = ?
         GROUP BY status
    """, (vid,))
    auto_open = auto_closed = 0
    for r in rows:
        if r['status'] == 'Closed': auto_closed = r['n']
        else: auto_open = r['n']
    auto_total = auto_open + auto_closed

    d = dict(v)
    d['observation_count'] = v['manual_observation_count'] if v['manual_observation_count'] is not None else auto_total
    d['close_count']       = v['manual_close_count']       if v['manual_close_count']       is not None else auto_closed
    d['open_count']        = v['manual_open_count']        if v['manual_open_count']        is not None else max(0, d['observation_count'] - d['close_count'])
    d['observation_manual'] = v['manual_observation_count'] is not None
    d['open_manual']        = v['manual_open_count']        is not None
    d['close_manual']       = v['manual_close_count']       is not None
    # 첨부 카운트
    ar = query('SELECT COUNT(*) AS n FROM vt_attachments WHERE vetting_id=?',
               (vid,), one=True)
    d['attach_count'] = ar['n'] if ar else 0
    return d


# ----- Vettings (vessel별 그룹) -----

@app.route('/api/vettings', methods=['GET'])
@login_required
def api_vettings_list():
    """선박별 vetting 그룹 응답.
    Query: ?year=2026&supervisor_id=N
    응답: [ { vessel: {...}, vettings: [...with findings...] } ]
    """
    year = request.args.get('year', type=int)
    sup_id = request.args.get('supervisor_id', type=int)

    # 대상 선박: VLCC/AFRAMAX/LR/MR만
    placeholders = ','.join('?' * len(VETTING_TYPES))
    sql = f'SELECT v.* FROM vessels v WHERE v.active=1 AND v.vessel_type IN ({placeholders})'
    params = list(VETTING_TYPES)
    if sup_id:
        sql += ' AND EXISTS (SELECT 1 FROM supervisor_vessels sv WHERE sv.vessel_id=v.id AND sv.supervisor_id=?)'
        params.append(sup_id)
    sql += ' ORDER BY v.name'
    vessels = query(sql, tuple(params))

    # vetting 한번에
    # vetting 필터:
    #  - 검사일이 있는 것은 해당 연도와 일치할 때만
    #  - 검사일이 없는 것 (방금 + 새 Vetting 추가 한 빈 행)은 모든 연도에 항상 표시
    if year:
        vettings = query('SELECT * FROM vettings')
        vettings = [v for v in vettings
                    if (not v['inspection_date'])
                    or (v['inspection_date'].startswith(str(year)))]
    else:
        vettings = query('SELECT * FROM vettings')

    # findings 한번에
    vids = [v['id'] for v in vettings]
    findings_by_vid = {vid: [] for vid in vids}
    if vids:
        ph = ','.join('?' * len(vids))
        all_f = query(
            f'SELECT * FROM vt_findings WHERE vetting_id IN ({ph}) ORDER BY vetting_id, no',
            tuple(vids),
        )
        for f in all_f:
            findings_by_vid[f['vetting_id']].append(dict(f))

    by_vessel = {}
    for v in vettings:
        d = _vetting_with_counts(v)
        d['findings'] = findings_by_vid.get(v['id'], [])
        by_vessel.setdefault(v['vessel_id'], []).append(d)

    # 검사일 내림차순 정렬 (최신이 위)
    for vid in by_vessel:
        by_vessel[vid].sort(key=lambda x: (x.get('inspection_date') or ''), reverse=True)

    # 선박별 담당 감독 ID 매핑 (Daily 이슈 등록 시 필요)
    sv_map = {}
    if vessels:
        v_ids = [v['id'] for v in vessels]
        ph2 = ','.join('?' * len(v_ids))
        rows = query(
            f'SELECT vessel_id, supervisor_id FROM supervisor_vessels WHERE vessel_id IN ({ph2})',
            tuple(v_ids),
        )
        for r in rows:
            sv_map.setdefault(r['vessel_id'], []).append(r['supervisor_id'])

    # 선박별 last_updated (해당 선박의 모든 vettings 중 가장 최근 updated_at)
    last_by_vessel = {}
    for v in vettings:
        u = v['updated_at']
        if u and (v['vessel_id'] not in last_by_vessel or u > last_by_vessel[v['vessel_id']]):
            last_by_vessel[v['vessel_id']] = u

    out = []
    for ves in vessels:
        vd = dict(ves)
        vd['supervisor_ids'] = sv_map.get(ves['id'], [])
        out.append({
            'vessel': vd,
            'vettings': by_vessel.get(ves['id'], []),
            'last_updated': last_by_vessel.get(ves['id']),
        })
    return jsonify(out)


@app.route('/api/vettings', methods=['POST'])
@login_required
def api_vetting_create():
    """단일 vetting 생성. 선박 ID만 필수, 나머지는 선택."""
    d = request.get_json() or {}
    vid = d.get('vessel_id')
    if not vid:
        return jsonify({'error': 'vessel_id 가 필요합니다.'}), 400
    v = query('SELECT vessel_type FROM vessels WHERE id=?', (vid,), one=True)
    if not v:
        return jsonify({'error': '선박을 찾을 수 없습니다.'}), 404
    if v['vessel_type'] not in VETTING_TYPES:
        return jsonify({'error': f'Vetting은 {", ".join(VETTING_TYPES)} 선박에만 적용됩니다.'}), 400

    st = d.get('sire_type') or None
    if st and st not in ('Idle', 'Bunkering', 'Discharge'):
        st = None
    valid = d.get('valid') or None
    if valid and valid not in ('Next Plan', 'Last Result'):
        valid = None

    new_id = execute("""
        INSERT INTO vettings
            (vessel_id, report_number, inspection_date, inspection_company,
             inspector, port, sire_type, valid, overall_remark, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vid,
          d.get('report_number') or '',
          d.get('inspection_date') or None,
          d.get('inspection_company') or '',
          d.get('inspector') or '',
          d.get('port') or '',
          st,
          valid,
          d.get('overall_remark') or '',
          session.get('username')))
    row = query('SELECT * FROM vettings WHERE id=?', (new_id,), one=True)
    return jsonify(_vetting_with_counts(row)), 201


@app.route('/api/vettings/<int:vid>', methods=['GET'])
@login_required
def api_vetting_get(vid):
    v = query('SELECT * FROM vettings WHERE id=?', (vid,), one=True)
    if not v:
        abort(404)
    d = _vetting_with_counts(v)
    d['findings'] = [dict(f) for f in query(
        'SELECT * FROM vt_findings WHERE vetting_id=? ORDER BY no', (vid,))]
    return jsonify(d)


@app.route('/api/vettings/<int:vid>', methods=['PUT'])
@login_required
def api_vetting_update(vid):
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    d = request.get_json() or {}
    sets, params = [], []
    for f in ('report_number','inspection_date','inspection_company','inspector',
              'port','sire_type','valid','overall_remark',
              'manual_observation_count','manual_open_count','manual_close_count'):
        if f in d:
            sets.append(f'{f} = ?')
            v = d[f]
            params.append(None if v == '' else v)
    if not sets:
        return jsonify({'ok': True})
    sets.append("updated_at = datetime('now','localtime')")
    execute(f'UPDATE vettings SET {", ".join(sets)} WHERE id=?', tuple(params + [vid]))
    return jsonify({'ok': True})


@app.route('/api/vettings/<int:vid>', methods=['DELETE'])
@login_required
def api_vetting_delete(vid):
    # 첨부 파일도 같이 삭제 (CASCADE는 DB만, 파일은 직접)
    atts = query('SELECT stored_name FROM vt_attachments WHERE vetting_id=?', (vid,))
    for a in atts:
        p = os.path.join(UPLOAD_DIR, a['stored_name'])
        if os.path.exists(p):
            try: os.remove(p)
            except OSError: pass
    execute('DELETE FROM vettings WHERE id=?', (vid,))
    return jsonify({'ok': True})


# ----- Findings -----

def _vt_next_no(vid):
    r = query('SELECT COALESCE(MAX(no), 0) + 1 AS next FROM vt_findings WHERE vetting_id=?',
              (vid,), one=True)
    return r['next']


@app.route('/api/vettings/<int:vid>/findings', methods=['POST'])
@login_required
def api_vt_findings_create(vid):
    """단건 또는 배치(items 배열) 생성."""
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    d = request.get_json() or {}
    items = d.get('items')
    if items is None:
        items = [{
            'item':        d.get('item'),
            'description': d.get('description'),
            'remark':      d.get('remark'),
            'user_remark': d.get('user_remark'),
            'status':      d.get('status') or 'Open',
        }]

    next_no = _vt_next_no(vid)
    created = []
    for it in items:
        st = it.get('status') or 'Open'
        if st not in ('Open','Closed'): st = 'Open'
        fid = execute("""
            INSERT INTO vt_findings (vetting_id, no, item, description, remark, user_remark, priority, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (vid, next_no,
              it.get('item') or '',
              it.get('description') or '',
              it.get('remark') or '',
              it.get('user_remark') or '',
              1 if it.get('priority') else 0,
              st))
        created.append(fid)
        next_no += 1
    return jsonify({'ids': created, 'count': len(created)}), 201


@app.route('/api/vt-findings/<int:fid>', methods=['PUT'])
@login_required
def api_vt_finding_update(fid):
    cur = query('SELECT vetting_id, status FROM vt_findings WHERE id=?', (fid,), one=True)
    if not cur:
        abort(404)
    d = request.get_json() or {}
    sets, params = [], []
    for f in ('item','description','remark','user_remark','status'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f] or '')
    if 'priority' in d:
        sets.append('priority = ?')
        params.append(1 if d.get('priority') else 0)
    if not sets:
        return jsonify({'ok': True})
    sets.append("updated_at = datetime('now','localtime')")
    execute(f'UPDATE vt_findings SET {", ".join(sets)} WHERE id=?', tuple(params + [fid]))

    # status 변경 시 vettings.updated_at 갱신 (선박 헤더의 Last update에 반영)
    if 'status' in d and d['status'] != cur['status']:
        execute(
            "UPDATE vettings SET updated_at = datetime('now','localtime') WHERE id=?",
            (cur['vetting_id'],),
        )
    return jsonify({'ok': True})


@app.route('/api/vt-findings/<int:fid>', methods=['DELETE'])
@login_required
def api_vt_finding_delete(fid):
    f = query('SELECT vetting_id FROM vt_findings WHERE id=?', (fid,), one=True)
    if not f:
        abort(404)
    vid = f['vetting_id']
    execute('DELETE FROM vt_findings WHERE id=?', (fid,))
    # No 재정렬
    rows = query('SELECT id FROM vt_findings WHERE vetting_id=? ORDER BY no', (vid,))
    for new_no, r in enumerate(rows, start=1):
        execute('UPDATE vt_findings SET no=? WHERE id=?', (new_no, r['id']))
    return jsonify({'ok': True})


# ----- Attachments -----

def _vetting_full_prompt():
    return (
        "다음은 선박 SIRE 2.0 점검(Vetting Inspection) 보고서다. 두 가지를 추출해 지정한 JSON으로만 답하라.\n"
        "■ meta: 보고서 표지/상단의 점검 메타정보. 보고서에 해당 정보가 없으면 반드시 빈 문자열로 둔다(지어내지 말 것).\n"
        "- report_number: Report No / Report # / 보고서 번호\n"
        "- inspection_date: 점검 실시일 (반드시 YYYY-MM-DD 형식. 다른 형식이면 YYYY-MM-DD로 변환)\n"
        "- inspection_company: 점검 주체 / Oil Major / 제출사 (예: VIVA ENERGY, BP, SHELL, TOTAL)\n"
        "- inspector: 점검관(Inspector) 성명\n"
        "- port: 점검 항구명만 추출한다(도시/항구 이름). 국가명·UNLOCODE 코드(예: [SGSIN])·중복 표기는 제거. "
        "예: 'Singapore - Singapore [SGSIN]' → 'Singapore', 'Fujairah - UAE [AEFJR]' → 'Fujairah'.\n"
        "- sire_type: 점검 시 운항 상태. 반드시 'Idle' · 'Bunkering' · 'Discharge' 중 하나로만. 식별 불가 시 빈 문자열.\n"
        "■ items: 지적(결함) 사항만 추출한다.\n"
        "■ 포함: 'Observable or detectable deficiency' / 'Not as expected'로 표시된 부정적 지적(빨간 글씨). "
        "또한 'Photograph' 분류의 지적(예: 'Photo not representative', 'Photograph supplied: ...' 아래 빨간 이탤릭 설명)처럼 "
        "사진 증빙이 부적절·불일치하다는 지적도 반드시 포함한다.\n"
        "■ 제외: 'Exceeded normal expectation' 등 칭찬/긍정 평가(초록 글씨)는 절대 포함하지 마라.\n"
        "- item: 항목 왼쪽에 표시된 분류 라벨을 괄호로 먼저 붙이고, 그 뒤 굵게 표시된 지적 제목을 그대로 이어 붙인다. "
        "분류 라벨은 보고서에 나온 그대로 쓴다 — Hardware · Human · Photograph · Process · Other 등 무엇이든. "
        "예: '(Hardware)Misc Nautical Equipment – Maintenance deferred', '(Human)Senior Engineer Officer – Not as expected', "
        "'(Photograph)Photo not representative'.\n"
        "- description: 제목 아래 상세 본문(설명/이탤릭 문장 포함)을 영어 원문 그대로 복사. 요약·변형 금지.\n"
        "- remark: description의 핵심 지적사항을 한국어 1~2문장으로 간결하게 요약(전체 직역 금지). 문장은 '~함/~됨/~음' 음슴체(개조식). "
        "기술 명칭·장비명·약어(예: ECDIS, DCP, DRS, smoke detector, high-high level alarm 등)는 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "없는 내용을 지어내지 말 것. 지적이 하나도 없으면 items를 빈 배열로.\n"
        '형식: {"meta":{"report_number":"","inspection_date":"","inspection_company":"","inspector":"",'
        '"port":"","sire_type":""},"items":[{"item":"","description":"","remark":""}]}'
    )


def _clean_port(p):
    """'Singapore - Singapore [SGSIN]' → 'Singapore'. 국가/코드/중복 제거, 항구명만."""
    s = (p or '').strip()
    if not s:
        return ''
    s = _re_cls.sub(r'\[[^\]]*\]', '', s)      # [SGSIN] 등 코드 제거
    s = s.split(' - ')[0]                       # ' - ' 앞 항구명만
    s = s.split(' / ')[0].split('/')[0]         # '/' 구분도 첫 토큰
    s = _re_cls.sub(r'\s+', ' ', s).strip(' -,')
    return s


def _norm_vetting_meta(m):
    m = m if isinstance(m, dict) else {}
    g = lambda k: (m.get(k) or '').strip()
    sire = g('sire_type')
    return {
        'report_number':      g('report_number'),
        'inspection_date':    g('inspection_date'),
        'inspection_company': g('inspection_company'),
        'inspector':          g('inspector'),
        'port':               _clean_port(g('port')),
        'sire_type':          sire if sire in ('Idle', 'Bunkering', 'Discharge') else '',
        'valid':              '',   # '상태'(Next Plan/Last Result)는 수동 입력 — 보고서에서 추출하지 않음
    }


def _extract_vetting_from_upload(f):
    """SIRE 보고서 업로드 → (items, meta, err). 헤더 메타 + 지적 항목을 한 번에 추출."""
    name = (f.filename or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)
    prompt = _vetting_full_prompt()

    if ext == 'pdf':
        if size_mb > 15:
            return None, None, {'reason': 'TOO_LARGE', 'message': f'PDF가 너무 큽니다({size_mb:.1f}MB). 15MB 이하로 줄여주세요.'}
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': 'application/pdf', 'data': b64}},
            {'text': prompt},
        ], model=_model_for('findings'))
    elif ext in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'):
        if size_mb > 15:
            return None, None, {'reason': 'TOO_LARGE', 'message': f'이미지가 너무 큽니다({size_mb:.1f}MB).'}
        import mimetypes
        media = mimetypes.guess_type(name)[0] or 'image/jpeg'
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': media, 'data': b64}},
            {'text': prompt},
        ], model=_model_for('findings'))
    elif ext in ('xlsx', 'xls'):
        try:
            txt = _xlsx_to_text(raw)
        except Exception as e:
            return None, None, {'reason': 'XLSX_PARSE_FAILED', 'message': f'엑셀을 읽지 못했습니다: {e}'}
        parsed = _gemini_call_json([{'text': prompt + '\n\n[보고서 표 내용]\n' + txt}],
                                   model=_model_for('findings'))
    else:
        return None, None, {'reason': 'BAD_TYPE', 'message': 'PDF · 이미지 · 엑셀(xlsx) 파일만 지원합니다.'}

    if isinstance(parsed, dict) and parsed.get('error') == 'NO_API_KEY':
        return None, None, {'reason': 'no_api_key', 'message': 'AI 자동추출이 설정되지 않았습니다(키 미설정).'}
    if isinstance(parsed, dict) and parsed.get('error'):
        return None, None, {'reason': parsed['error'], 'message': '자동 추출에 실패했습니다.',
                            'detail': parsed.get('detail') or parsed.get('raw')}
    items = _normalize_findings(parsed, 'sire')
    meta = _norm_vetting_meta(parsed.get('meta') if isinstance(parsed, dict) else None)
    return items, meta, None


@app.route('/api/vettings/<int:vid>/extract-report', methods=['POST'])
@login_required
def api_vt_extract_report(vid):
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'ok': False, 'message': '파일이 없습니다.'}), 400
    items, meta, err = _extract_vetting_from_upload(request.files['file'])
    if err:
        return jsonify({'ok': False, **err}), 200
    # 헤더 메타 자동 반영: 추출값이 있는 필드만 갱신 (없으면 기존값 유지)
    applied = {}
    sets, params = [], []
    for col in ('report_number', 'inspection_date', 'inspection_company',
                'inspector', 'port', 'sire_type', 'valid'):
        val = (meta or {}).get(col, '')
        if val:
            sets.append(f'{col}=?'); params.append(val); applied[col] = val
    if sets:
        sets.append("updated_at=datetime('now','localtime')")
        params.append(vid)
        execute(f'UPDATE vettings SET {", ".join(sets)} WHERE id=?', tuple(params))
    return jsonify({'ok': True, 'items': items, 'count': len(items),
                    'meta': meta, 'applied': applied})


def _md_from_date(d):
    """'2026-04-30' → '4/30'. 파싱 실패 시 원문."""
    try:
        y, m, dd = (d or '').split('-')
        return f'{int(m)}/{int(dd)}'
    except Exception:
        return (d or '').strip()


def _company_abbr(c):
    """'VIVA ENERGY' → 'VIVA' (첫 토큰 대문자). 빈 값이면 ''."""
    c = (c or '').strip()
    if not c:
        return ''
    return c.split()[0].upper()


def _sire_abbr(s):
    return {'Bunkering': 'BUNKER', 'Discharge': 'DISCHARGE', 'Idle': 'IDLE'}.get(
        (s or '').strip(), (s or '').strip().upper())


def _condense_obs(items):
    """[{i,summary,description,user_remark}] → {i: short}. 선박 약어체 한 줄.
    GEMINI 키 없거나 실패 시 빈 dict (상위에서 번역요약으로 폴백)."""
    out = {}
    if not GEMINI_API_KEY or not items:
        return out
    payload = json.dumps([{'i': it['i'], 'summary': it.get('summary', ''),
                           'description': it.get('description', '')} for it in items],
                         ensure_ascii=False)
    prompt = (
        "아래는 선박 SIRE 점검 지적 항목들이다(JSON 배열). 각 항목의 핵심 결함을 "
        "선박 현업 약어체로 아주 짧게 한 줄로 요약하라.\n"
        "- 장비명은 선박 약어로 대문자 표기: Cargo Oil Tank→COT, Ballast Water Treatment System→BWTS, "
        "Main Engine→M/E, Auxiliary Engine→A/E, pressure→PRESS., No.3 Port→3P, Vapour return manifold→VAP. RETURN MANIFOLD 등.\n"
        "- 결함은 '불량/파손/누설/마모/고장' 등 한 단어로 압축. 군더더기·서술 제거.\n"
        "- 예: 'Cargo tank high level alarm display 결함으로 상시 점등됨' → 'COT HIGH LEVEL ALARM DISPLAY 불량', "
        "'3 Port cargo tank 압력 센서 결함' → '3P COT PRESS. SENSOR 불량'.\n"
        + _MARITIME_TERMS +
        "입력의 i를 그대로 사용해 JSON으로만 답하라.\n"
        '형식: {"items":[{"i":0,"short":"..."}]}\n\n[입력]\n' + payload)
    res = _gemini_call_json([{'text': prompt}], model=_model_for('summary'))
    arr = _coerce_translation_items(res)
    for o in (arr or []):
        if not isinstance(o, dict):
            continue
        try:
            i = int(o.get('i'))
        except (TypeError, ValueError):
            continue
        sh = (o.get('short') or o.get('en') or '').strip()
        if sh:
            out[i] = sh
    return out


@app.route('/api/vettings/<int:vid>/obs-summary', methods=['POST'])
@login_required
def api_vt_obs_summary(vid):
    """Priority 체크 + Open 항목 기준으로 '지적 상세' 요약을 생성해 overall_remark에 기록."""
    v = query('SELECT * FROM vettings WHERE id=?', (vid,), one=True)
    if not v:
        abort(404)
    findings = query('SELECT * FROM vt_findings WHERE vetting_id=? ORDER BY no, id', (vid,))
    open_f = [f for f in findings if (f['status'] or 'Open') == 'Open']
    def _is_prio(f):
        try:
            return bool(f['priority'])
        except (KeyError, IndexError):
            return False
    prio = [f for f in open_f if _is_prio(f)]
    total_open = len(open_f)
    minor = total_open - len(prio)

    header_bits = [b for b in (_md_from_date(v['inspection_date']),
                               _company_abbr(v['inspection_company']),
                               _sire_abbr(v['sire_type'])) if b]
    header = (' '.join(header_bits) + ' ' if header_bits else '') + \
             f'SIRE OBS 잔여 {total_open}건 조치 중'

    shorts = _condense_obs([
        {'i': i, 'summary': f['remark'] or '', 'description': f['description'] or '',
         'user_remark': f['user_remark'] or ''}
        for i, f in enumerate(prio)
    ])

    lines = [header]
    for i, f in enumerate(prio):
        short = shorts.get(i) or (f['remark'] or f['item'] or '').strip()
        ur = (f['user_remark'] or '').strip()
        lines.append(f'{i + 1}. {short}' + (f' - {ur}' if ur else ''))
    if minor > 0:
        lines.append(f'그 외 Minor 지적 {minor}건')
    text = '\n'.join(lines)

    execute("UPDATE vettings SET overall_remark=?, updated_at=datetime('now','localtime') WHERE id=?",
            (text, vid))
    return jsonify({'ok': True, 'summary': text,
                    'total_open': total_open, 'priority_open': len(prio), 'minor': minor})


@app.route('/api/vettings/<int:vid>/export')
@login_required
def api_vt_export(vid):
    from flask import send_file
    v = query('''SELECT vt.*, ve.name AS vessel_name
                   FROM vettings vt JOIN vessels ve ON ve.id = vt.vessel_id
                  WHERE vt.id=?''', (vid,), one=True)
    if not v:
        abort(404)
    fr = query('''SELECT no, item, description, remark, user_remark, status
                    FROM vt_findings WHERE vetting_id=? ORDER BY no, id''', (vid,))
    rows = [[r['no'], r['item'] or '', r['description'] or '',
             r['remark'] or '', r['user_remark'] or '', r['status'] or ''] for r in fr]
    vessel = v['vessel_name']
    rno = v['report_number'] or ''
    title = f"SIRE Observation List — {vessel}"
    sub_bits = [f"검사일: {v['inspection_date'] or '-'}", f"Port: {v['port'] or '-'}"]
    if rno:
        sub_bits.append(f"Report: {rno}")
    sub_bits.append(f"총 {len(rows)}건")
    headers = ['No.', 'ITEM', 'DESCRIPTION', '번역 요약', 'Remark', 'STATUS']
    bio = _findings_workbook(title, '   │   '.join(sub_bits), headers, rows,
                             wrap_cols={2, 3, 4, 5}, widths=[6, 26, 46, 38, 30, 10])
    date_tag = (v['inspection_date'] or '').replace('-', '')
    fname = f"SIRE_{_safe_filename(vessel)}_{date_tag or vid}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/vettings/<int:vid>/attachments', methods=['GET'])
@login_required
def api_vt_attachments_list(vid):
    rows = query(
        'SELECT * FROM vt_attachments WHERE vetting_id=? ORDER BY id DESC',
        (vid,),
    )
    return jsonify([dict(r) for r in rows])


@app.route('/api/vettings/<int:vid>/attachments', methods=['POST'])
@login_required
def api_vt_attachment_upload(vid):
    if not query('SELECT id FROM vettings WHERE id=?', (vid,), one=True):
        abort(404)
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 없습니다.'}), 400

    ext = os.path.splitext(f.filename)[1]
    stored = f"vt_{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(UPLOAD_DIR, stored)
    f.save(save_path)
    size = os.path.getsize(save_path)

    aid = execute("""
        INSERT INTO vt_attachments
            (vetting_id, filename, stored_name, file_size, mime_type, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (vid, f.filename, stored, size, f.mimetype, session.get('username')))
    return jsonify({'id': aid, 'filename': f.filename, 'file_size': size}), 201


@app.route('/api/vt-attachments/<int:aid>', methods=['GET'])
@login_required
def api_vt_attachment_get(aid):
    a = query('SELECT * FROM vt_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    inline = request.args.get('inline')
    return send_from_directory(
        UPLOAD_DIR, a['stored_name'],
        as_attachment=not inline,
        download_name=a['filename'],
    )


@app.route('/api/vt-attachments/<int:aid>', methods=['DELETE'])
@login_required
def api_vt_attachment_delete(aid):
    a = query('SELECT * FROM vt_attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    p = os.path.join(UPLOAD_DIR, a['stored_name'])
    if os.path.exists(p):
        try: os.remove(p)
        except OSError: pass
    execute('DELETE FROM vt_attachments WHERE id=?', (aid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — Calendar Events (일정 모듈)
# ═════════════════════════════════════════════════════════════════
CAL_VALID_COLORS = ('gray','red','amber','yellow','green','blue','purple','pink')


@app.route('/api/cal/events', methods=['GET'])
@login_required
def api_cal_events_list():
    """기간 내 일정 조회.
    Query: ?start=YYYY-MM-DD&end=YYYY-MM-DD&supervisor_id=N
    - supervisor_id 없거나 'all' = 전체 (공용 + 모든 감독)
    - supervisor_id=N = 해당 감독의 일정 + 공용(supervisor_id IS NULL)
    """
    start = request.args.get('start')
    end   = request.args.get('end')
    sup   = request.args.get('supervisor_id')

    sql = 'SELECT * FROM calendar_events WHERE 1=1'
    params = []
    if start:
        # 시작일이 end 보다 작거나, end_date가 start보다 크거나 (멀티데이 겹침)
        sql += ' AND (COALESCE(end_date, start_date) >= ?)'
        params.append(start)
    if end:
        sql += ' AND (start_date <= ?)'
        params.append(end)
    if sup and sup != 'all':
        sql += ' AND (supervisor_id = ? OR supervisor_id IS NULL)'
        params.append(int(sup))
    sql += ' ORDER BY start_date, COALESCE(start_time, "00:00")'

    rows = query(sql, tuple(params))
    return jsonify([dict(r) for r in rows])


@app.route('/api/cal/events/find', methods=['GET'])
@login_required
def api_cal_event_find():
    """source_type + source_id 로 기존 일정 조회 (중복 체크용).
    Query: ?source_type=issue|cs|vetting&source_id=N
    응답: event dict 또는 null
    """
    src_type = request.args.get('source_type')
    src_id   = request.args.get('source_id', type=int)
    if not src_type or not src_id:
        return jsonify(None)
    r = query('SELECT * FROM calendar_events WHERE source_type=? AND source_id=?',
              (src_type, src_id), one=True)
    return jsonify(dict(r) if r else None)


@app.route('/api/cal/events', methods=['POST'])
@login_required
def api_cal_event_create():
    d = request.get_json() or {}
    if not d.get('title'):
        return jsonify({'error': 'title 이 필요합니다.'}), 400
    if not d.get('start_date'):
        return jsonify({'error': 'start_date 가 필요합니다.'}), 400

    color = (d.get('color') or 'blue').lower()
    if color not in CAL_VALID_COLORS:
        color = 'blue'

    all_day = 1 if d.get('all_day', True) else 0

    new_id = execute("""
        INSERT INTO calendar_events
            (supervisor_id, vessel_id, title, start_date, end_date,
             all_day, start_time, end_time, category, color, location, notes,
             source_type, source_id, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        d.get('supervisor_id') or None,
        d.get('vessel_id') or None,
        d['title'],
        d['start_date'],
        d.get('end_date') or None,
        all_day,
        d.get('start_time') or None,
        d.get('end_time') or None,
        d.get('category') or '',
        color,
        d.get('location') or '',
        d.get('notes') or '',
        d.get('source_type') or 'manual',
        d.get('source_id') or None,
        session.get('username'),
    ))
    return jsonify({'id': new_id}), 201


@app.route('/api/cal/events/<int:eid>', methods=['GET'])
@login_required
def api_cal_event_get(eid):
    r = query('SELECT * FROM calendar_events WHERE id=?', (eid,), one=True)
    if not r:
        abort(404)
    return jsonify(dict(r))


@app.route('/api/cal/events/<int:eid>', methods=['PUT'])
@login_required
def api_cal_event_update(eid):
    if not query('SELECT id FROM calendar_events WHERE id=?', (eid,), one=True):
        abort(404)
    d = request.get_json() or {}
    sets, params = [], []
    for f in ('supervisor_id','vessel_id','title','start_date','end_date',
              'all_day','start_time','end_time','category','color',
              'location','notes'):
        if f in d:
            v = d[f]
            if f == 'color' and v:
                v = v.lower()
                if v not in CAL_VALID_COLORS:
                    v = 'blue'
            if f == 'all_day':
                v = 1 if v else 0
            sets.append(f'{f} = ?')
            params.append(None if v == '' else v)
    if not sets:
        return jsonify({'ok': True})
    sets.append("updated_at = datetime('now','localtime')")
    execute(f'UPDATE calendar_events SET {", ".join(sets)} WHERE id=?',
            tuple(params + [eid]))
    return jsonify({'ok': True})


@app.route('/api/cal/events/<int:eid>', methods=['DELETE'])
@login_required
def api_cal_event_delete(eid):
    execute('DELETE FROM calendar_events WHERE id=?', (eid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  API — Dry Dock Report (메타 CRUD)
#   · Step 1: 보고서 자체의 생성/조회/수정/삭제만
#   · 섹션·블록 편집 / 추출은 Step 2~3에서 추가
# ═════════════════════════════════════════════════════════════════
def _dock_to_dict(row):
    d = dict(row)
    # 출력 시 None → '' 변환은 프론트에서 처리
    return d


def _can_edit_dock_report(report_row_or_id):
    """
    현재 세션 사용자가 이 보고서를 편집할 권한이 있는가?
      · admin: 항상 True
      · 담당 감독(supervisor_id 일치): True
      · 그 외: False
    인자로 report 행(dict 또는 sqlite Row) 또는 id(int) 모두 받음.
    """
    if session.get('role') == 'admin':
        return True
    my_sv = session.get('supervisor_id')
    if not my_sv:
        return False

    if isinstance(report_row_or_id, int):
        r = query('SELECT supervisor_id FROM dock_reports WHERE id=?',
                  (report_row_or_id,), one=True)
        if not r:
            return False
        report_sv = r['supervisor_id']
    else:
        report_sv = report_row_or_id.get('supervisor_id') \
                    if hasattr(report_row_or_id, 'get') \
                    else report_row_or_id['supervisor_id']

    return report_sv is not None and report_sv == my_sv


def _require_dock_edit(rid):
    """편집 권한 없으면 403. 통과 시 None 반환."""
    if not query('SELECT id FROM dock_reports WHERE id=?', (rid,), one=True):
        abort(404)
    if not _can_edit_dock_report(rid):
        return jsonify({'error': '이 보고서를 편집할 권한이 없습니다. (담당 감독 또는 관리자만 수정 가능)'}), 403
    return None


def _require_dock_edit_via_section(sid):
    """섹션 ID → 보고서 ID → 권한 검사"""
    r = query('SELECT report_id FROM dock_report_sections WHERE id=?', (sid,), one=True)
    if not r:
        abort(404)
    rid = r['report_id']
    if not _can_edit_dock_report(rid):
        return jsonify({'error': '이 보고서를 편집할 권한이 없습니다.'}), 403
    return None


def _require_dock_edit_via_block(bid):
    """블록 ID → 섹션 → 보고서 → 권한 검사"""
    r = query('''
        SELECT s.report_id FROM dock_report_blocks b
          JOIN dock_report_sections s ON s.id = b.section_id
         WHERE b.id = ?
    ''', (bid,), one=True)
    if not r:
        abort(404)
    rid = r['report_id']
    if not _can_edit_dock_report(rid):
        return jsonify({'error': '이 보고서를 편집할 권한이 없습니다.'}), 403
    return None


@app.route('/api/dock-reports', methods=['GET'])
@login_required
def api_dock_list():
    """목록 조회 — 필터: vessel_id, status, is_template, q"""
    conds, params = ['1=1'], []

    is_tmpl = request.args.get('is_template')
    if is_tmpl is not None:
        conds.append('d.is_template = ?')
        params.append(1 if is_tmpl in ('1', 'true', 'yes') else 0)
    else:
        # 기본은 보고서만 (템플릿 제외)
        conds.append('d.is_template = 0')

    if request.args.get('vessel_id'):
        conds.append('d.vessel_id = ?')
        params.append(request.args.get('vessel_id'))

    if request.args.get('status'):
        conds.append('d.status = ?')
        params.append(request.args.get('status'))

    if request.args.get('q'):
        like = f'%{request.args.get("q")}%'
        conds.append('(d.title LIKE ? OR d.shipyard LIKE ? OR d.dock_no LIKE ?)')
        params += [like, like, like]

    sql = f'''
        SELECT d.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM dock_reports d
          JOIN vessels       v ON v.id = d.vessel_id
          LEFT JOIN supervisors s ON s.id = d.supervisor_id
         WHERE {' AND '.join(conds)}
         ORDER BY d.updated_at DESC, d.id DESC
    '''
    rows = query(sql, params)
    out = []
    for r in rows:
        d = _dock_to_dict(r)
        d['can_edit'] = _can_edit_dock_report(r)
        out.append(d)
    return jsonify(out)


@app.route('/api/dock-reports', methods=['POST'])
@login_required
def api_dock_create():
    d = request.get_json(silent=True) or {}
    vessel_id = d.get('vessel_id')
    title     = (d.get('title') or '').strip()
    if not vessel_id:
        return jsonify({'error': '선박을 선택하세요.'}), 400
    if not title:
        return jsonify({'error': '제목을 입력하세요.'}), 400
    if not query('SELECT id FROM vessels WHERE id=?', (vessel_id,), one=True):
        return jsonify({'error': '존재하지 않는 선박입니다.'}), 400

    # 권한: admin이거나, 자기 자신을 담당 감독으로 지정하는 경우만 생성 허용
    supervisor_id = d.get('supervisor_id') or None
    if session.get('role') != 'admin':
        my_sv = session.get('supervisor_id')
        if not my_sv:
            return jsonify({'error': '보고서 작성 권한이 없습니다. (담당 감독으로 등록된 계정만 가능)'}), 403
        # member는 자기 자신을 담당으로만 지정 가능
        if supervisor_id and int(supervisor_id) != my_sv:
            return jsonify({'error': '본인을 담당 감독으로 지정한 경우에만 생성할 수 있습니다.'}), 403
        # 미지정 시 자동으로 본인 지정
        if not supervisor_id:
            supervisor_id = my_sv

    is_template = 1 if d.get('is_template') else 0

    new_id = execute('''
        INSERT INTO dock_reports
            (vessel_id, supervisor_id, title, dock_no, shipyard,
             period_start, period_end, imo_no, gross_tonnage, dead_weight,
             approval_drafter, approval_team_lead, approval_director, approval_ceo,
             status, is_template, template_name, created_by)
        VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?)
    ''', (
        vessel_id,
        supervisor_id,
        title,
        d.get('dock_no') or None,
        d.get('shipyard') or None,
        d.get('period_start') or None,
        d.get('period_end') or None,
        d.get('imo_no') or None,
        d.get('gross_tonnage') or None,
        d.get('dead_weight') or None,
        d.get('approval_drafter') or None,
        d.get('approval_team_lead') or None,
        d.get('approval_director') or None,
        d.get('approval_ceo') or None,
        d.get('status') or 'draft',
        is_template,
        d.get('template_name') if is_template else None,
        session.get('display_name') or session.get('username') or '',
    ))
    return jsonify({'id': new_id, 'ok': True}), 201


@app.route('/api/dock-reports/<int:rid>', methods=['GET'])
@login_required
def api_dock_get(rid):
    """보고서 상세 — 메타 + 섹션 트리 + 블록 모두 포함"""
    r = query('''
        SELECT d.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM dock_reports d
          JOIN vessels       v ON v.id = d.vessel_id
          LEFT JOIN supervisors s ON s.id = d.supervisor_id
         WHERE d.id = ?
    ''', (rid,), one=True)
    if not r:
        abort(404)

    out = _dock_to_dict(r)
    out['can_edit'] = _can_edit_dock_report(r)

    # 섹션 + 블록 (Step 2에서 활용; 현재는 빈 리스트라도 채워줌)
    secs = query('''
        SELECT * FROM dock_report_sections
         WHERE report_id = ?
         ORDER BY display_order, id
    ''', (rid,))
    sec_list = [dict(s) for s in secs]

    sec_ids = [s['id'] for s in sec_list]
    blocks = []
    if sec_ids:
        placeholders = ','.join('?' for _ in sec_ids)
        blocks = query(f'''
            SELECT * FROM dock_report_blocks
             WHERE section_id IN ({placeholders})
             ORDER BY section_id, display_order, id
        ''', sec_ids)
    blocks_by_sec = {}
    for b in blocks:
        bd = dict(b)
        try:
            bd['content'] = json.loads(bd.pop('content_json'))
        except Exception:
            bd['content'] = {}
        blocks_by_sec.setdefault(bd['section_id'], []).append(bd)

    for s in sec_list:
        s['blocks'] = blocks_by_sec.get(s['id'], [])

    out['sections'] = sec_list
    return jsonify(out)


@app.route('/api/dock-reports/<int:rid>', methods=['PUT'])
@login_required
def api_dock_update(rid):
    """메타 정보 수정"""
    err = _require_dock_edit(rid)
    if err:
        return err
    d = request.get_json(silent=True) or {}

    updatable = {
        'vessel_id', 'supervisor_id', 'title', 'dock_no', 'shipyard',
        'period_start', 'period_end', 'imo_no', 'gross_tonnage', 'dead_weight',
        'approval_drafter', 'approval_team_lead', 'approval_director', 'approval_ceo',
        'status', 'template_name',
    }
    # supervisor_id 변경은 admin만 가능 (담당자가 자기 보고서를 남에게 넘기는 것 방지)
    if 'supervisor_id' in d and session.get('role') != 'admin':
        d.pop('supervisor_id', None)

    sets, params = [], []
    for k in updatable:
        if k in d:
            sets.append(f'{k} = ?')
            v = d.get(k)
            params.append(v if (v not in ('',)) else None)

    if not sets:
        return jsonify({'ok': True, 'updated': 0})

    sets.append("updated_at = datetime('now','localtime')")
    params.append(rid)
    execute(f'UPDATE dock_reports SET {", ".join(sets)} WHERE id = ?', params)
    return jsonify({'ok': True})


@app.route('/api/dock-reports/<int:rid>', methods=['DELETE'])
@login_required
def api_dock_delete(rid):
    err = _require_dock_edit(rid)
    if err:
        return err
    execute('DELETE FROM dock_reports WHERE id = ?', (rid,))
    # 섹션/블록은 ON DELETE CASCADE로 자동 삭제
    return jsonify({'ok': True})


def _touch_dock_report(rid):
    """보고서 updated_at 갱신 — 섹션/블록 변경 시 호출"""
    execute("UPDATE dock_reports SET updated_at=datetime('now','localtime') WHERE id=?",
            (rid,))


def _section_report_id(sid):
    r = query('SELECT report_id FROM dock_report_sections WHERE id=?', (sid,), one=True)
    return r['report_id'] if r else None


def _block_report_id(bid):
    r = query('''
        SELECT s.report_id FROM dock_report_blocks b
          JOIN dock_report_sections s ON s.id = b.section_id
         WHERE b.id = ?
    ''', (bid,), one=True)
    return r['report_id'] if r else None


# ─── Sections ─────────────────────────────────────────────────
@app.route('/api/dock-reports/<int:rid>/sections', methods=['POST'])
@login_required
def api_dock_section_create(rid):
    err = _require_dock_edit(rid)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip() or '새 섹션'
    parent_id = d.get('parent_id')
    if parent_id:
        # parent가 같은 report 내인지 확인
        p = query('SELECT report_id FROM dock_report_sections WHERE id=?',
                  (parent_id,), one=True)
        if not p or p['report_id'] != rid:
            return jsonify({'error': '잘못된 상위 섹션입니다.'}), 400

    # 같은 부모 아래 마지막 순서
    cond = 'parent_id IS NULL' if not parent_id else 'parent_id = ?'
    cp = (rid,) if not parent_id else (parent_id,)
    last = query(f'''
        SELECT COALESCE(MAX(display_order), -1) AS mx
          FROM dock_report_sections
         WHERE report_id = ? AND {cond}
    ''', (rid, *([parent_id] if parent_id else [])), one=True)
    next_order = (last['mx'] if last else -1) + 1

    new_id = execute('''
        INSERT INTO dock_report_sections (report_id, parent_id, title, display_order)
        VALUES (?,?,?,?)
    ''', (rid, parent_id, title, next_order))
    _touch_dock_report(rid)
    return jsonify({'id': new_id, 'ok': True}), 201


@app.route('/api/dock-sections/<int:sid>', methods=['PUT'])
@login_required
def api_dock_section_update(sid):
    err = _require_dock_edit_via_section(sid)
    if err:
        return err
    rid = _section_report_id(sid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip()
    if not title:
        return jsonify({'error': '제목을 입력하세요.'}), 400
    execute('UPDATE dock_report_sections SET title=? WHERE id=?', (title, sid))
    _touch_dock_report(rid)
    return jsonify({'ok': True})


@app.route('/api/dock-sections/<int:sid>', methods=['DELETE'])
@login_required
def api_dock_section_delete(sid):
    err = _require_dock_edit_via_section(sid)
    if err:
        return err
    rid = _section_report_id(sid)
    if not rid:
        abort(404)
    execute('DELETE FROM dock_report_sections WHERE id=?', (sid,))
    # 자식 섹션·블록 모두 CASCADE
    _touch_dock_report(rid)
    return jsonify({'ok': True})


@app.route('/api/dock-sections/<int:sid>/move', methods=['POST'])
@login_required
def api_dock_section_move(sid):
    """같은 부모 아래에서 위/아래로 한 칸 이동"""
    err = _require_dock_edit_via_section(sid)
    if err:
        return err
    rid = _section_report_id(sid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    direction = d.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'invalid direction'}), 400

    me = query('SELECT * FROM dock_report_sections WHERE id=?', (sid,), one=True)
    cond = 'parent_id IS NULL' if me['parent_id'] is None else 'parent_id = ?'
    args = (me['report_id'],) if me['parent_id'] is None else (me['report_id'], me['parent_id'])

    if direction == 'up':
        nb = query(f'''
            SELECT * FROM dock_report_sections
             WHERE report_id=? AND {cond} AND display_order < ?
             ORDER BY display_order DESC LIMIT 1
        ''', (*args, me['display_order']), one=True)
    else:
        nb = query(f'''
            SELECT * FROM dock_report_sections
             WHERE report_id=? AND {cond} AND display_order > ?
             ORDER BY display_order ASC LIMIT 1
        ''', (*args, me['display_order']), one=True)

    if not nb:
        return jsonify({'ok': True, 'moved': False})

    execute('UPDATE dock_report_sections SET display_order=? WHERE id=?',
            (nb['display_order'], me['id']))
    execute('UPDATE dock_report_sections SET display_order=? WHERE id=?',
            (me['display_order'], nb['id']))
    _touch_dock_report(rid)
    return jsonify({'ok': True, 'moved': True})


@app.route('/api/dock-sections/<int:sid>/reparent', methods=['POST'])
@login_required
def api_dock_section_reparent(sid):
    """섹션을 다른 부모로 이동.
       body: { "new_parent_id": null | int }
            null/None을 보내면 최상위(루트)로 이동.
    """
    err = _require_dock_edit_via_section(sid)
    if err:
        return err
    rid = _section_report_id(sid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    new_parent_id = d.get('new_parent_id')
    # 정수 또는 None만 허용
    if new_parent_id is not None:
        try:
            new_parent_id = int(new_parent_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid new_parent_id'}), 400

    me = query('SELECT * FROM dock_report_sections WHERE id=?', (sid,), one=True)
    if not me:
        abort(404)

    # 새 부모가 같은 보고서 안에 있어야 함
    if new_parent_id is not None:
        new_parent = query('SELECT * FROM dock_report_sections WHERE id=?',
                           (new_parent_id,), one=True)
        if not new_parent or new_parent['report_id'] != me['report_id']:
            return jsonify({'error': '같은 보고서의 섹션만 부모로 지정할 수 있습니다.'}), 400

        # 자기 자신을 부모로 설정 금지
        if new_parent_id == sid:
            return jsonify({'error': '자기 자신을 부모로 지정할 수 없습니다.'}), 400

        # 자손에게 옮기는 것 금지 (순환 참조 방지) - 후손 검사
        descendants = set()
        stack = [sid]
        while stack:
            cur = stack.pop()
            children = query(
                'SELECT id FROM dock_report_sections WHERE parent_id=?',
                (cur,))
            for c in children:
                if c['id'] in descendants:
                    continue
                descendants.add(c['id'])
                stack.append(c['id'])
        if new_parent_id in descendants:
            return jsonify({'error': '자기 자신의 하위 섹션으로 이동할 수 없습니다.'}), 400

    # 변경 사항 없음
    if (me['parent_id'] or None) == new_parent_id:
        return jsonify({'ok': True, 'moved': False})

    # 새 부모 아래의 마지막 display_order + 1로 배치
    if new_parent_id is None:
        max_ord = query('''
            SELECT MAX(display_order) AS m FROM dock_report_sections
             WHERE report_id=? AND parent_id IS NULL
        ''', (me['report_id'],), one=True)
    else:
        max_ord = query('''
            SELECT MAX(display_order) AS m FROM dock_report_sections
             WHERE report_id=? AND parent_id=?
        ''', (me['report_id'], new_parent_id), one=True)

    new_order = (max_ord['m'] or 0) + 1

    execute('''
        UPDATE dock_report_sections
           SET parent_id=?, display_order=?
         WHERE id=?
    ''', (new_parent_id, new_order, sid))
    _touch_dock_report(rid)
    return jsonify({'ok': True, 'moved': True,
                    'new_parent_id': new_parent_id,
                    'new_display_order': new_order})


# ─── Blocks ──────────────────────────────────────────────────
def _default_block_content(block_type):
    if block_type == 'paragraph':   return {'text': ''}
    if block_type == 'bullet_list': return {'items': ['']}
    if block_type == 'table':
        return {
            'headers': ['항목', '내용'],
            'rows':    [['', '']],
            'col_widths': [],   # 비어있으면 균등 배분, 있으면 px 단위 너비
        }
    if block_type == 'image':
        # 갤러리: 여러 장 가능. images=[] (비어있음) + columns=2 (2장씩 한 줄)
        return {'images': [], 'columns': 2}
    return {}


@app.route('/api/dock-sections/<int:sid>/blocks', methods=['POST'])
@login_required
def api_dock_block_create(sid):
    err = _require_dock_edit_via_section(sid)
    if err:
        return err
    rid = _section_report_id(sid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    bt = d.get('block_type')
    if bt not in ('paragraph', 'bullet_list', 'table', 'image'):
        return jsonify({'error': 'invalid block_type'}), 400
    content = d.get('content') or _default_block_content(bt)

    last = query('''
        SELECT COALESCE(MAX(display_order), -1) AS mx
          FROM dock_report_blocks WHERE section_id=?
    ''', (sid,), one=True)
    next_order = (last['mx'] if last else -1) + 1

    new_id = execute('''
        INSERT INTO dock_report_blocks (section_id, block_type, content_json, display_order)
        VALUES (?,?,?,?)
    ''', (sid, bt, json.dumps(content, ensure_ascii=False), next_order))
    _touch_dock_report(rid)
    return jsonify({'id': new_id, 'ok': True, 'content': content}), 201


@app.route('/api/dock-blocks/<int:bid>', methods=['PUT'])
@login_required
def api_dock_block_update(bid):
    err = _require_dock_edit_via_block(bid)
    if err:
        return err
    rid = _block_report_id(bid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    content = d.get('content')
    if content is None:
        return jsonify({'error': 'content가 필요합니다.'}), 400
    execute('UPDATE dock_report_blocks SET content_json=? WHERE id=?',
            (json.dumps(content, ensure_ascii=False), bid))
    _touch_dock_report(rid)
    return jsonify({'ok': True})


@app.route('/api/dock-blocks/<int:bid>', methods=['DELETE'])
@login_required
def api_dock_block_delete(bid):
    err = _require_dock_edit_via_block(bid)
    if err:
        return err
    rid = _block_report_id(bid)
    if not rid:
        abort(404)
    execute('DELETE FROM dock_report_blocks WHERE id=?', (bid,))
    _touch_dock_report(rid)
    return jsonify({'ok': True})


@app.route('/api/dock-blocks/<int:bid>/move', methods=['POST'])
@login_required
def api_dock_block_move(bid):
    err = _require_dock_edit_via_block(bid)
    if err:
        return err
    rid = _block_report_id(bid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    direction = d.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'invalid direction'}), 400

    me = query('SELECT * FROM dock_report_blocks WHERE id=?', (bid,), one=True)
    if direction == 'up':
        nb = query('''
            SELECT * FROM dock_report_blocks
             WHERE section_id=? AND display_order < ?
             ORDER BY display_order DESC LIMIT 1
        ''', (me['section_id'], me['display_order']), one=True)
    else:
        nb = query('''
            SELECT * FROM dock_report_blocks
             WHERE section_id=? AND display_order > ?
             ORDER BY display_order ASC LIMIT 1
        ''', (me['section_id'], me['display_order']), one=True)

    if not nb:
        return jsonify({'ok': True, 'moved': False})

    execute('UPDATE dock_report_blocks SET display_order=? WHERE id=?',
            (nb['display_order'], me['id']))
    execute('UPDATE dock_report_blocks SET display_order=? WHERE id=?',
            (me['display_order'], nb['id']))
    _touch_dock_report(rid)
    return jsonify({'ok': True, 'moved': True})


# ─── Image upload ────────────────────────────────────────────
# Word "그림 압축 — 웹(150ppi)" 기준에 맞춤
#   · 16cm 본문폭 × 150ppi ≈ 944px → 안전 마진 두고 장변 1280px
#   · JPEG quality 85 (사진용 표준 압축)
#   · EXIF orientation 적용 (스마트폰 회전 자동 보정)
DOCK_IMAGE_MAX_LONG_SIDE = 1280
DOCK_IMAGE_JPEG_QUALITY  = 85


def _process_uploaded_image(file_storage, dest_path,
                            max_long_side=DOCK_IMAGE_MAX_LONG_SIDE,
                            jpeg_quality=DOCK_IMAGE_JPEG_QUALITY):
    """
    업로드된 이미지를 리사이즈 + 재인코딩하여 dest_path에 저장.
    실패 시 원본을 그대로 저장하고 False 반환.
    성공 시 (final_path, original_size_bytes, final_size_bytes) 반환.
    dest_path의 확장자는 결과에 따라 .jpg로 변경될 수 있음 (PNG 투명 X일 때).
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        # Pillow 없으면 그냥 저장
        file_storage.save(dest_path)
        return dest_path, os.path.getsize(dest_path), os.path.getsize(dest_path)

    # 원본을 메모리에 읽어두기 (저장 실패 시 fallback용)
    file_storage.stream.seek(0)
    raw_bytes = file_storage.stream.read()
    original_size = len(raw_bytes)

    try:
        from io import BytesIO
        im = Image.open(BytesIO(raw_bytes))

        # EXIF orientation 적용
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass

        w, h = im.size
        long_side = max(w, h)

        # 리사이즈 필요 시
        if long_side > max_long_side:
            ratio = max_long_side / long_side
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            im = im.resize((new_w, new_h), Image.LANCZOS)

        # 저장 — PNG 투명도 있으면 PNG 유지, 아니면 JPEG로 통일
        ext_lower = dest_path.rsplit('.', 1)[-1].lower()
        has_alpha = (im.mode in ('RGBA', 'LA')) or (
            im.mode == 'P' and 'transparency' in im.info
        )

        if ext_lower == 'png' and has_alpha:
            # PNG 투명도 보존
            im.save(dest_path, 'PNG', optimize=True)
            final_path = dest_path
        else:
            # JPEG로 통일 (용량 작음)
            if im.mode != 'RGB':
                im = im.convert('RGB')
            # 확장자 .jpg로 통일
            base = dest_path.rsplit('.', 1)[0]
            final_path = base + '.jpg'
            im.save(final_path, 'JPEG',
                    quality=jpeg_quality,
                    optimize=True, progressive=True)

        return final_path, original_size, os.path.getsize(final_path)

    except Exception as e:
        # 처리 실패 → 원본 그대로 저장
        with open(dest_path, 'wb') as f:
            f.write(raw_bytes)
        return dest_path, original_size, len(raw_bytes)


@app.route('/api/dock-reports/<int:rid>/upload-image', methods=['POST'])
@login_required
def api_dock_upload_image(rid):
    err = _require_dock_edit(rid)
    if err:
        return err
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 비어있습니다.'}), 400

    # 확장자 화이트리스트 (이미지만)
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif', 'bmp'}:
        return jsonify({'error': '이미지 파일만 업로드 가능합니다.'}), 400

    # static/uploads/dock/ 폴더
    dock_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'dock')
    os.makedirs(dock_dir, exist_ok=True)

    # 임시 파일명 (확장자는 처리 함수가 결정)
    import time
    base_fname = f'dock-{rid}-{int(time.time()*1000)}-{secrets.token_hex(4)}'
    initial_path = os.path.join(dock_dir, f'{base_fname}.{ext}')

    # 리사이즈 + 재인코딩
    final_path, orig_size, final_size = _process_uploaded_image(f, initial_path)
    final_fname = os.path.basename(final_path)

    url = url_for('static', filename=f'uploads/dock/{final_fname}')

    # 압축률 계산 (로깅용)
    reduction = 0
    if orig_size > 0:
        reduction = int((1 - final_size / orig_size) * 100)

    return jsonify({
        'ok': True,
        'filename': final_fname,
        'url': url,
        'original_kb': round(orig_size / 1024, 1),
        'final_kb':    round(final_size / 1024, 1),
        'reduction_pct': reduction,
    }), 201


# ─── Word / PDF Export ───────────────────────────────────────
def _get_full_report_data(rid):
    """build_docx에 넘길 보고서 데이터 빌드 — api_dock_get과 동일한 구조"""
    r = query('''
        SELECT d.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               v.vessel_type AS vessel_type,
               s.name       AS supervisor_name
          FROM dock_reports d
          JOIN vessels       v ON v.id = d.vessel_id
          LEFT JOIN supervisors s ON s.id = d.supervisor_id
         WHERE d.id = ?
    ''', (rid,), one=True)
    if not r:
        return None
    out = dict(r)

    secs = query('''
        SELECT * FROM dock_report_sections
         WHERE report_id = ?
         ORDER BY display_order, id
    ''', (rid,))
    sec_list = [dict(s) for s in secs]
    sec_ids = [s['id'] for s in sec_list]
    blocks_by_sec = {}
    if sec_ids:
        placeholders = ','.join('?' for _ in sec_ids)
        blocks = query(f'''
            SELECT * FROM dock_report_blocks
             WHERE section_id IN ({placeholders})
             ORDER BY section_id, display_order, id
        ''', sec_ids)
        for b in blocks:
            bd = dict(b)
            try:
                bd['content'] = json.loads(bd.pop('content_json'))
            except Exception:
                bd['content'] = {}
            blocks_by_sec.setdefault(bd['section_id'], []).append(bd)
    for s in sec_list:
        s['blocks'] = blocks_by_sec.get(s['id'], [])
    out['sections'] = sec_list
    return out


def _safe_filename(s):
    """파일명에서 OS 비호환 문자 제거"""
    import re
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s)
    s = s.strip().strip('.')
    return s[:80] or 'report'


@app.route('/api/dock-reports/<int:rid>/export/docx')
@login_required
def api_dock_export_docx(rid):
    try:
        from dock_report_docx import build_docx
    except ImportError as e:
        return jsonify({'error': f'docx 생성 모듈 로드 실패: {e}'}), 500

    data = _get_full_report_data(rid)
    if not data:
        abort(404)

    try:
        docx_bytes = build_docx(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'문서 생성 실패: {e}'}), 500

    from io import BytesIO
    from flask import send_file
    fname = _safe_filename(data.get('title') or f'DryDock_Report_{rid}') + '.docx'
    return send_file(
        BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=fname,
    )


@app.route('/api/dock-reports/<int:rid>/export/pdf')
@login_required
def api_dock_export_pdf(rid):
    try:
        from dock_report_docx import build_docx
    except ImportError as e:
        return jsonify({'error': f'docx 생성 모듈 로드 실패: {e}'}), 500

    data = _get_full_report_data(rid)
    if not data:
        abort(404)

    # 1) docx 생성
    try:
        docx_bytes = build_docx(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'문서 생성 실패: {e}'}), 500

    # 2) docx → pdf (LibreOffice headless)
    import tempfile, subprocess, shutil, os as _os
    try:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = _os.path.join(tmp, 'report.docx')
            with open(docx_path, 'wb') as f:
                f.write(docx_bytes)

            soffice = shutil.which('soffice') or shutil.which('libreoffice')
            if not soffice:
                return jsonify({
                    'error': 'PDF 변환 도구(LibreOffice)가 설치되지 않았습니다. '
                             '서버에 sudo dnf install -y libreoffice-core libreoffice-writer 명령으로 설치해주세요.'
                }), 500

            proc = subprocess.run(
                [soffice, '--headless', '--convert-to', 'pdf',
                 '--outdir', tmp, docx_path],
                capture_output=True, timeout=120,
            )
            if proc.returncode != 0:
                return jsonify({
                    'error': f'PDF 변환 실패: {proc.stderr.decode("utf-8", errors="ignore")[:500]}'
                }), 500

            pdf_path = _os.path.join(tmp, 'report.pdf')
            if not _os.path.exists(pdf_path):
                return jsonify({'error': 'PDF 파일이 생성되지 않았습니다.'}), 500

            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'PDF 변환 시간 초과 (2분).'}), 500
    except Exception as e:
        return jsonify({'error': f'PDF 변환 오류: {e}'}), 500

    from io import BytesIO
    from flask import send_file
    fname = _safe_filename(data.get('title') or f'DryDock_Report_{rid}') + '.pdf'
    return send_file(
        BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=fname,
    )


# ═════════════════════════════════════════════════════════════════
#  API — Boarding Report (방선보고서)
#   · 구조는 Dry Dock Report와 거의 동일 (별도 테이블, 별도 권한 체크)
#   · 메타 필드만 다름 (port / boarding_start_end / master / chief_eng 등)
# ═════════════════════════════════════════════════════════════════
def _can_edit_boarding_report(report_row_or_id):
    if session.get('role') == 'admin':
        return True
    my_sv = session.get('supervisor_id')
    if not my_sv:
        return False
    if isinstance(report_row_or_id, int):
        r = query('SELECT supervisor_id FROM boarding_reports WHERE id=?',
                  (report_row_or_id,), one=True)
        if not r:
            return False
        report_sv = r['supervisor_id']
    else:
        report_sv = report_row_or_id.get('supervisor_id') if hasattr(report_row_or_id, 'get') \
                    else report_row_or_id['supervisor_id']
    return report_sv is not None and report_sv == my_sv


def _require_brep_edit(rid):
    if not query('SELECT id FROM boarding_reports WHERE id=?', (rid,), one=True):
        abort(404)
    if not _can_edit_boarding_report(rid):
        return jsonify({'error': '이 보고서를 편집할 권한이 없습니다. (담당 감독 또는 관리자만 수정 가능)'}), 403
    return None


def _brep_section_report_id(sid):
    r = query('SELECT report_id FROM boarding_report_sections WHERE id=?', (sid,), one=True)
    return r['report_id'] if r else None


def _brep_block_report_id(bid):
    r = query('''
        SELECT s.report_id FROM boarding_report_blocks b
          JOIN boarding_report_sections s ON s.id = b.section_id
         WHERE b.id = ?
    ''', (bid,), one=True)
    return r['report_id'] if r else None


def _require_brep_edit_via_section(sid):
    rid = _brep_section_report_id(sid)
    if not rid:
        abort(404)
    if not _can_edit_boarding_report(rid):
        return jsonify({'error': '이 보고서를 편집할 권한이 없습니다.'}), 403
    return None


def _require_brep_edit_via_block(bid):
    rid = _brep_block_report_id(bid)
    if not rid:
        abort(404)
    if not _can_edit_boarding_report(rid):
        return jsonify({'error': '이 보고서를 편집할 권한이 없습니다.'}), 403
    return None


def _touch_brep(rid):
    execute("UPDATE boarding_reports SET updated_at=datetime('now','localtime') WHERE id=?",
            (rid,))


def _brep_to_dict(row):
    return dict(row)


# ─── Boarding Report — 보고서 메타 CRUD ─────────────────────────
@app.route('/api/boarding-reports', methods=['GET'])
@login_required
def api_brep_list():
    conds, params = ['1=1'], []

    is_tmpl = request.args.get('is_template')
    if is_tmpl is not None:
        conds.append('b.is_template = ?')
        params.append(1 if is_tmpl in ('1', 'true', 'yes') else 0)
    else:
        conds.append('b.is_template = 0')

    if request.args.get('vessel_id'):
        conds.append('b.vessel_id = ?')
        params.append(request.args.get('vessel_id'))

    if request.args.get('status'):
        conds.append('b.status = ?')
        params.append(request.args.get('status'))

    if request.args.get('q'):
        like = f'%{request.args.get("q")}%'
        conds.append('(b.title LIKE ? OR b.port LIKE ?)')
        params += [like, like]

    sql = f'''
        SELECT b.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM boarding_reports b
          JOIN vessels       v ON v.id = b.vessel_id
          LEFT JOIN supervisors s ON s.id = b.supervisor_id
         WHERE {' AND '.join(conds)}
         ORDER BY b.updated_at DESC, b.id DESC
    '''
    rows = query(sql, params)
    out = []
    for r in rows:
        d = _brep_to_dict(r)
        d['can_edit'] = _can_edit_boarding_report(r)
        out.append(d)
    return jsonify(out)


@app.route('/api/boarding-reports', methods=['POST'])
@login_required
def api_brep_create():
    d = request.get_json(silent=True) or {}
    vessel_id = d.get('vessel_id')
    title     = (d.get('title') or '').strip()
    if not vessel_id:
        return jsonify({'error': '선박을 선택하세요.'}), 400
    if not title:
        return jsonify({'error': '제목을 입력하세요.'}), 400
    if not query('SELECT id FROM vessels WHERE id=?', (vessel_id,), one=True):
        return jsonify({'error': '존재하지 않는 선박입니다.'}), 400

    supervisor_id = d.get('supervisor_id') or None
    if session.get('role') != 'admin':
        my_sv = session.get('supervisor_id')
        if not my_sv:
            return jsonify({'error': '보고서 작성 권한이 없습니다. (담당 감독으로 등록된 계정만 가능)'}), 403
        if supervisor_id and int(supervisor_id) != my_sv:
            return jsonify({'error': '본인을 담당 감독으로 지정한 경우에만 생성할 수 있습니다.'}), 403
        if not supervisor_id:
            supervisor_id = my_sv

    is_template = 1 if d.get('is_template') else 0

    new_id = execute('''
        INSERT INTO boarding_reports
            (vessel_id, supervisor_id, title, port,
             boarding_start, boarding_end,
             master_name, master_board_date, chief_eng_name, chief_eng_board_date,
             sv_checklist_score,
             approval_drafter, approval_team_lead, approval_director, approval_ceo,
             status, is_template, template_name, created_by)
        VALUES (?,?,?,?, ?,?, ?,?,?,?, ?, ?,?,?,?, ?,?,?,?)
    ''', (
        vessel_id, supervisor_id, title,
        d.get('port') or None,
        d.get('boarding_start') or None,
        d.get('boarding_end') or None,
        d.get('master_name') or None,
        d.get('master_board_date') or None,
        d.get('chief_eng_name') or None,
        d.get('chief_eng_board_date') or None,
        d.get('sv_checklist_score') or None,
        d.get('approval_drafter') or None,
        d.get('approval_team_lead') or None,
        d.get('approval_director') or None,
        d.get('approval_ceo') or None,
        d.get('status') or 'draft',
        is_template,
        d.get('template_name') if is_template else None,
        session.get('display_name') or session.get('username') or '',
    ))

    # Step 2에서 활용: 신규 보고서 생성 시 기본 섹션 자동 생성
    # (방선보고서 + Defect List 통합본 양식)
    default_sections = [
        ('Inspector Opinion', None),
        ('Vessel General Condition & Deficiencies', None),
        ('첨부 사진', None),
        ('Defect List', None),
    ]
    for idx, (title_text, parent) in enumerate(default_sections):
        execute('''
            INSERT INTO boarding_report_sections
                (report_id, parent_id, title, display_order)
            VALUES (?, ?, ?, ?)
        ''', (new_id, parent, title_text, idx))

    return jsonify({'id': new_id, 'ok': True}), 201


@app.route('/api/boarding-reports/<int:rid>', methods=['GET'])
@login_required
def api_brep_get(rid):
    r = query('''
        SELECT b.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM boarding_reports b
          JOIN vessels       v ON v.id = b.vessel_id
          LEFT JOIN supervisors s ON s.id = b.supervisor_id
         WHERE b.id = ?
    ''', (rid,), one=True)
    if not r:
        abort(404)

    out = _brep_to_dict(r)
    out['can_edit'] = _can_edit_boarding_report(r)

    secs = query('''
        SELECT * FROM boarding_report_sections
         WHERE report_id = ?
         ORDER BY display_order, id
    ''', (rid,))
    sec_list = [dict(s) for s in secs]

    sec_ids = [s['id'] for s in sec_list]
    blocks = []
    if sec_ids:
        placeholders = ','.join('?' for _ in sec_ids)
        blocks = query(f'''
            SELECT * FROM boarding_report_blocks
             WHERE section_id IN ({placeholders})
             ORDER BY section_id, display_order, id
        ''', sec_ids)
    blocks_by_sec = {}
    for b in blocks:
        bd = dict(b)
        try:
            bd['content'] = json.loads(bd.pop('content_json'))
        except Exception:
            bd['content'] = {}
        blocks_by_sec.setdefault(bd['section_id'], []).append(bd)

    for s in sec_list:
        s['blocks'] = blocks_by_sec.get(s['id'], [])

    out['sections'] = sec_list
    return jsonify(out)


@app.route('/api/boarding-reports/<int:rid>', methods=['PUT'])
@login_required
def api_brep_update(rid):
    err = _require_brep_edit(rid)
    if err:
        return err
    d = request.get_json(silent=True) or {}

    updatable = {
        'vessel_id', 'supervisor_id', 'title', 'port',
        'boarding_start', 'boarding_end',
        'master_name', 'master_board_date', 'chief_eng_name', 'chief_eng_board_date',
        'sv_checklist_score',
        'approval_drafter', 'approval_team_lead', 'approval_director', 'approval_ceo',
        'status', 'template_name',
    }
    if 'supervisor_id' in d and session.get('role') != 'admin':
        d.pop('supervisor_id', None)

    sets, params = [], []
    for k in updatable:
        if k in d:
            sets.append(f'{k} = ?')
            v = d.get(k)
            params.append(v if (v not in ('',)) else None)

    if not sets:
        return jsonify({'ok': True, 'updated': 0})

    sets.append("updated_at = datetime('now','localtime')")
    params.append(rid)
    execute(f'UPDATE boarding_reports SET {", ".join(sets)} WHERE id = ?', params)
    return jsonify({'ok': True})


@app.route('/api/boarding-reports/<int:rid>', methods=['DELETE'])
@login_required
def api_brep_delete(rid):
    err = _require_brep_edit(rid)
    if err:
        return err
    execute('DELETE FROM boarding_reports WHERE id = ?', (rid,))
    return jsonify({'ok': True})


# ─── Boarding Report — 섹션 CRUD ────────────────────────────────
@app.route('/api/boarding-reports/<int:rid>/sections', methods=['POST'])
@login_required
def api_brep_section_create(rid):
    err = _require_brep_edit(rid)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip() or '새 섹션'
    parent_id = d.get('parent_id')
    if parent_id:
        p = query('SELECT report_id FROM boarding_report_sections WHERE id=?',
                  (parent_id,), one=True)
        if not p or p['report_id'] != rid:
            return jsonify({'error': '잘못된 상위 섹션입니다.'}), 400

    cond = 'parent_id IS NULL' if not parent_id else 'parent_id = ?'
    last = query(f'''
        SELECT COALESCE(MAX(display_order), -1) AS mx
          FROM boarding_report_sections
         WHERE report_id = ? AND {cond}
    ''', (rid, *([parent_id] if parent_id else [])), one=True)
    next_order = (last['mx'] if last else -1) + 1

    new_id = execute('''
        INSERT INTO boarding_report_sections (report_id, parent_id, title, display_order)
        VALUES (?,?,?,?)
    ''', (rid, parent_id, title, next_order))
    _touch_brep(rid)
    return jsonify({'id': new_id, 'ok': True}), 201


@app.route('/api/boarding-sections/<int:sid>', methods=['PUT'])
@login_required
def api_brep_section_update(sid):
    err = _require_brep_edit_via_section(sid)
    if err:
        return err
    rid = _brep_section_report_id(sid)
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip()
    if not title:
        return jsonify({'error': '제목을 입력하세요.'}), 400
    execute('UPDATE boarding_report_sections SET title=? WHERE id=?', (title, sid))
    _touch_brep(rid)
    return jsonify({'ok': True})


@app.route('/api/boarding-sections/<int:sid>', methods=['DELETE'])
@login_required
def api_brep_section_delete(sid):
    err = _require_brep_edit_via_section(sid)
    if err:
        return err
    rid = _brep_section_report_id(sid)
    execute('DELETE FROM boarding_report_sections WHERE id=?', (sid,))
    _touch_brep(rid)
    return jsonify({'ok': True})


@app.route('/api/boarding-sections/<int:sid>/move', methods=['POST'])
@login_required
def api_brep_section_move(sid):
    err = _require_brep_edit_via_section(sid)
    if err:
        return err
    rid = _brep_section_report_id(sid)
    d = request.get_json(silent=True) or {}
    direction = d.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'invalid direction'}), 400

    me = query('SELECT * FROM boarding_report_sections WHERE id=?', (sid,), one=True)
    cond = 'parent_id IS NULL' if me['parent_id'] is None else 'parent_id = ?'
    args = (me['report_id'],) if me['parent_id'] is None else (me['report_id'], me['parent_id'])

    if direction == 'up':
        nb = query(f'''
            SELECT * FROM boarding_report_sections
             WHERE report_id=? AND {cond} AND display_order < ?
             ORDER BY display_order DESC LIMIT 1
        ''', (*args, me['display_order']), one=True)
    else:
        nb = query(f'''
            SELECT * FROM boarding_report_sections
             WHERE report_id=? AND {cond} AND display_order > ?
             ORDER BY display_order ASC LIMIT 1
        ''', (*args, me['display_order']), one=True)

    if not nb:
        return jsonify({'ok': True, 'moved': False})

    execute('UPDATE boarding_report_sections SET display_order=? WHERE id=?',
            (nb['display_order'], me['id']))
    execute('UPDATE boarding_report_sections SET display_order=? WHERE id=?',
            (me['display_order'], nb['id']))
    _touch_brep(rid)
    return jsonify({'ok': True, 'moved': True})


@app.route('/api/boarding-sections/<int:sid>/reparent', methods=['POST'])
@login_required
def api_brep_section_reparent(sid):
    """섹션을 다른 부모로 이동.
       body: { "new_parent_id": null | int }
    """
    err = _require_brep_edit_via_section(sid)
    if err:
        return err
    rid = _brep_section_report_id(sid)
    if not rid:
        abort(404)
    d = request.get_json(silent=True) or {}
    new_parent_id = d.get('new_parent_id')
    if new_parent_id is not None:
        try:
            new_parent_id = int(new_parent_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid new_parent_id'}), 400

    me = query('SELECT * FROM boarding_report_sections WHERE id=?', (sid,), one=True)
    if not me:
        abort(404)

    if new_parent_id is not None:
        new_parent = query('SELECT * FROM boarding_report_sections WHERE id=?',
                           (new_parent_id,), one=True)
        if not new_parent or new_parent['report_id'] != me['report_id']:
            return jsonify({'error': '같은 보고서의 섹션만 부모로 지정할 수 있습니다.'}), 400
        if new_parent_id == sid:
            return jsonify({'error': '자기 자신을 부모로 지정할 수 없습니다.'}), 400

        descendants = set()
        stack = [sid]
        while stack:
            cur = stack.pop()
            children = query(
                'SELECT id FROM boarding_report_sections WHERE parent_id=?',
                (cur,))
            for c in children:
                if c['id'] in descendants:
                    continue
                descendants.add(c['id'])
                stack.append(c['id'])
        if new_parent_id in descendants:
            return jsonify({'error': '자기 자신의 하위 섹션으로 이동할 수 없습니다.'}), 400

    if (me['parent_id'] or None) == new_parent_id:
        return jsonify({'ok': True, 'moved': False})

    if new_parent_id is None:
        max_ord = query('''
            SELECT MAX(display_order) AS m FROM boarding_report_sections
             WHERE report_id=? AND parent_id IS NULL
        ''', (me['report_id'],), one=True)
    else:
        max_ord = query('''
            SELECT MAX(display_order) AS m FROM boarding_report_sections
             WHERE report_id=? AND parent_id=?
        ''', (me['report_id'], new_parent_id), one=True)

    new_order = (max_ord['m'] or 0) + 1

    execute('''
        UPDATE boarding_report_sections
           SET parent_id=?, display_order=?
         WHERE id=?
    ''', (new_parent_id, new_order, sid))
    _touch_brep(rid)
    return jsonify({'ok': True, 'moved': True,
                    'new_parent_id': new_parent_id,
                    'new_display_order': new_order})


# ─── Boarding Report — 블록 CRUD ────────────────────────────────
def _brep_default_block_content(block_type):
    if block_type == 'paragraph':   return {'text': ''}
    if block_type == 'bullet_list': return {'items': [{'text': '', 'indent': 0}], 'marker': 'bullet'}
    if block_type == 'table':
        return {'headers': ['항목', '내용'], 'rows': [['', '']], 'col_widths': []}
    if block_type == 'image':
        return {'images': [], 'columns': 2}
    if block_type == 'info_table':
        # 방선보고서 헤더용 (Label-Value 쌍)
        return {'rows': [
            {'label': 'Vessel',    'value': ''},
            {'label': 'Port',      'value': ''},
            {'label': 'Inspector', 'value': ''},
            {'label': 'Date/Time', 'value': ''},
        ]}
    if block_type == 'defect_table':
        # Defect List 항목 리스트 (각 항목: 사진 + 발견사항 + 조치사항 + Risk)
        return {'items': []}
    return {}


@app.route('/api/boarding-sections/<int:sid>/blocks', methods=['POST'])
@login_required
def api_brep_block_create(sid):
    err = _require_brep_edit_via_section(sid)
    if err:
        return err
    rid = _brep_section_report_id(sid)
    d = request.get_json(silent=True) or {}
    bt = d.get('block_type')
    if bt not in ('paragraph','bullet_list','table','image','info_table','defect_table'):
        return jsonify({'error': 'invalid block_type'}), 400
    content = d.get('content') or _brep_default_block_content(bt)

    last = query('''
        SELECT COALESCE(MAX(display_order), -1) AS mx
          FROM boarding_report_blocks WHERE section_id=?
    ''', (sid,), one=True)
    next_order = (last['mx'] if last else -1) + 1

    new_id = execute('''
        INSERT INTO boarding_report_blocks (section_id, block_type, content_json, display_order)
        VALUES (?,?,?,?)
    ''', (sid, bt, json.dumps(content, ensure_ascii=False), next_order))
    _touch_brep(rid)
    return jsonify({'id': new_id, 'ok': True, 'content': content}), 201


@app.route('/api/boarding-blocks/<int:bid>', methods=['PUT'])
@login_required
def api_brep_block_update(bid):
    err = _require_brep_edit_via_block(bid)
    if err:
        return err
    rid = _brep_block_report_id(bid)
    d = request.get_json(silent=True) or {}
    content = d.get('content')
    if content is None:
        return jsonify({'error': 'content가 필요합니다.'}), 400
    execute('UPDATE boarding_report_blocks SET content_json=? WHERE id=?',
            (json.dumps(content, ensure_ascii=False), bid))
    _touch_brep(rid)
    return jsonify({'ok': True})


@app.route('/api/boarding-blocks/<int:bid>', methods=['DELETE'])
@login_required
def api_brep_block_delete(bid):
    err = _require_brep_edit_via_block(bid)
    if err:
        return err
    rid = _brep_block_report_id(bid)
    execute('DELETE FROM boarding_report_blocks WHERE id=?', (bid,))
    _touch_brep(rid)
    return jsonify({'ok': True})


@app.route('/api/boarding-blocks/<int:bid>/move', methods=['POST'])
@login_required
def api_brep_block_move(bid):
    err = _require_brep_edit_via_block(bid)
    if err:
        return err
    rid = _brep_block_report_id(bid)
    d = request.get_json(silent=True) or {}
    direction = d.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'invalid direction'}), 400

    me = query('SELECT * FROM boarding_report_blocks WHERE id=?', (bid,), one=True)
    if direction == 'up':
        nb = query('''
            SELECT * FROM boarding_report_blocks
             WHERE section_id=? AND display_order < ?
             ORDER BY display_order DESC LIMIT 1
        ''', (me['section_id'], me['display_order']), one=True)
    else:
        nb = query('''
            SELECT * FROM boarding_report_blocks
             WHERE section_id=? AND display_order > ?
             ORDER BY display_order ASC LIMIT 1
        ''', (me['section_id'], me['display_order']), one=True)

    if not nb:
        return jsonify({'ok': True, 'moved': False})

    execute('UPDATE boarding_report_blocks SET display_order=? WHERE id=?',
            (nb['display_order'], me['id']))
    execute('UPDATE boarding_report_blocks SET display_order=? WHERE id=?',
            (me['display_order'], nb['id']))
    _touch_brep(rid)
    return jsonify({'ok': True, 'moved': True})


# ─── Boarding Report — 이미지 업로드 ────────────────────────────
# (dock/ 폴더와 분리하기 위해 별도 boarding/ 폴더 사용)
@app.route('/api/boarding-reports/<int:rid>/upload-image', methods=['POST'])
@login_required
def api_brep_upload_image(rid):
    err = _require_brep_edit(rid)
    if err:
        return err
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 비어있습니다.'}), 400

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif', 'bmp'}:
        return jsonify({'error': '이미지 파일만 업로드 가능합니다.'}), 400

    boarding_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'boarding')
    os.makedirs(boarding_dir, exist_ok=True)

    import time
    base_fname = f'brep-{rid}-{int(time.time()*1000)}-{secrets.token_hex(4)}'
    initial_path = os.path.join(boarding_dir, f'{base_fname}.{ext}')

    # Dock Report와 동일한 이미지 압축 로직 사용
    final_path, orig_size, final_size = _process_uploaded_image(f, initial_path)
    final_fname = os.path.basename(final_path)

    url = url_for('static', filename=f'uploads/boarding/{final_fname}')
    reduction = 0
    if orig_size > 0:
        reduction = int((1 - final_size / orig_size) * 100)

    return jsonify({
        'ok': True,
        'filename': final_fname,
        'url': url,
        'original_kb': round(orig_size / 1024, 1),
        'final_kb':    round(final_size / 1024, 1),
        'reduction_pct': reduction,
    }), 201


# ─── Boarding Report Word/PDF Export ────────────────────────────
def _get_full_brep_data(rid):
    r = query('''
        SELECT b.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM boarding_reports b
          JOIN vessels       v ON v.id = b.vessel_id
          LEFT JOIN supervisors s ON s.id = b.supervisor_id
         WHERE b.id = ?
    ''', (rid,), one=True)
    if not r:
        return None
    out = dict(r)

    secs = query('''
        SELECT * FROM boarding_report_sections
         WHERE report_id = ?
         ORDER BY display_order, id
    ''', (rid,))
    sec_list = [dict(s) for s in secs]
    sec_ids = [s['id'] for s in sec_list]
    blocks_by_sec = {}
    if sec_ids:
        placeholders = ','.join('?' for _ in sec_ids)
        blocks = query(f'''
            SELECT * FROM boarding_report_blocks
             WHERE section_id IN ({placeholders})
             ORDER BY section_id, display_order, id
        ''', sec_ids)
        for b in blocks:
            bd = dict(b)
            try:
                bd['content'] = json.loads(bd.pop('content_json'))
            except Exception:
                bd['content'] = {}
            blocks_by_sec.setdefault(bd['section_id'], []).append(bd)
    for s in sec_list:
        s['blocks'] = blocks_by_sec.get(s['id'], [])
    out['sections'] = sec_list
    return out


@app.route('/api/boarding-reports/<int:rid>/export/docx')
@login_required
def api_brep_export_docx(rid):
    try:
        from boarding_report_docx import build_docx
    except ImportError as e:
        return jsonify({'error': f'docx 생성 모듈 로드 실패: {e}'}), 500

    data = _get_full_brep_data(rid)
    if not data:
        abort(404)
    try:
        docx_bytes = build_docx(data)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': f'문서 생성 실패: {e}'}), 500

    from io import BytesIO
    from flask import send_file
    fname = _safe_filename(data.get('title') or f'BoardingReport_{rid}') + '.docx'
    return send_file(
        BytesIO(docx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name=fname,
    )


@app.route('/api/boarding-reports/<int:rid>/export/pdf')
@login_required
def api_brep_export_pdf(rid):
    try:
        from boarding_report_docx import build_docx
    except ImportError as e:
        return jsonify({'error': f'docx 생성 모듈 로드 실패: {e}'}), 500

    data = _get_full_brep_data(rid)
    if not data:
        abort(404)
    try:
        docx_bytes = build_docx(data)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': f'문서 생성 실패: {e}'}), 500

    import tempfile, subprocess, shutil, os as _os
    try:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = _os.path.join(tmp, 'report.docx')
            with open(docx_path, 'wb') as f:
                f.write(docx_bytes)
            soffice = shutil.which('soffice') or shutil.which('libreoffice')
            if not soffice:
                return jsonify({
                    'error': 'PDF 변환 도구(LibreOffice)가 설치되지 않았습니다. '
                             'sudo dnf install -y libreoffice-core libreoffice-writer'
                }), 500
            proc = subprocess.run(
                [soffice, '--headless', '--convert-to', 'pdf',
                 '--outdir', tmp, docx_path],
                capture_output=True, timeout=120,
            )
            if proc.returncode != 0:
                return jsonify({
                    'error': f'PDF 변환 실패: {proc.stderr.decode("utf-8", errors="ignore")[:500]}'
                }), 500
            pdf_path = _os.path.join(tmp, 'report.pdf')
            if not _os.path.exists(pdf_path):
                return jsonify({'error': 'PDF 파일이 생성되지 않았습니다.'}), 500
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'PDF 변환 시간 초과 (2분).'}), 500
    except Exception as e:
        return jsonify({'error': f'PDF 변환 오류: {e}'}), 500

    from io import BytesIO
    from flask import send_file
    fname = _safe_filename(data.get('title') or f'BoardingReport_{rid}') + '.pdf'
    return send_file(
        BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=fname,
    )


# ═════════════════════════════════════════════════════════════════
#  API — attachments
# ═════════════════════════════════════════════════════════════════
def _ext_allowed(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


@app.route('/api/issues/<int:iid>/attachments', methods=['POST'])
@login_required
def api_attachment_upload(iid):
    if not query('SELECT id FROM issues WHERE id=?', (iid,), one=True):
        abort(404)
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 비어있습니다.'}), 400
    if not _ext_allowed(f.filename):
        return jsonify({'error': '허용되지 않는 파일 형식입니다.'}), 400

    ext = f.filename.rsplit('.', 1)[1].lower()
    stored = f'{uuid.uuid4().hex}.{ext}'
    save_path = os.path.join(UPLOAD_DIR, stored)
    f.save(save_path)
    size = os.path.getsize(save_path)
    aid = execute('''
        INSERT INTO attachments
            (issue_id, filename, stored_name, file_size, mime_type, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (iid, secure_filename(f.filename), stored, size,
          f.mimetype or '', session.get('username')))
    return jsonify({
        'id': aid,
        'filename': f.filename,
        'stored_name': stored,
        'file_size': size,
    }), 201


@app.route('/api/attachments/<int:aid>')
@login_required
def api_attachment_download(aid):
    a = query('SELECT * FROM attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    # ?inline=1 이면 브라우저에서 바로 표시 (이미지 썸네일 / PDF 미리보기용)
    inline = request.args.get('inline') == '1'
    return send_from_directory(
        UPLOAD_DIR, a['stored_name'],
        as_attachment=not inline,
        download_name=a['filename'],
    )


@app.route('/api/attachments/<int:aid>', methods=['DELETE'])
@login_required
def api_attachment_delete(aid):
    a = query('SELECT * FROM attachments WHERE id=?', (aid,), one=True)
    if not a:
        abort(404)
    p = os.path.join(UPLOAD_DIR, a['stored_name'])
    if os.path.exists(p):
        os.remove(p)
    execute('DELETE FROM attachments WHERE id=?', (aid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  출장 경비 (Business Trip Expense) — 영수증 추출/증빙
# ═════════════════════════════════════════════════════════════════
RECEIPT_IMAGE_MAX_LONG_SIDE = 1568   # 영수증 작은 글씨 가독성 위해 dock(1280)보다 크게
RECEIPT_IMAGE_JPEG_QUALITY  = 88
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL   = os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite')

# 용도별 모델 — /etc/trmt.env 에서 지정 (미지정 시 GEMINI_MODEL 사용)
#   MODEL_SUMMARY  : 요약        (텍스트)
#   MODEL_TRANSLATE: 영문 번역    (텍스트)
#   MODEL_FINDINGS : 리포트 추출  (멀티모달 필요)
#   MODEL_REMARK   : 리마크 요약  (텍스트)
#   MODEL_RECEIPT  : 영수증 비전  (멀티모달 필수)
_MODEL_ENV = {
    'summary':   'MODEL_SUMMARY',
    'translate': 'MODEL_TRANSLATE',
    'findings':  'MODEL_FINDINGS',
    'remark':    'MODEL_REMARK',
    'receipt':   'MODEL_RECEIPT',
}


def _model_for(purpose):
    """용도별 모델 ID 반환 (환경변수 우선, 없으면 기본 GEMINI_MODEL)."""
    env = _MODEL_ENV.get(purpose)
    return (os.environ.get(env) if env else None) or GEMINI_MODEL


def _trip_owned(t):
    if session.get('role') == 'admin':
        return True
    return t['supervisor_id'] is not None and t['supervisor_id'] == session.get('supervisor_id')


def _get_trip_for_edit(tid):
    """편집용 trip row 조회. (trip, None) 또는 (None, error_response)."""
    t = query('SELECT * FROM biz_trips WHERE id=?', (tid,), one=True)
    if not t:
        return None, (jsonify({'error': 'not found'}), 404)
    if not _trip_owned(t):
        return None, (jsonify({'error': '권한이 없습니다.'}), 403)
    return t, None


def _trip_to_dict(r):
    d = dict(r)
    try:
        d['corp_cards'] = json.loads(r['corp_cards']) if r['corp_cards'] else []
    except Exception:
        d['corp_cards'] = []
    return d


def _delete_receipt_image(fname):
    if not fname:
        return
    p = os.path.join(app.config['UPLOAD_FOLDER'], 'receipt', fname)
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _parse_amount(v):
    """'1,200.50' / '₩48,000' / 1200 등 다양한 입력을 float 또는 None으로."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    import re
    m = re.search(r'-?\d[\d,]*(\.\d+)?', str(v))
    if not m:
        return None
    try:
        return float(m.group().replace(',', ''))
    except ValueError:
        return None


# ─── Pages ───────────────────────────────────────────────────
@app.route('/expenses')
@login_required
def expenses_page():
    return render_template('expenses.html')


@app.route('/expenses/<int:tid>')
@login_required
def expense_detail_page(tid):
    t = query('SELECT id FROM biz_trips WHERE id=?', (tid,), one=True)
    if not t:
        abort(404)
    return render_template('expense_detail.html', trip_id=tid)


# ─── API : 출장 카드 ─────────────────────────────────────────
@app.route('/api/biz-trips', methods=['GET'])
@login_required
def api_trips_list():
    conds, params = ['1=1'], []
    if session.get('role') != 'admin':
        conds.append('t.supervisor_id = ?')
        params.append(session.get('supervisor_id'))
    if request.args.get('status'):
        conds.append('t.status = ?')
        params.append(request.args.get('status'))
    if request.args.get('q'):
        conds.append('t.title LIKE ?')
        params.append(f"%{request.args.get('q')}%")
    sql = f'''
        SELECT t.*, s.name AS supervisor_name
          FROM biz_trips t
          LEFT JOIN supervisors s ON s.id = t.supervisor_id
         WHERE {' AND '.join(conds)}
         ORDER BY t.updated_at DESC, t.id DESC
    '''
    rows = query(sql, params)
    out = []
    for r in rows:
        d = _trip_to_dict(r)
        d['can_edit'] = _trip_owned(r)
        cnt = query('SELECT COUNT(*) AS c FROM biz_receipts WHERE trip_id=?', (r['id'],), one=True)['c']
        d['receipt_count'] = cnt
        sums = query('SELECT currency, COALESCE(SUM(amount),0) AS s FROM biz_receipts WHERE trip_id=? GROUP BY currency', (r['id'],))
        d['totals'] = {(row['currency'] or '?'): row['s'] for row in sums}
        out.append(d)
    return jsonify(out)


@app.route('/api/biz-trips', methods=['POST'])
@login_required
def api_trips_create():
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip()
    if not title:
        return jsonify({'error': '출장명을 입력하세요.'}), 400
    sup = session.get('supervisor_id')
    if session.get('role') == 'admin' and d.get('supervisor_id'):
        sup = d.get('supervisor_id')
    cards = d.get('corp_cards') or []
    if isinstance(cards, str):
        cards = [c.strip() for c in cards.split(',') if c.strip()]
    new_id = execute('''
        INSERT INTO biz_trips
            (supervisor_id, title, trip_start, trip_end, corp_cards, status, created_by)
        VALUES (?,?,?,?,?,?,?)
    ''', (
        sup, title, d.get('trip_start') or None, d.get('trip_end') or None,
        json.dumps(cards, ensure_ascii=False), d.get('status') or 'open',
        session.get('display_name') or session.get('username') or '',
    ))
    return jsonify({'id': new_id, 'ok': True}), 201


@app.route('/api/biz-trips/<int:tid>', methods=['GET'])
@login_required
def api_trip_get(tid):
    t = query('''SELECT t.*, s.name AS supervisor_name
                   FROM biz_trips t LEFT JOIN supervisors s ON s.id=t.supervisor_id
                  WHERE t.id=?''', (tid,), one=True)
    if not t:
        abort(404)
    if not _trip_owned(t):
        return jsonify({'error': '권한이 없습니다.'}), 403
    d = _trip_to_dict(t)
    d['can_edit'] = _trip_owned(t)
    recs = query('SELECT * FROM biz_receipts WHERE trip_id=? ORDER BY display_order, id', (tid,))
    d['receipts'] = [dict(r) for r in recs]
    sums = query('SELECT currency, COALESCE(SUM(amount),0) AS s FROM biz_receipts WHERE trip_id=? GROUP BY currency', (tid,))
    d['totals'] = {(row['currency'] or '?'): row['s'] for row in sums}
    return jsonify(d)


@app.route('/api/biz-trips/<int:tid>', methods=['PUT'])
@login_required
def api_trip_update(tid):
    t, err = _get_trip_for_edit(tid)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    sets, params = [], []
    if 'title' in d:
        sets.append('title=?'); params.append((d.get('title') or '').strip())
    for k in ('trip_start', 'trip_end', 'status'):
        if k in d:
            sets.append(f'{k}=?'); params.append(d.get(k) or None)
    if 'corp_cards' in d:
        cards = d.get('corp_cards') or []
        if isinstance(cards, str):
            cards = [c.strip() for c in cards.split(',') if c.strip()]
        sets.append('corp_cards=?'); params.append(json.dumps(cards, ensure_ascii=False))
    if not sets:
        return jsonify({'ok': True, 'updated': 0})
    sets.append("updated_at=datetime('now','localtime')")
    params.append(tid)
    execute(f'UPDATE biz_trips SET {", ".join(sets)} WHERE id=?', params)
    return jsonify({'ok': True})


@app.route('/api/biz-trips/<int:tid>', methods=['DELETE'])
@login_required
def api_trip_delete(tid):
    t, err = _get_trip_for_edit(tid)
    if err:
        return err
    for r in query('SELECT image_filename FROM biz_receipts WHERE trip_id=?', (tid,)):
        _delete_receipt_image(r['image_filename'])
    execute('DELETE FROM biz_receipts WHERE trip_id=?', (tid,))
    execute('DELETE FROM biz_trips WHERE id=?', (tid,))
    return jsonify({'ok': True})


# ─── API : 영수증 이미지 업로드 ──────────────────────────────
@app.route('/api/biz-trips/<int:tid>/upload-receipt', methods=['POST'])
@login_required
def api_receipt_upload(tid):
    t, err = _get_trip_for_edit(tid)
    if err:
        return err
    if 'file' not in request.files:
        return jsonify({'error': '파일이 없습니다.'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '파일명이 비어있습니다.'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif', 'bmp'}:
        return jsonify({'error': '이미지 파일만 업로드 가능합니다.'}), 400
    rdir = os.path.join(app.config['UPLOAD_FOLDER'], 'receipt')
    os.makedirs(rdir, exist_ok=True)
    import time
    base = f'rcpt-{tid}-{int(time.time()*1000)}-{secrets.token_hex(4)}'
    initial = os.path.join(rdir, f'{base}.{ext}')
    final_path, orig, final = _process_uploaded_image(
        f, initial, RECEIPT_IMAGE_MAX_LONG_SIDE, RECEIPT_IMAGE_JPEG_QUALITY)
    fname = os.path.basename(final_path)
    url = url_for('static', filename=f'uploads/receipt/{fname}')
    return jsonify({'ok': True, 'filename': fname, 'url': url,
                    'original_kb': round(orig / 1024, 1),
                    'final_kb': round(final / 1024, 1)}), 201


# ─── Gemini 비전 추출 (Gemini 3.1 Flash Lite) ────────────────
def _gemini_vision_extract(image_path):
    """저장된 영수증 이미지를 Gemini 3.1 Flash Lite로 추출 (vendor/date/currency/amount + 품질 판정)."""
    if not GEMINI_API_KEY:
        return {'error': 'NO_API_KEY'}
    import base64, mimetypes, urllib.request, urllib.error
    with open(image_path, 'rb') as fp:
        raw = fp.read()
    media = mimetypes.guess_type(image_path)[0] or 'image/jpeg'
    b64 = base64.standard_b64encode(raw).decode()
    prompt = (
        "이 이미지는 출장 경비 영수증/인보이스다. 아래 항목만 추출해 지정한 JSON 형식으로만 답하라.\n"
        "- vendor: 상호/가맹점명 (없으면 null)\n"
        "- date: 거래 일자 YYYY-MM-DD (확실치 않으면 null)\n"
        "- currency: 통화 ISO 코드 (KRW/CNY/USD/JPY/EUR 등, 기호는 코드로 변환, 불명확하면 null)\n"
        "- amount: 총 결제 금액 숫자만 (콤마/통화기호 제거, 소수 허용, 불명확하면 null)\n"
        "글자가 흐리거나 잘려 확신할 수 없으면 해당 필드는 null로 두고, "
        "readable(true/false), confidence(high/medium/low), "
        "issues(배열: blurry/glare/cropped/dark/unclear_amount 등)를 채워라.\n"
        '형식: {"readable":true,"confidence":"high","issues":[],'
        '"vendor":null,"date":null,"currency":null,"amount":null}'
    )
    body = {
        'contents': [{
            'parts': [
                {'inline_data': {'mime_type': media, 'data': b64}},
                {'text': prompt},
            ],
        }],
        'generationConfig': {'response_mime_type': 'application/json'},
    }
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{_model_for("receipt")}:generateContent')
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            'content-type': 'application/json',
            'x-goog-api-key': GEMINI_API_KEY,
        }, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as he:
        try:
            detail = he.read().decode('utf-8')[:300]
        except Exception:
            detail = str(he)
        return {'error': 'API_CALL_FAILED', 'detail': detail}
    except Exception as e:
        return {'error': 'API_CALL_FAILED', 'detail': str(e)}

    # candidates[0].content.parts[*].text 취합
    text = ''
    try:
        cands = data.get('candidates') or []
        if not cands:
            return {'error': 'API_CALL_FAILED', 'detail': json.dumps(data)[:300]}
        for part in (cands[0].get('content', {}).get('parts') or []):
            if isinstance(part.get('text'), str):
                text += part['text']
    except Exception as e:
        return {'error': 'PARSE_FAILED', 'raw': str(e)}

    text = text.strip()
    if text.startswith('```'):
        text = text.strip('`')
        if text[:4].lower() == 'json':
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        return {'error': 'PARSE_FAILED', 'raw': text}


@app.route('/api/biz-trips/<int:tid>/extract', methods=['POST'])
@login_required
def api_receipt_extract(tid):
    t, err = _get_trip_for_edit(tid)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    fname = d.get('filename') or ''
    if not fname or '/' in fname or '\\' in fname or '..' in fname:
        return jsonify({'error': '잘못된 파일명'}), 400
    path = os.path.join(app.config['UPLOAD_FOLDER'], 'receipt', fname)
    if not os.path.exists(path):
        return jsonify({'error': '파일을 찾을 수 없습니다.'}), 404
    result = _gemini_vision_extract(path)
    if result.get('error') == 'NO_API_KEY':
        return jsonify({'ok': False, 'reason': 'no_api_key',
                        'message': 'AI 자동추출이 설정되지 않았습니다. 직접 입력해 주세요.'}), 200
    if result.get('error'):
        return jsonify({'ok': False, 'reason': result['error'],
                        'message': '자동 추출에 실패했습니다. 다시 시도하거나 직접 입력해 주세요.',
                        'detail': result.get('detail') or result.get('raw')}), 200
    fields = {
        'vendor':     result.get('vendor'),
        'occur_date': result.get('date'),
        'currency':   result.get('currency'),
        'amount':     result.get('amount'),
    }
    missing = [k for k in ('occur_date', 'currency', 'amount') if not fields.get(k)]
    need_retake = (result.get('readable') is False) or bool(missing) or (result.get('confidence') == 'low')
    return jsonify({
        'ok': True,
        'fields': fields,
        'readable': result.get('readable', True),
        'confidence': result.get('confidence'),
        'issues': result.get('issues') or [],
        'missing': missing,
        'need_retake': need_retake,
        'raw': json.dumps(result, ensure_ascii=False),
    })


# ─── API : 영수증 (표의 한 줄) ───────────────────────────────
@app.route('/api/biz-trips/<int:tid>/receipts', methods=['POST'])
@login_required
def api_receipt_create(tid):
    t, err = _get_trip_for_edit(tid)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    mx = query('SELECT COALESCE(MAX(display_order),-1) AS m FROM biz_receipts WHERE trip_id=?', (tid,), one=True)['m']
    amount = _parse_amount(d.get('amount'))
    new_id = execute('''
        INSERT INTO biz_receipts
            (trip_id, image_filename, image_url, vendor, cost_type, use_type,
             occur_date, card_no, remark, currency, amount, extracted_raw, display_order)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        tid, d.get('image_filename') or None, d.get('image_url') or None,
        d.get('vendor') or None, d.get('cost_type') or None, d.get('use_type') or None,
        d.get('occur_date') or None, d.get('card_no') or None, d.get('remark') or None,
        d.get('currency') or None, amount, d.get('extracted_raw') or None, mx + 1,
    ))
    execute("UPDATE biz_trips SET updated_at=datetime('now','localtime') WHERE id=?", (tid,))
    r = query('SELECT * FROM biz_receipts WHERE id=?', (new_id,), one=True)
    return jsonify({'ok': True, 'receipt': dict(r)}), 201


@app.route('/api/biz-receipts/<int:rid>', methods=['PUT'])
@login_required
def api_receipt_update(rid):
    r = query('SELECT * FROM biz_receipts WHERE id=?', (rid,), one=True)
    if not r:
        abort(404)
    t, err = _get_trip_for_edit(r['trip_id'])
    if err:
        return err
    d = request.get_json(silent=True) or {}
    sets, params = [], []
    for k in ('vendor', 'cost_type', 'use_type', 'occur_date', 'card_no', 'remark', 'currency'):
        if k in d:
            sets.append(f'{k}=?'); params.append(d.get(k) or None)
    if 'amount' in d:
        sets.append('amount=?'); params.append(_parse_amount(d.get('amount')))
    if 'display_order' in d:
        sets.append('display_order=?'); params.append(int(d.get('display_order') or 0))
    if not sets:
        return jsonify({'ok': True, 'updated': 0})
    params.append(rid)
    execute(f'UPDATE biz_receipts SET {", ".join(sets)} WHERE id=?', params)
    execute("UPDATE biz_trips SET updated_at=datetime('now','localtime') WHERE id=?", (r['trip_id'],))
    return jsonify({'ok': True})


@app.route('/api/biz-receipts/<int:rid>', methods=['DELETE'])
@login_required
def api_receipt_delete(rid):
    r = query('SELECT * FROM biz_receipts WHERE id=?', (rid,), one=True)
    if not r:
        abort(404)
    t, err = _get_trip_for_edit(r['trip_id'])
    if err:
        return err
    _delete_receipt_image(r['image_filename'])
    execute('DELETE FROM biz_receipts WHERE id=?', (rid,))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  Error handlers
# ═════════════════════════════════════════════════════════════════
@app.errorhandler(413)
def _too_large(e):
    return jsonify({'error': '파일 크기는 20MB 이하여야 합니다.'}), 413

@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'not found'}), 404
    return render_template('404.html'), 404


# ═════════════════════════════════════════════════════════════════
#  외부 연동용 데이터 API (읽기 전용, API 키 보호)
#  · 출장 경비(biz_*) 제외 — 그 외 전체 탭 공개
# ═════════════════════════════════════════════════════════════════
def _ensure_api_table():
    execute("""CREATE TABLE IF NOT EXISTS api_settings (
                 k TEXT PRIMARY KEY, v TEXT )""")


def _get_api_key(create=True):
    _ensure_api_table()
    row = query("SELECT v FROM api_settings WHERE k='api_key'", one=True)
    if row and row['v']:
        return row['v']
    if not create:
        return None
    key = secrets.token_hex(24)
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", (key,))
    return key


def _check_api_key():
    provided = (request.headers.get('X-API-Key')
                or request.args.get('key') or '').strip()
    if not provided:
        return False
    real = _get_api_key(create=False)
    if not real:
        return False
    return secrets.compare_digest(provided, real)


def _vkey(name):
    return (name or '').strip().lower()


def _ref(kind, ident):
    """외부 API용 안정 고유키(주소). DB id 기반이라 변하지 않음. 사이트 UI에는 노출 안 됨."""
    return f'{kind}:{ident}' if ident is not None else None


def api_key_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not _check_api_key():
            return jsonify({'error': 'unauthorized',
                            'message': 'valid API key required (X-API-Key header or ?key=)'}), 401
        return fn(*a, **k)
    return wrapper


# ---- 내부(로그인) : 키 조회/재발급 ----
@app.route('/api/ext/key', methods=['GET'])
@login_required
def api_ext_key_get():
    return jsonify({'api_key': _get_api_key(),
                    'base_url': request.host_url.rstrip('/')})


@app.route('/api/ext/key/regenerate', methods=['POST'])
@login_required
def api_ext_key_regen():
    _ensure_api_table()
    key = secrets.token_hex(24)
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('api_key', ?)", (key,))
    return jsonify({'api_key': key})


# ---- 데이터 빌더 ----
def _ext_issues():
    rows = query("""SELECT i.*, v.name AS vessel_name, v.imo AS imo,
                           s.name AS supervisor_name
                      FROM issues i
                      LEFT JOIN vessels v ON v.id = i.vessel_id
                      LEFT JOIN supervisors s ON s.id = i.supervisor_id
                     ORDER BY i.issue_date, i.id""")
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['actions'] = json.loads(d['actions']) if d.get('actions') else []
        except Exception:
            d['actions'] = []
        d['vessel_key'] = _vkey(d.get('vessel_name'))
        d['ref'] = _ref('issue', d.get('id'))
        for ai, a in enumerate(d['actions']):
            if isinstance(a, dict):
                a['ref'] = f"{d['ref']}#action:{ai}"
        out.append(d)
    return out


def _ext_surveys():
    surveys = query("""SELECT cs.*, v.name AS vessel_name, v.imo AS imo
                         FROM cs_surveys cs LEFT JOIN vessels v ON v.id = cs.vessel_id
                        ORDER BY cs.year DESC, cs.quarter DESC, cs.id""")
    out = []
    for s in surveys:
        d = dict(s)
        d['vessel_key'] = _vkey(d.get('vessel_name'))
        d['ref'] = _ref('survey', d.get('id'))
        d['findings'] = [dict(f) | {'ref': _ref('cs_finding', f['id'])} for f in query(
            """SELECT id, category, no, item, description, remark, status
                 FROM cs_findings WHERE survey_id=?
                ORDER BY CASE category WHEN 'Defect' THEN 0 ELSE 1 END, no, id""",
            (s['id'],))]
        out.append(d)
    return out


def _ext_vettings():
    vts = query("""SELECT vt.*, v.name AS vessel_name, v.imo AS imo
                     FROM vettings vt LEFT JOIN vessels v ON v.id = vt.vessel_id
                    ORDER BY vt.inspection_date DESC, vt.id""")
    out = []
    for v in vts:
        d = dict(v)
        d['vessel_key'] = _vkey(d.get('vessel_name'))
        d['ref'] = _ref('vetting', d.get('id'))
        d['findings'] = [dict(f) | {'ref': _ref('vt_finding', f['id'])} for f in query(
            """SELECT id, no, item, description, remark, user_remark, priority, status
                 FROM vt_findings WHERE vetting_id=? ORDER BY no, id""", (v['id'],))]
        out.append(d)
    return out


def _report_tree(report_id, sec_table, blk_table):
    sec_kind = sec_table[:-1]   # dock_report_sections → dock_report_section
    blk_kind = blk_table[:-1]   # dock_report_blocks   → dock_report_block
    secs = query(f"SELECT * FROM {sec_table} WHERE report_id=? ORDER BY display_order, id",
                 (report_id,))
    out = []
    for s in secs:
        sd = dict(s)
        sd['ref'] = _ref(sec_kind, s['id'])
        blocks = []
        for b in query(f"SELECT * FROM {blk_table} WHERE section_id=? ORDER BY display_order, id",
                       (s['id'],)):
            bd = dict(b)
            bd['ref'] = _ref(blk_kind, b['id'])
            try:
                bd['content'] = json.loads(bd['content_json']) if bd.get('content_json') else None
            except Exception:
                bd['content'] = None
            bd.pop('content_json', None)
            blocks.append(bd)
        sd['blocks'] = blocks
        out.append(sd)
    return out


def _ext_dock_reports():
    reps = query("""SELECT d.*, v.name AS vessel_name, v.imo AS imo
                      FROM dock_reports d LEFT JOIN vessels v ON v.id = d.vessel_id
                     WHERE COALESCE(d.is_template,0)=0
                     ORDER BY d.id DESC""")
    out = []
    for r in reps:
        d = dict(r)
        d['vessel_key'] = _vkey(d.get('vessel_name'))
        d['ref'] = _ref('dock_report', d.get('id'))
        d['sections'] = _report_tree(r['id'], 'dock_report_sections', 'dock_report_blocks')
        out.append(d)
    return out


def _ext_boarding_reports():
    reps = query("""SELECT b.*, v.name AS vessel_name, v.imo AS imo
                      FROM boarding_reports b LEFT JOIN vessels v ON v.id = b.vessel_id
                     WHERE COALESCE(b.is_template,0)=0
                     ORDER BY b.id DESC""")
    out = []
    for r in reps:
        d = dict(r)
        d['vessel_key'] = _vkey(d.get('vessel_name'))
        d['ref'] = _ref('boarding_report', d.get('id'))
        d['sections'] = _report_tree(r['id'], 'boarding_report_sections', 'boarding_report_blocks')
        out.append(d)
    return out


def _ext_calendar():
    rows = query("""SELECT c.*, v.name AS vessel_name, s.name AS supervisor_name
                      FROM calendar_events c
                      LEFT JOIN vessels v ON v.id = c.vessel_id
                      LEFT JOIN supervisors s ON s.id = c.supervisor_id
                     ORDER BY c.start_date, c.id""")
    out = []
    for r in rows:
        d = dict(r)
        d['vessel_key'] = _vkey(d.get('vessel_name'))
        d['ref'] = _ref('event', d.get('id'))
        out.append(d)
    return out


def _ext_vessels():
    return [dict(r) | {'vessel_key': _vkey(r['name']), 'ref': _ref('vessel', r['id'])}
            for r in query("SELECT * FROM vessels ORDER BY name")]


def _class_digest(coc_list, stat_list, society):
    """CLASS STATUS 요약 — 선급 / COC합 / 중복표기 번호목록 (Class Status 탭 요약 패널과 동일)."""
    norm = lambda s: ' '.join((s or '').strip().lower().split())
    text = lambda it: (it.get('remark') or it.get('description') or '').strip()
    def fmt(it, dup):
        s = text(it)
        if dup:
            s += ' (선급지적 / 기국사항 중복)'
        due = (it.get('due_date') or '').strip()
        if due:
            s += ' // DUE DATE : ' + due
        return s
    stat_matched = set()
    lines = []
    for c in coc_list:
        key = norm(c.get('description'))
        mi = -1
        if key:
            for i, s in enumerate(stat_list):
                if i not in stat_matched and norm(s.get('description')) == key:
                    mi = i
                    break
        if mi >= 0:
            stat_matched.add(mi)
            lines.append(fmt(c, True))
        else:
            lines.append(fmt(c, False))
    for i, s in enumerate(stat_list):
        if i not in stat_matched:
            lines.append(fmt(s, False))
    lines = [l for l in lines if l]
    detail = '\n'.join(f'{i + 1}. {l}' for i, l in enumerate(lines))
    return {'society': society or '-', 'coc_total': len(coc_list) + len(stat_list), 'detail': detail}


def _ext_class_status():
    """선급 Class Status 스냅샷(선박별 + 미매칭)."""
    out = []
    for cs in query('SELECT * FROM class_status ORDER BY updated_at DESC'):
        vname = cs['vessel_name_raw']
        if cs['vessel_id']:
            v = query('SELECT name FROM vessels WHERE id=?', (cs['vessel_id'],), one=True)
            if v:
                vname = v['name']
        items = query('SELECT id, category, no, issued_date, description, due_date, remark, importance '
                      'FROM class_status_items WHERE cs_id=? ORDER BY category, no', (cs['id'],))
        coc_l = [dict(i) | {'ref': _ref('class_item', i['id'])} for i in items if i['category'] == 'COC']
        stat_l = [dict(i) | {'ref': _ref('class_item', i['id'])} for i in items if i['category'] == 'STATUTORY']
        out.append({
            'id': cs['id'],
            'ref': _ref('class_status', cs['id']),
            'vessel_name': vname,
            'vessel_key': _vkey(vname),
            'matched': cs['vessel_id'] is not None,
            'class_society': cs['class_society'],
            'report_date': cs['report_date'],
            'updated_at': cs['updated_at'],
            'coc':       coc_l,
            'statutory': stat_l,
            'digest':    _class_digest(coc_l, stat_l, cs['class_society']),
        })
    return out


def _ext_summaries():
    """저장된 업무 요약(전체 + 감독별)을 scope별로 반환."""
    _ensure_summary_table()
    out = []
    for r in query("SELECT scope, data, generated_at FROM issue_summaries"):
        try:
            rows = json.loads(r['data'])
        except Exception:
            rows = []
        sup = None
        if r['scope'] != 'all':
            sv = query('SELECT name FROM supervisors WHERE id=?', (r['scope'],), one=True)
            sup = sv['name'] if sv else None
        out.append({'scope': r['scope'], 'ref': _ref('summary', r['scope']),
                    'supervisor_name': sup,
                    'generated_at': r['generated_at'], 'rows': rows})
    return out


def _ext_vetting_digests():
    """선박 단위 SIRE 요약(자동 집계) — Vetting 탭 펼침 요약 패널과 동일 내용."""
    out = []
    for ve in query("SELECT id, name, imo FROM vessels ORDER BY name"):
        vts = query("SELECT * FROM vettings WHERE vessel_id=? "
                    "ORDER BY inspection_date DESC, id DESC", (ve['id'],))
        if not vts:
            continue
        enr = [_vetting_with_counts(v) for v in vts]
        latest = enr[0]
        # OBS: 최신이 'Next Plan'이면 그 이전(Next Plan 아닌 최신) Report 수치 사용
        obs_src = latest
        if (latest.get('valid') or '') == 'Next Plan':
            obs_src = next((v for v in enr if (v.get('valid') or '') != 'Next Plan'), latest)
        detail = '\n\n'.join(
            (v.get('overall_remark') or '').strip()
            for v in enr
            if (v.get('open_count') or 0) > 0 and (v.get('overall_remark') or '').strip()
        )
        out.append({
            'ref': _ref('vetting_digest', ve['id']),
            'vessel_name': ve['name'],
            'vessel_key': _vkey(ve['name']),
            'imo': ve['imo'],
            'status': latest.get('valid') or '',
            'port': latest.get('port') or '',
            'inspection_date': latest.get('inspection_date') or '',
            'oil_major': latest.get('inspection_company') or '',
            'obs_total': obs_src.get('observation_count') or 0,
            'obs_open': obs_src.get('open_count') or 0,
            'detail': detail,
            'latest_vetting_ref': _ref('vetting', latest.get('id')),
        })
    return out


# ---- 공개(키 보호) 데이터 엔드포인트 ----
@app.route('/api/ext/issues')
@api_key_required
def api_ext_issues():
    return jsonify(_ext_issues())


@app.route('/api/ext/summary-generate', methods=['POST'])
@api_key_required
def api_ext_summary_generate():
    """스케줄러용(맥 launchd, 매일 18시): 전체 업무요약 생성·갱신. API 키 인증."""
    rows, gen_at, counts = _run_summary_generate(None)
    return jsonify({'ok': True, 'generated_at': gen_at,
                    'total': counts.get('all', len(rows)), 'counts': counts})


@app.route('/api/ext/surveys')
@api_key_required
def api_ext_surveys():
    return jsonify(_ext_surveys())


@app.route('/api/ext/vettings')
@api_key_required
def api_ext_vettings():
    return jsonify(_ext_vettings())


@app.route('/api/ext/vetting-digests')
@api_key_required
def api_ext_vetting_digests():
    return jsonify(_ext_vetting_digests())


@app.route('/api/ext/dock-reports')
@api_key_required
def api_ext_dock():
    return jsonify(_ext_dock_reports())


@app.route('/api/ext/boarding-reports')
@api_key_required
def api_ext_boarding():
    return jsonify(_ext_boarding_reports())


@app.route('/api/ext/calendar')
@api_key_required
def api_ext_calendar():
    return jsonify(_ext_calendar())


@app.route('/api/ext/vessels')
@api_key_required
def api_ext_vessels():
    return jsonify(_ext_vessels())


@app.route('/api/ext/summaries')
@api_key_required
def api_ext_summaries():
    return jsonify(_ext_summaries())


@app.route('/api/ext/class-status')
@api_key_required
def api_ext_class_status():
    return jsonify(_ext_class_status())


@app.route('/api/ext/class-status/push-flag')
@api_key_required
def api_ext_class_status_push_flag():
    """맥 러너 폴링용 — 'BV Pushing' 버튼이 찍은 플래그 시각 반환."""
    r = query("SELECT v FROM api_settings WHERE k='cls_push_flag'", one=True)
    return jsonify({'flag': r['v'] if r else None})


@app.route('/api/ext/class-status/upload', methods=['POST'])
@api_key_required
def api_ext_class_status_upload():
    """맥 러너가 BV에서 받은 Ship Status PDF 업로드 → 기존 AI추출·매칭·저장 파이프라인."""
    files = request.files.getlist('files') or (
        [request.files['file']] if 'file' in request.files else [])
    if not [f for f in files if f and f.filename]:
        return jsonify({'ok': False, 'message': '파일 없음'}), 400
    results = _cls_handle_files(files)
    return jsonify({'ok': any(r.get('ok') for r in results), 'results': results})


@app.route('/api/ext/all')
@api_key_required
def api_ext_all():
    from datetime import datetime as _dt
    return jsonify({
        'generated_at': _dt.now().isoformat(timespec='seconds'),
        'source': 'TRMT3',
        'vessels':           _ext_vessels(),
        'issues':            _ext_issues(),
        'condition_surveys': _ext_surveys(),
        'vettings':          _ext_vettings(),
        'vetting_digests':   _ext_vetting_digests(),
        'dock_reports':      _ext_dock_reports(),
        'boarding_reports':  _ext_boarding_reports(),
        'calendar_events':   _ext_calendar(),
        'work_summaries':    _ext_summaries(),
        'class_status':      _ext_class_status(),
    })


# ---- helper: name -> id (MCP automation passes vessel/supervisor by name) ----
def _resolve_vessel_id(d):
    vid = d.get('vessel_id')
    if vid:
        return vid
    nm = d.get('vessel_name') or d.get('vessel')
    if nm:
        v = _match_vessel_by_name(nm)
        if v:
            return v['id']
    return None


def _resolve_supervisor_id(d):
    sid = d.get('supervisor_id')
    if sid:
        return sid
    nm = (d.get('supervisor_name') or d.get('supervisor') or '').strip()
    if nm:
        r = query('SELECT id FROM supervisors WHERE lower(name)=lower(?)', (nm,), one=True)
        if r:
            return r['id']
    return None


@app.route('/api/ext/supervisors')
@api_key_required
def api_ext_supervisors():
    return jsonify([dict(r) for r in
                    query('SELECT id, name, color FROM supervisors ORDER BY name')])


@app.route('/api/ext/issues', methods=['POST'])
@api_key_required
def api_ext_issue_create():
    from datetime import date as _date
    d = request.get_json(silent=True) or {}
    vid = _resolve_vessel_id(d)
    sid = _resolve_supervisor_id(d)
    if not vid:
        return jsonify({'error': 'vessel not found', 'hint': 'need vessel_id or vessel_name'}), 400
    if not sid:
        return jsonify({'error': 'supervisor not found', 'hint': 'need supervisor_id or supervisor_name'}), 400
    item_topic = (d.get('item_topic') or '').strip()
    if not item_topic:
        return jsonify({'error': 'item_topic required'}), 400
    issue_date = (d.get('issue_date') or '').strip() or _date.today().isoformat()
    actions = d.get('actions') or []
    if not isinstance(actions, list):
        actions = []
    priority = d.get('priority') or 'Normal'
    status = d.get('status') or 'Open'
    if priority not in ('Normal', 'Urgent', 'COC & Flag', 'Next DD'):
        priority = 'Normal'
    if status not in ('Open', 'InProgress', 'Closed'):
        status = 'Open'
    iid = execute("""
        INSERT INTO issues
            (supervisor_id, vessel_id, issue_date, due_date, item_topic,
             description, actions, priority, status, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sid, vid, issue_date, d.get('due_date') or None, item_topic,
        d.get('description') or '', json.dumps(actions, ensure_ascii=False),
        priority, status, d.get('created_by') or 'mcp',
    ))
    return jsonify({'id': iid, 'ref': _ref('issue', iid)}), 201


@app.route('/api/ext/issues/<int:iid>', methods=['PUT'])
@api_key_required
def api_ext_issue_update(iid):
    if not query('SELECT id FROM issues WHERE id=?', (iid,), one=True):
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    if ('vessel_name' in d or 'vessel' in d) and not d.get('vessel_id'):
        rv = _resolve_vessel_id(d)
        if rv:
            d['vessel_id'] = rv
    if ('supervisor_name' in d or 'supervisor' in d) and not d.get('supervisor_id'):
        rs = _resolve_supervisor_id(d)
        if rs:
            d['supervisor_id'] = rs
    fields = ['supervisor_id', 'vessel_id', 'issue_date', 'due_date', 'item_topic',
              'description', 'actions', 'priority', 'status']
    sets, params = [], []
    for f in fields:
        if f in d:
            val = d[f]
            if f == 'actions':
                if not isinstance(val, list):
                    val = []
                val = json.dumps(val, ensure_ascii=False)
            elif val == '':
                val = None
            sets.append(f + ' = ?')
            params.append(val)
    if not sets:
        return jsonify({'error': 'no fields'}), 400
    sets.append('updated_at = datetime("now","localtime")')
    params.append(iid)
    execute('UPDATE issues SET ' + ', '.join(sets) + ' WHERE id = ?', params)
    return jsonify({'id': iid, 'ref': _ref('issue', iid)})

# ---- Phase 2: 메일 제목 정규화 + 매칭/액션/메일키 (additive) ----
def _norm_subject(s):
    """메일 제목 정규화: 앞쪽 RE/FW/회신/전달/[EXTERNAL] 등 반복 제거 + 공백/소문자."""
    import re as _re_s
    if not s:
        return ''
    t = str(s).strip()
    pat = _re_s.compile(
        r'^\s*(\[[^\]]*\]\s*|re\s*:|fw\s*:|fwd\s*:|회신\s*:|전달\s*:|답장\s*:)\s*',
        _re_s.IGNORECASE)
    prev = None
    while prev != t:
        prev = t
        t = pat.sub('', t)
    return _re_s.sub(r'\s+', ' ', t).strip().lower()
 
 
@app.route('/api/ext/issues/match')
@api_key_required
def api_ext_issue_match():
    subject = request.args.get('subject', '')
    conv_id = request.args.get('conv_id', '')
    norm = _norm_subject(subject)
 
    def _flat(t):
        return ' '.join((t or '').lower().split())
 
    rows = query(
        'SELECT i.*, v.name AS vessel_name, s.name AS supervisor_name '
        'FROM issues i '
        'LEFT JOIN vessels v ON v.id=i.vessel_id '
        'LEFT JOIN supervisors s ON s.id=i.supervisor_id '
        'ORDER BY i.id DESC')
    matches = []
    for r in rows:
        d = dict(r)
        why = None
        if conv_id and d.get('email_conv_id') and d['email_conv_id'] == conv_id:
            why = 'conv_id'
        elif norm and d.get('email_subject_norm') and d['email_subject_norm'] == norm:
            why = 'subject_key'
        elif norm and len(norm) >= 12 and norm in _flat(d.get('description')):
            why = 'description'
        elif norm and len(norm) >= 12 and norm in _flat(d.get('item_topic')):
            why = 'item_topic'
        if not why:
            continue
        try:
            acts = json.loads(d['actions']) if d.get('actions') else []
        except Exception:
            acts = []
        matches.append({
            'id': d.get('id'), 'ref': _ref('issue', d.get('id')),
            'item_topic': d.get('item_topic'), 'status': d.get('status'),
            'priority': d.get('priority'), 'vessel_name': d.get('vessel_name'),
            'supervisor_name': d.get('supervisor_name'),
            'actions': acts, 'match_by': why,
        })
    return jsonify({'query_subject_norm': norm, 'count': len(matches),
                    'matches': matches})
 
 
@app.route('/api/ext/issues/<int:iid>/actions', methods=['POST'])
@api_key_required
def api_ext_issue_add_action(iid):
    from datetime import date as _date
    row = query('SELECT actions FROM issues WHERE id=?', (iid,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    progress = (d.get('progress') or '').strip()
    if not progress:
        return jsonify({'error': 'progress required'}), 400
    try:
        actions = json.loads(row['actions']) if row['actions'] else []
        if not isinstance(actions, list):
            actions = []
    except Exception:
        actions = []
    actions.append({
        'date': (d.get('date') or '').strip() or _date.today().isoformat(),
        'progress': progress,
        'important': bool(d.get('important')),
    })
    execute('UPDATE issues SET actions=?, updated_at=datetime("now","localtime") '
            'WHERE id=?', (json.dumps(actions, ensure_ascii=False), iid))
    return jsonify({'id': iid, 'ref': _ref('issue', iid),
                    'actions_count': len(actions)})
 
 
@app.route('/api/ext/issues/<int:iid>/email-key', methods=['POST'])
@api_key_required
def api_ext_issue_set_email_key(iid):
    if not query('SELECT id FROM issues WHERE id=?', (iid,), one=True):
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    norm = _norm_subject(d.get('email_subject') or '')
    conv = d.get('email_conv_id') or None
    execute('UPDATE issues SET email_subject_norm=?, email_conv_id=? WHERE id=?',
            (norm or None, conv, iid))
    return jsonify({'id': iid, 'ref': _ref('issue', iid)})


# ═════════════════════════════════════════════════════════════════
#  AOR(Technical) — 검토→상신 draft 승인 큐
#   · prep 엔진(맥)이 Submitted Tech AOR + 이메일매칭 카드를 POST /api/ext/aor/drafts
#   · 사람이 /aor 탭서 cost·comment·결재라인 확인/수정 → 승인 → status='approved'
#   · approve 가 automation_run(aor_submit) 큐 적재 → 맥이 claim → SP_SET_AOR 상신
#   · 완전자동 상신 금지 — 사람 승인 게이트 필수
# ═════════════════════════════════════════════════════════════════
@app.route('/aor')
@admin_required
def aor_page():
    return render_template('aor.html')


@app.route('/api/aor/drafts')
@admin_required
def api_aor_list():
    status = (request.args.get('status') or 'pending').strip()
    if status == 'all':
        rows = query("SELECT * FROM aor_draft ORDER BY CASE status "
                     "WHEN 'pending' THEN 0 WHEN 'hold' THEN 1 WHEN 'approved' THEN 2 "
                     "WHEN 'submitting' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END, id DESC")
    else:
        rows = query('SELECT * FROM aor_draft WHERE status=? ORDER BY id DESC', (status,))
    pending = query("SELECT COUNT(*) c FROM aor_draft WHERE status='pending'", one=True)
    _ensure_api_table()
    crew = query("SELECT v FROM api_settings WHERE k='aor_crew_submitted'", one=True)
    at = query("SELECT v FROM api_settings WHERE k='aor_stats_at'", one=True)
    return jsonify({'count': len(rows), 'pending': pending['c'],
                    'crew_submitted': (int(crew['v']) if crew and str(crew['v']).isdigit() else None),
                    'crew_at': (at['v'] if at else None),
                    'drafts': [dict(r) for r in rows]})


@app.route('/api/ext/aor/drafts', methods=['POST'])
@api_key_required
def api_ext_aor_create():
    """prep 엔진 ingest: Submitted AOR 카드 적재. 같은 aor_cd 가 pending이면 갱신(중복 방지)."""
    d = request.get_json(silent=True) or {}
    aor_cd = (d.get('aor_cd') or '').strip()
    if not aor_cd:
        return jsonify({'error': 'aor_cd required'}), 400
    ex = query("SELECT id, status FROM aor_draft WHERE aor_cd=? "
               "AND status IN ('pending','approved','submitting','submitted') "
               "ORDER BY id DESC LIMIT 1", (aor_cd,), one=True)
    cm = d.get('cost_match')
    cols = dict(
        vsl_cd=d.get('vsl_cd'), vsl_nm=d.get('vsl_nm'), subj=d.get('subj'),
        amt=d.get('amt'), cur_cd=d.get('cur_cd'), req_user_nm=d.get('req_user_nm'),
        cost_proposed=d.get('cost_proposed'),
        cost_match=(1 if cm is True else 0 if cm is False else None),
        match_conf=d.get('match_conf'), email_subj=d.get('email_subj'),
        proposed_comment=d.get('proposed_comment'), approval_app_no=d.get('approval_app_no'),
        approval_line=(json.dumps(d.get('approval_line'), ensure_ascii=False)
                       if d.get('approval_line') is not None else None),
        attach_files=(json.dumps(d.get('attach_files'), ensure_ascii=False)
                      if d.get('attach_files') is not None else None),
        raw_row=(json.dumps(d.get('raw_row'), ensure_ascii=False)
                 if d.get('raw_row') is not None else None),
    )
    if ex and ex['status'] == 'pending':
        sets = ', '.join(f"{k}=?" for k in cols)
        execute(f"UPDATE aor_draft SET {sets} WHERE id=?", (*cols.values(), ex['id']))
        return jsonify({'id': ex['id'], 'status': 'pending', 'updated': True}), 200
    if ex:   # approved/submitting/submitted — 진행중이므로 손대지 않음
        return jsonify({'id': ex['id'], 'status': ex['status'], 'dedup': True}), 200
    did = execute(
        "INSERT INTO aor_draft (aor_cd, vsl_cd, vsl_nm, subj, amt, cur_cd, req_user_nm, "
        "cost_proposed, cost_match, match_conf, email_subj, proposed_comment, "
        "approval_app_no, approval_line, attach_files, raw_row) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aor_cd, *cols.values()))
    return jsonify({'id': did, 'status': 'pending'}), 201


def _queue_aor(task, user):
    """approve/reject 시 aor_submit·aor_reject run 큐 적재(대기/진행중이면 재사용 — claim이 해당 상태 전부 처리)."""
    if not _automation_enabled():
        return None
    busy = query("SELECT run_id FROM automation_run WHERE task=? "
                 "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1", (task,), one=True)
    if busy:
        return busy['run_id']
    rid = uuid.uuid4().hex[:12]
    execute("INSERT INTO automation_run (run_id, task, mode, status, requested_by) "
            "VALUES (?, ?, 'live', 'queued', ?)", (rid, task, user))
    return rid


@app.route('/api/aor/drafts/<int:did>/approve', methods=['POST'])
@admin_required
def api_aor_approve(did):
    """승인 = 상신 지시. 본문 수정값(comment·app_no) 반영 후 status='approved' + 상신큐 적재."""
    row = query('SELECT * FROM aor_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['status'] != 'pending':
        return jsonify({'error': 'already decided', 'status': row['status']}), 409
    d = request.get_json(silent=True) or {}
    comment = d['proposed_comment'] if 'proposed_comment' in d else row['proposed_comment']
    app_no = (d.get('approval_app_no') or row['approval_app_no'] or '').strip()
    if not app_no:
        return jsonify({'error': '결재라인(approval_app_no) 미지정 — 카드에서 결재라인 선택 후 승인',
                        'field': 'approval_app_no'}), 400
    if not row['raw_row']:
        return jsonify({'error': 'raw_row 없음 — prep 데이터 손상, 리젝 후 재적재 필요'}), 400
    if not _automation_enabled():
        return jsonify({'error': 'killswitch ON — 자동화 정지중. 마스터 스위치 먼저 켜세요.'}), 409
    user = session.get('username') or 'web'
    rc = execute_rc("UPDATE aor_draft SET status='approved', proposed_comment=?, approval_app_no=?, "
                    "decided_at=datetime('now','localtime'), decided_by=? WHERE id=? AND status='pending'",
                    (comment, app_no, user, did))
    if not rc:   # race — 그 사이 다른 처리(리젝/중복승인)로 pending 아님
        cur = query('SELECT status FROM aor_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': 'already decided', 'status': cur['status'] if cur else '?'}), 409
    rid = _queue_aor('aor_submit', user)
    return jsonify({'id': did, 'status': 'approved', 'submit_run': rid,
                    'message': '승인됨 — 맥 러너가 곧 SVMS 상신(최대 1~2분)'})


@app.route('/api/aor/drafts/<int:did>/reject', methods=['POST'])
@admin_required
def api_aor_reject(did):
    """리젝 = SVMS STATUS=R + 관리사 통보메일. 맥 러너가 처리(automation_run aor_reject 큐)."""
    row = query('SELECT * FROM aor_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['status'] not in ('pending', 'failed'):
        return jsonify({'error': 'already decided', 'status': row['status']}), 409
    if not row['raw_row']:
        return jsonify({'error': 'raw_row 없음 — 리젝 불가, 카드 삭제 후 재적재'}), 400
    if not _automation_enabled():
        return jsonify({'error': 'killswitch ON — 자동화 정지중. 마스터 스위치 먼저 켜세요.'}), 409
    d = request.get_json(silent=True) or {}
    user = session.get('username') or 'web'
    rc = execute_rc("UPDATE aor_draft SET status='rejecting', reject_reason=?, "
                    "decided_at=datetime('now','localtime'), decided_by=? "
                    "WHERE id=? AND status IN ('pending','failed')",
                    ((d.get('reason') or '').strip() or None, user, did))
    if not rc:   # race — 이미 처리됨
        cur = query('SELECT status FROM aor_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': 'already decided', 'status': cur['status'] if cur else '?'}), 409
    rid = _queue_aor('aor_reject', user)
    return jsonify({'id': did, 'status': 'rejecting', 'reject_run': rid,
                    'message': '리젝 접수 — 맥 러너가 곧 SVMS 리젝+통보메일(최대 1~2분)'})


@app.route('/api/aor/drafts/<int:did>/hold', methods=['POST'])
@admin_required
def api_aor_hold(did):
    """보류 — TRMT 카드만 hold 로 이동(SVMS 무영향). 나중에 unhold 로 검토 복귀."""
    rc = execute_rc("UPDATE aor_draft SET status='hold', "
                    "decided_at=datetime('now','localtime'), decided_by=? "
                    "WHERE id=? AND status='pending'", (session.get('username') or 'web', did))
    if not rc:
        cur = query('SELECT status FROM aor_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': 'pending 상태만 보류 가능', 'status': cur['status'] if cur else '?'}), 409
    return jsonify({'id': did, 'status': 'hold'})


@app.route('/api/aor/drafts/<int:did>/unhold', methods=['POST'])
@admin_required
def api_aor_unhold(did):
    """보류 해제 — 다시 검토 대기(pending)로. SVMS 무영향."""
    rc = execute_rc("UPDATE aor_draft SET status='pending', decided_at=NULL, decided_by=NULL "
                    "WHERE id=? AND status='hold'", (did,))
    if not rc:
        return jsonify({'error': 'hold 상태만 복귀 가능'}), 409
    return jsonify({'id': did, 'status': 'pending'})


@app.route('/api/aor/drafts/<int:did>', methods=['DELETE'])
@admin_required
def api_aor_delete(did):
    if not query('SELECT id FROM aor_draft WHERE id=?', (did,), one=True):
        return jsonify({'error': 'not found'}), 404
    execute('DELETE FROM aor_draft WHERE id=?', (did,))
    return jsonify({'id': did, 'deleted': True})


@app.route('/api/aor/drafts/decided', methods=['DELETE'])
@admin_required
def api_aor_clear_decided():
    """처리완료(승인·리젝 등) 일괄 삭제 — 대기(pending)·보류(hold)·진행중(submitting)은 보존."""
    n = execute_rc("DELETE FROM aor_draft WHERE status NOT IN ('pending','hold','submitting')")
    return jsonify({'ok': True, 'deleted': n})


# ---- ext (맥 러너: 상신 실행) ----
@app.route('/api/ext/aor/approved')
@api_key_required
def api_ext_aor_approved():
    """맥 러너가 상신할 approved 건 목록을 가져가며 status='submitting'으로 락."""
    cols = "id, aor_cd, proposed_comment, approval_app_no, raw_row"
    if request.args.get('peek'):   # dry 검증 — 락 안 하고 조회만
        rows = query(f"SELECT {cols} FROM aor_draft WHERE status='approved' ORDER BY id ASC")
        return jsonify({'count': len(rows), 'drafts': [dict(r) for r in rows], 'peek': True})
    # claim 전 기존 submitting = 이전 run 중단 잔류(stuck). 단일 러너라 정상 진행분과 안 겹침 → 멱등 재처리.
    out = [dict(r) for r in
           query(f"SELECT {cols} FROM aor_draft WHERE status='submitting' ORDER BY id ASC")]
    for r in query(f"SELECT {cols} FROM aor_draft WHERE status='approved' ORDER BY id ASC"):
        # 조건부 claim — 'approved'→'submitting' 락 성공분만 추가(동시 호출 중복 방지)
        if execute_rc("UPDATE aor_draft SET status='submitting' WHERE id=? AND status='approved'",
                      (r['id'],)):
            out.append(dict(r))
    return jsonify({'count': len(out), 'drafts': out})


@app.route('/api/ext/aor/drafts/<int:did>/result', methods=['POST'])
@api_key_required
def api_ext_aor_result(did):
    """맥 러너의 상신 결과 보고: ok=True → submitted, else failed(사람 재검토)."""
    d = request.get_json(silent=True) or {}
    ok = bool(d.get('ok'))
    result = (d.get('result') or '')[:2000]
    new = 'submitted' if ok else 'failed'
    rc = execute_rc("UPDATE aor_draft SET status=?, submitted_at=datetime('now','localtime'), "
                    "submit_result=? WHERE id=? AND status='submitting'", (new, result, did))
    return jsonify({'id': did, 'ok': ok, 'applied': bool(rc)})


@app.route('/api/ext/aor/rejecting')
@api_key_required
def api_ext_aor_rejecting():
    """맥 러너가 리젝할 rejecting 건 목록(STATUS=R 처리 + 통보메일 대상)."""
    rows = query("SELECT id, aor_cd, reject_reason, raw_row FROM aor_draft "
                 "WHERE status='rejecting' ORDER BY id ASC")
    return jsonify({'count': len(rows), 'drafts': [dict(r) for r in rows]})


@app.route('/api/ext/aor/drafts/<int:did>/reject-result', methods=['POST'])
@api_key_required
def api_ext_aor_reject_result(did):
    """맥 러너의 리젝 결과: ok=True → rejected(완료), else reject_failed(사람 재검토)."""
    d = request.get_json(silent=True) or {}
    ok = bool(d.get('ok'))
    result = (d.get('result') or '')[:2000]
    new = 'rejected' if ok else 'reject_failed'
    rc = execute_rc("UPDATE aor_draft SET status=?, submitted_at=datetime('now','localtime'), "
                    "submit_result=? WHERE id=? AND status='rejecting'", (new, result, did))
    return jsonify({'id': did, 'ok': ok, 'applied': bool(rc)})


@app.route('/api/ext/aor/stats', methods=['POST'])
@api_key_required
def api_ext_aor_stats():
    """prep 실행 시 부가 통계(예: Crew dept submitted 건수) 갱신 — 참고 표시용."""
    d = request.get_json(silent=True) or {}
    try:
        n = int(d.get('crew_submitted') or 0)
    except (TypeError, ValueError):
        n = 0
    _ensure_api_table()
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('aor_crew_submitted', ?)", (str(n),))
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES "
            "('aor_stats_at', datetime('now','localtime'))")
    return jsonify({'ok': True, 'crew_submitted': n})


# ---- 온디맨드 '메일 풀링하기' 플래그 (사이트 버튼 → 맥미니가 저빈도 폴링) ----
@app.route('/api/wf/pull-now', methods=['POST'])
@admin_required
def api_wf_pull_now():
    import time as _t
    _ensure_api_table()
    ts = str(int(_t.time()))
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('wf_pull_request', ?)", (ts,))
    return jsonify({'ok': True, 'ts': int(ts)})


@app.route('/api/wf/pull-flag')
@api_key_required
def api_wf_pull_flag():
    row = query("SELECT v FROM api_settings WHERE k='wf_pull_request'", one=True)
    return jsonify({'ts': int(row['v']) if row and (row['v'] or '').isdigit() else 0})


# ═════════════════════════════════════════════════════════════════
#  자동화 모음 (SOA/전자결재 온디맨드 버튼 → 맥미니 launchd 폴링 실행)
# ═════════════════════════════════════════════════════════════════
# task = 실행단위. mode: 'verify'(읽기전용 DRY) | 'live'(자동 승인/상신).
# 맥미니가 task+mode를 스크립트+env로 매핑(서버는 명령어를 모름 — 안전).
# ===== 비용청구(Fund Request) 2단게이트 =====
#   · review 엔진(맥)이 장금 Technical Submitted 검토결과를 POST /api/ext/fundreq/drafts (카드 적재, [검증] 버튼)
#   · 사람이 /fundreq 탭서 카드마다 승인(approved) / 리젝(rejecting, 사유) 결정
#   · [자동상신] 버튼 → 맥 fundreq_exec 가 approved=SP_SET_OPEX 상신(STATUS=U) / rejecting=STATUS=R+통보메일
@app.route('/fundreq')
@admin_required
def fundreq_page():
    return render_template('fundreq.html')


@app.route('/api/fundreq/drafts')
@admin_required
def api_fundreq_list():
    status = request.args.get('status')
    if status:
        rows = query('SELECT * FROM fundreq_draft WHERE status=? ORDER BY id DESC', (status,))
    else:
        rows = query("SELECT * FROM fundreq_draft ORDER BY CASE status WHEN 'pending' THEN 0 "
                     "WHEN 'approved' THEN 1 WHEN 'rejecting' THEN 2 ELSE 3 END, id DESC")
    pending = query("SELECT COUNT(*) c FROM fundreq_draft WHERE status='pending'", one=True)
    return jsonify({'drafts': [dict(r) for r in rows], 'pending': pending['c'],
                    'enabled': _automation_enabled()})


@app.route('/api/ext/fundreq/drafts', methods=['POST'])
@api_key_required
def api_ext_fundreq_create():
    """review 엔진 ingest: 검토결과 카드 적재. 같은 opex_cd 가 pending이면 갱신(중복 방지)."""
    d = request.get_json(silent=True) or {}
    opex_cd = (d.get('opex_cd') or '').strip()
    if not opex_cd:
        return jsonify({'error': 'opex_cd required'}), 400
    ex = query("SELECT id, status FROM fundreq_draft WHERE opex_cd=? "
               "AND status IN ('pending','approved','submitting','submitted','rejecting','rejected') "
               "ORDER BY id DESC LIMIT 1", (opex_cd,), one=True)
    cols = dict(
        vsl_cd=d.get('vsl_cd'), vsl_nm=d.get('vsl_nm'), subj=d.get('subj'),
        amt=d.get('amt'), cur_cd=d.get('cur_cd'), tp=d.get('tp'),
        ref_no=d.get('ref_no'), ref_amt=d.get('ref_amt'), dn=d.get('dn'),
        diff=d.get('diff'), verdict=d.get('verdict'), why=d.get('why'),
        raw_row=(json.dumps(d.get('raw_row'), ensure_ascii=False) if d.get('raw_row') is not None else None),
    )
    if ex and ex['status'] == 'pending':
        sets = ', '.join(f"{k}=?" for k in cols)
        execute(f"UPDATE fundreq_draft SET {sets} WHERE id=?", (*cols.values(), ex['id']))
        return jsonify({'id': ex['id'], 'status': 'pending', 'updated': True}), 200
    if ex:   # 이미 결정/진행중 — 손대지 않음
        return jsonify({'id': ex['id'], 'status': ex['status'], 'dedup': True}), 200
    did = execute(
        "INSERT INTO fundreq_draft (opex_cd, vsl_cd, vsl_nm, subj, amt, cur_cd, tp, ref_no, "
        "ref_amt, dn, diff, verdict, why, raw_row) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (opex_cd, *cols.values()))
    return jsonify({'id': did, 'status': 'pending'}), 201


@app.route('/api/fundreq/drafts/<int:did>/approve', methods=['POST'])
@admin_required
def api_fundreq_approve(did):
    """승인 마킹 — status='approved'. 실제 상신은 [자동상신] 버튼이 맥 러너로 실행."""
    row = query('SELECT * FROM fundreq_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if not row['raw_row']:
        return jsonify({'error': 'raw_row 없음 — 재검토 필요'}), 400
    rc = execute_rc("UPDATE fundreq_draft SET status='approved', "
                    "decided_at=datetime('now','localtime'), decided_by=? "
                    "WHERE id=? AND status IN ('pending','rejecting')",
                    (session.get('username') or 'web', did))
    if not rc:
        cur = query('SELECT status FROM fundreq_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': 'already decided', 'status': cur['status'] if cur else '?'}), 409
    return jsonify({'id': did, 'status': 'approved'})


@app.route('/api/fundreq/drafts/<int:did>/reject', methods=['POST'])
@admin_required
def api_fundreq_reject(did):
    """리젝 마킹(사유 필수) — status='rejecting'. 실제 리젝+통보메일은 [자동상신] 버튼이 맥 러너로 실행."""
    row = query('SELECT * FROM fundreq_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if not row['raw_row']:
        return jsonify({'error': 'raw_row 없음 — 재검토 필요'}), 400
    d = request.get_json(silent=True) or {}
    reason = (d.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': '리젝 사유(reason) 필수', 'field': 'reason'}), 400
    rc = execute_rc("UPDATE fundreq_draft SET status='rejecting', reject_reason=?, "
                    "decided_at=datetime('now','localtime'), decided_by=? "
                    "WHERE id=? AND status IN ('pending','approved')",
                    (reason, session.get('username') or 'web', did))
    if not rc:
        cur = query('SELECT status FROM fundreq_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': 'already decided', 'status': cur['status'] if cur else '?'}), 409
    return jsonify({'id': did, 'status': 'rejecting'})


@app.route('/api/fundreq/drafts/<int:did>/reset', methods=['POST'])
@admin_required
def api_fundreq_reset(did):
    """결정 취소 — 실행 전(approved/rejecting)만 pending 으로 되돌림."""
    rc = execute_rc("UPDATE fundreq_draft SET status='pending', reject_reason=NULL, "
                    "decided_at=NULL, decided_by=NULL WHERE id=? AND status IN ('approved','rejecting')", (did,))
    if not rc:
        cur = query('SELECT status FROM fundreq_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': '실행 전(approved/rejecting)만 취소 가능', 'status': cur['status'] if cur else '?'}), 409
    return jsonify({'id': did, 'status': 'pending'})


@app.route('/api/fundreq/drafts/<int:did>', methods=['DELETE'])
@admin_required
def api_fundreq_delete(did):
    if not query('SELECT id FROM fundreq_draft WHERE id=?', (did,), one=True):
        return jsonify({'error': 'not found'}), 404
    execute('DELETE FROM fundreq_draft WHERE id=?', (did,))
    return jsonify({'id': did, 'deleted': True})


@app.route('/api/fundreq/drafts/decided', methods=['DELETE'])
@admin_required
def api_fundreq_clear_decided():
    """처리완료 일괄 삭제 — 대기(pending)·결정대기(approved/rejecting)·진행중(submitting)은 보존."""
    n = execute_rc("DELETE FROM fundreq_draft WHERE status IN ('submitted','rejected','failed','reject_failed')")
    return jsonify({'ok': True, 'deleted': n})


# ---- ext (맥 러너) ----
@app.route('/api/ext/fundreq/approved')
@api_key_required
def api_ext_fundreq_approved():
    """맥 러너가 상신할 approved 건 → status='submitting' 락(조건부)."""
    cols = "id, opex_cd, vsl_cd, raw_row"
    if request.args.get('peek'):
        rows = query(f"SELECT {cols} FROM fundreq_draft WHERE status='approved' ORDER BY id ASC")
        return jsonify({'count': len(rows), 'drafts': [dict(r) for r in rows], 'peek': True})
    out = [dict(r) for r in query(f"SELECT {cols} FROM fundreq_draft WHERE status='submitting' ORDER BY id ASC")]
    for r in query(f"SELECT {cols} FROM fundreq_draft WHERE status='approved' ORDER BY id ASC"):
        if execute_rc("UPDATE fundreq_draft SET status='submitting' WHERE id=? AND status='approved'", (r['id'],)):
            out.append(dict(r))
    return jsonify({'count': len(out), 'drafts': out})


@app.route('/api/ext/fundreq/rejecting')
@api_key_required
def api_ext_fundreq_rejecting():
    """맥 러너가 리젝할 rejecting 건(STATUS=R + 통보메일 대상)."""
    rows = query("SELECT id, opex_cd, vsl_cd, reject_reason, raw_row FROM fundreq_draft "
                 "WHERE status='rejecting' ORDER BY id ASC")
    return jsonify({'count': len(rows), 'drafts': [dict(r) for r in rows]})


@app.route('/api/ext/fundreq/drafts/<int:did>/result', methods=['POST'])
@api_key_required
def api_ext_fundreq_result(did):
    """상신 결과: ok=True → submitted, else failed."""
    d = request.get_json(silent=True) or {}
    ok = bool(d.get('ok'))
    rc = execute_rc("UPDATE fundreq_draft SET status=?, done_at=datetime('now','localtime'), result=? "
                    "WHERE id=? AND status='submitting'",
                    ('submitted' if ok else 'failed', (d.get('result') or '')[:2000], did))
    return jsonify({'id': did, 'ok': ok, 'applied': bool(rc)})


@app.route('/api/ext/fundreq/drafts/<int:did>/reject-result', methods=['POST'])
@api_key_required
def api_ext_fundreq_reject_result(did):
    """리젝 결과: ok=True → rejected, else reject_failed."""
    d = request.get_json(silent=True) or {}
    ok = bool(d.get('ok'))
    rc = execute_rc("UPDATE fundreq_draft SET status=?, done_at=datetime('now','localtime'), result=? "
                    "WHERE id=? AND status='rejecting'",
                    ('rejected' if ok else 'reject_failed', (d.get('result') or '')[:2000], did))
    return jsonify({'id': did, 'ok': ok, 'applied': bool(rc)})


# ============================================================
# reqgen — 입거 requisition 엑셀 → SVMS 구매청구 DRAFT 자동작성
#   /reqgen(admin): 엑셀 업로드 → S/ST 시트 파싱 → 카드 적재 → Voyage/Port/Date 입력+승인 →
#   automation_run(reqgen_save) 큐 → 맥 러너가 SVMS NEW→SP_SET_REQ_INFO DRAFT 저장.
#   매핑 근거: memory/svms-api-reqgen-save.md (F12 실캡처). 상신은 사람이 SVMS서 직접.
# ============================================================
_REQGEN_UNIT_MAP = {'PCS': 'EA'}
_REQGEN_EXP_RULES = [
    ('090301', ('MAIN ENGINE', 'M/E')),
    ('090302', ('G/E', 'GENERATOR', 'AUX ENGINE', 'A/E')),
    ('090303', ('BOILER',)),
    ('090304', ('CRANE', 'VALVE', 'WINCH', 'DECK')),
]


def _reqgen_infer_exp(part_tp, equipment, subject):
    if part_tp == '1':
        return '090403'                       # STORE → 정비용 선용품 고정
    hay = f"{equipment or ''} {subject or ''}".upper()
    for code, kws in _REQGEN_EXP_RULES:
        if any(k in hay for k in kws):
            return code
    return '090305'                           # 기타(애매)


def _reqgen_cell(ws, coord):
    v = ws[coord].value
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _reqgen_parse_sheet(ws, vsl_cd, vsl_nm):
    name = ws.title
    part_tp = '1' if name.upper().startswith('ST') else '0'
    part_tp_nm = 'Consumable' if part_tp == '1' else 'Spare Part'
    vnm = _reqgen_cell(ws, 'C4') or vsl_nm        # 시트 VESSEL(C4) 우선, INDEX G2 fallback
    equipment = _reqgen_cell(ws, 'C5')
    maker = _reqgen_cell(ws, 'C6')
    type_nm = _reqgen_cell(ws, 'G6')
    subject = _reqgen_cell(ws, 'C7')
    header = {
        'PART_TP': part_tp, 'PART_TP_NM': part_tp_nm,
        'VSL_CD': vsl_cd, 'VSL_NM': vnm,
        'CATE_NM': equipment, 'EQ_NM': equipment,
        'MAKER_NM': maker, 'TYPE_NM': type_nm,
        'SUBJ': (f"[DOCK] {subject}" if subject else '[DOCK]'),
        'DOCK_YN': 'Y', 'DEPT_CD': 'E', 'DEPT_CD_NM': 'Engine',
        'URG_YN': 'N', 'STATUS': 'N', 'DM_YN': 'N',
        'REQ_DT': None, 'PHR_DT': None, 'REQ_VOY': None, 'PHR_VOY': None,
        'REQ_PORT': None, 'REQ_PORT_NM': None, 'PHR_PORT': None, 'PHR_PORT_NM': None,
    }
    lines = []
    current_compo = None
    seq = 0
    for r in range(11, ws.max_row + 1):
        no = _reqgen_cell(ws, f'A{r}')
        partno = _reqgen_cell(ws, f'B{r}')
        desc = _reqgen_cell(ws, f'C{r}')
        unit = _reqgen_cell(ws, f'F{r}')
        qty = _reqgen_cell(ws, f'G{r}')
        if desc is None and partno is None and qty is None:
            continue
        if desc is not None and qty is None and no is None and partno is None:
            current_compo = desc                      # Component 그룹헤더
            continue
        if qty is None and no is None:
            continue
        seq += 1
        unit_cd = _REQGEN_UNIT_MAP.get(str(unit).upper(), unit) if unit else None
        lines.append({
            'SORT_SEQ': seq, 'COMPO_NM': current_compo,
            'MFG_PART_NO': partno, 'PART_NM': desc,
            'PUNIT_CD': unit_cd, 'REQ_QTY': qty,
            'EXP_CD': _reqgen_infer_exp(part_tp, equipment, subject), 'EQ_NM': equipment,
        })
    return {'sheet': name, 'header': header, 'lines': lines}


def _reqgen_parse_repair_sheet(ws, vsl_cd, vsl_nm):
    """R 시트(SHORE REPAIR) → 수리신청 draft. 라인그리드 없이 텍스트(REQ_DTL)."""
    name = ws.title
    vnm = _reqgen_cell(ws, 'C4') or vsl_nm        # 시트 VESSEL(C4) 우선
    equipment = _reqgen_cell(ws, 'C5')
    maker = _reqgen_cell(ws, 'C6')
    type_nm = _reqgen_cell(ws, 'G6')
    subject = _reqgen_cell(ws, 'C7')
    # ITEM LIST: A=No, B=JOB SCOPE, E=UNIT, F=Q'ty, G=REMARK
    scope = []
    for r in range(11, ws.max_row + 1):
        b = _reqgen_cell(ws, f'B{r}')
        if not b:
            continue
        scope.append({'scope': b, 'unit': _reqgen_cell(ws, f'E{r}'),
                      'qty': _reqgen_cell(ws, f'F{r}'), 'remark': _reqgen_cell(ws, f'G{r}')})
    # box3(REQ_DTL) 본문 구성
    lt = []
    for i, s in enumerate(scope, 1):
        t = s['scope'].lstrip('-').strip()
        ex = []
        q = (f"{s['qty']} {s['unit']}".strip() if (s['qty'] or s['unit']) else '')
        if q:
            ex.append(q)
        if s['remark']:
            ex.append(s['remark'])
        lt.append(f"{i}. {t}" + (f" — {' / '.join(ex)}" if ex else ""))
    req_dtl = ((f"{subject}. Please quote for the following job scope:\n\n" if subject else '')
               + "\n".join(lt))
    header = {
        'doc_type': 'MA', 'sheet': name, 'VSL_CD': vsl_cd, 'VSL_NM': vnm,
        'CATE_NM': equipment, 'EQ_NM': equipment, 'MAKER_NM': maker, 'TYPE_NM': type_nm,
        'SUBJ_BASE': subject, 'REQ_DTL': req_dtl,
        'RSN_CD': 'P', 'DEPT_CD': 'E', 'DOCK_YN': 'Y', 'URG_YN': 'N', 'STATUS': 'N',
        # 아래는 카드 공통입력(approve 시): APP_VOY/APP_PORT*/APP_DT, REQ_CAU, REQ_INS, REQ_STK
    }
    return {'sheet': name, 'doc_type': 'MA', 'header': header,
            'lines': scope, 'equipment': equipment, 'subj': subject}


def _reqgen_parse_workbook(stream, vsl_cd, vsl_nm=None):
    import re as _re
    from openpyxl import load_workbook
    wb = load_workbook(stream, data_only=True, read_only=True)
    if vsl_nm is None and 'INDEX' in wb.sheetnames:
        vsl_nm = _reqgen_cell(wb['INDEX'], 'G2')
    out = []
    for nm in wb.sheetnames:
        if _re.match(r'^(ST|S)\d+$', nm):                 # 구매청구
            res = _reqgen_parse_sheet(wb[nm], vsl_cd, vsl_nm)
            if res['lines']:
                out.append(res)
        elif _re.match(r'^R\d+$', nm):                    # 수리신청
            res = _reqgen_parse_repair_sheet(wb[nm], vsl_cd, vsl_nm)
            if res['lines']:
                out.append(res)
    return vsl_nm, out


@app.route('/reqgen')
@admin_required
def reqgen_page():
    return render_template('reqgen.html')


@app.route('/api/reqgen/upload', methods=['POST'])
@admin_required
def api_reqgen_upload():
    """엑셀 업로드 → S/ST 시트 파싱 → reqgen_draft 카드 적재(status=pending). SVMS 무영향."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '엑셀 파일(file) 필요'}), 400
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        return jsonify({'error': '.xlsx 파일만 가능'}), 400
    vsl_cd = (request.form.get('vsl_cd') or '').strip().upper() or None
    try:
        import io as _io
        stream = _io.BytesIO(f.read())            # SpooledTemporaryFile 은 seekable 아님 → BytesIO 로
        vsl_nm, sheets = _reqgen_parse_workbook(stream, vsl_cd)
    except Exception as e:
        return jsonify({'error': f'파싱 실패: {e}'}), 400
    if not sheets:
        return jsonify({'error': '청구 가능한 시트(S*/ST*/R*)에 항목이 없음'}), 400
    batch = uuid.uuid4().hex[:12]
    created = []
    for s in sheets:
        h, lines = s['header'], s['lines']
        dt = s.get('doc_type', 'PC')
        if dt == 'MA':                                   # 수리신청
            did = execute(
                "INSERT INTO reqgen_draft (batch, doc_type, sheet, vsl_cd, vsl_nm, part_tp, kind_nm, "
                "equipment, subj, line_cnt, exp_cd, header_json, lines_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch, 'MA', s['sheet'], vsl_cd, (h.get('VSL_NM') or vsl_nm), None, '수리', s['equipment'],
                 s['subj'], len(lines), None,
                 json.dumps(h, ensure_ascii=False), json.dumps(lines, ensure_ascii=False)))
        else:                                            # 구매청구
            did = execute(
                "INSERT INTO reqgen_draft (batch, doc_type, sheet, vsl_cd, vsl_nm, part_tp, kind_nm, "
                "equipment, subj, line_cnt, exp_cd, header_json, lines_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch, 'PC', s['sheet'], vsl_cd, (h.get('VSL_NM') or vsl_nm), h['PART_TP'], h['PART_TP_NM'], h['CATE_NM'],
                 h['SUBJ'], len(lines), (lines[0]['EXP_CD'] if lines else None),
                 json.dumps(h, ensure_ascii=False), json.dumps(lines, ensure_ascii=False)))
        created.append({'id': did, 'sheet': s['sheet'], 'doc_type': dt, 'lines': len(lines)})
    return jsonify({'batch': batch, 'vsl_nm': vsl_nm, 'vsl_cd': vsl_cd,
                    'count': len(created), 'drafts': created}), 201


@app.route('/api/reqgen/drafts')
@admin_required
def api_reqgen_list():
    status = request.args.get('status')
    if status:
        rows = query('SELECT * FROM reqgen_draft WHERE status=? ORDER BY id DESC', (status,))
    else:
        rows = query("SELECT * FROM reqgen_draft ORDER BY CASE status WHEN 'pending' THEN 0 "
                     "WHEN 'approved' THEN 1 WHEN 'saving' THEN 2 ELSE 3 END, id DESC")
    pending = query("SELECT COUNT(*) c FROM reqgen_draft WHERE status='pending'", one=True)
    return jsonify({'drafts': [dict(r) for r in rows], 'pending': pending['c'],
                    'enabled': _automation_enabled()})


@app.route('/api/reqgen/drafts/<int:did>', methods=['PATCH'])
@admin_required
def api_reqgen_patch(did):
    """카드 개별 설정 저장(수리 Stock of Spare 등). pending 상태만."""
    row = query('SELECT * FROM reqgen_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['status'] != 'pending':
        return jsonify({'error': 'pending 상태만 수정 가능', 'status': row['status']}), 409
    d = request.get_json(silent=True) or {}
    if 'stock' in d:
        stock = 'owner' if (d.get('stock') == 'owner') else 'service'
        execute("UPDATE reqgen_draft SET stock=? WHERE id=?", (stock, did))
        return jsonify({'id': did, 'stock': stock})
    return jsonify({'id': did, 'noop': True})


@app.route('/api/reqgen/drafts/<int:did>/approve', methods=['POST'])
@admin_required
def api_reqgen_approve(did):
    """승인 = SVMS 저장 지시. Voyage/Port/Date 를 헤더에 반영 후 status='approved' + 저장큐 적재."""
    row = query('SELECT * FROM reqgen_draft WHERE id=?', (did,), one=True)
    if not row:
        return jsonify({'error': 'not found'}), 404
    if row['status'] != 'pending':
        return jsonify({'error': 'already decided', 'status': row['status']}), 409
    d = request.get_json(silent=True) or {}
    voyage = (d.get('voyage') or row['voyage'] or '').strip()
    port = (d.get('port') or row['port'] or '').strip().upper()
    port_nm = (d.get('port_nm') or row['port_nm'] or '').strip()
    req_dt = (d.get('req_dt') or row['req_dt'] or '').strip().replace('-', '')
    missing = [k for k, v in (('Voyage', voyage), ('Port', port), ('Date', req_dt)) if not v]
    if missing:
        return jsonify({'error': f"승인 전 필수입력: {', '.join(missing)}", 'field': missing[0].lower()}), 400
    if not _automation_enabled():
        return jsonify({'error': 'killswitch ON — 자동화 정지중. 마스터 스위치 먼저 켜세요.'}), 409
    if not row['header_json']:
        return jsonify({'error': 'header_json 없음 — 카드 삭제 후 재업로드'}), 400
    header = json.loads(row['header_json'])
    header.update({'REQ_VOY': voyage, 'PHR_VOY': voyage,
                   'REQ_PORT': port, 'REQ_PORT_NM': port_nm or None,
                   'PHR_PORT': port, 'PHR_PORT_NM': port_nm or None,
                   'REQ_DT': req_dt, 'PHR_DT': req_dt})
    user = session.get('username') or 'web'
    rc = execute_rc("UPDATE reqgen_draft SET status='approved', header_json=?, voyage=?, port=?, "
                    "port_nm=?, req_dt=?, decided_at=datetime('now','localtime'), decided_by=? "
                    "WHERE id=? AND status='pending'",
                    (json.dumps(header, ensure_ascii=False), voyage, port, port_nm or None,
                     req_dt, user, did))
    if not rc:
        cur = query('SELECT status FROM reqgen_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': 'already decided', 'status': cur['status'] if cur else '?'}), 409
    rid = _queue_aor('reqgen_save', user)        # automation_run 큐(맥 러너가 claim)
    return jsonify({'id': did, 'status': 'approved', 'save_run': rid,
                    'message': '승인됨 — 맥 러너가 곧 SVMS DRAFT 저장(최대 1~2분)'})


@app.route('/api/reqgen/approve-all', methods=['POST'])
@admin_required
def api_reqgen_approve_all():
    """일괄 승인 — 공통 Voyage/Port/Date 를 모든 pending 카드 헤더에 반영 후 approved + 저장큐 1회.
    Port명(REQ_PORT_NM)은 비워둠 → 맥 러너가 포트코드로 SVMS 포트마스터에서 자동 채움."""
    d = request.get_json(silent=True) or {}
    voyage = (d.get('voyage') or '').strip()
    port = (d.get('port') or '').strip().upper()
    req_dt = (d.get('req_dt') or '').strip().replace('-', '')
    # 수리신청 공통 박스(Cause/Inspection은 선박공통, Stock은 카드별)
    cause = (d.get('cause') or '').strip()
    inspection = (d.get('inspection') or '').strip()
    def _stock_txt(sel):
        return ('Owner Supply' if sel == 'owner'
                else 'N/A, Relevant Spare parts & kits to be supplied by service company.')
    missing = [k for k, v in (('Voyage', voyage), ('Port', port), ('Date', req_dt)) if not v]
    if missing:
        return jsonify({'error': f"필수입력: {', '.join(missing)}", 'field': missing[0].lower()}), 400
    if not _automation_enabled():
        return jsonify({'error': 'killswitch ON — 자동화 정지중. 마스터 스위치 먼저 켜세요.'}), 409
    rows = query("SELECT * FROM reqgen_draft WHERE status='pending'")
    if not rows:
        return jsonify({'error': '대기(pending) 카드 없음'}), 400
    repair_rows = [r for r in rows if r['doc_type'] == 'MA']
    if repair_rows and not (cause and inspection):
        return jsonify({'error': '수리신청 카드가 있어 Cause/Inspection 입력 필요',
                        'field': 'cause' if not cause else 'inspection'}), 400
    user = session.get('username') or 'web'
    n = 0
    for row in rows:
        if not row['header_json']:
            continue
        header = json.loads(row['header_json'])
        if row['doc_type'] == 'MA':                  # 수리신청 — APP_* + 박스(Stock은 카드별)
            header.update({'APP_VOY': voyage, 'APP_PORT_CD': port, 'APP_PORT_NM': None,
                           'APP_DT': req_dt, 'REQ_CAU': cause, 'REQ_INS': inspection,
                           'REQ_STK': _stock_txt(row['stock'])})
        else:                                        # 구매청구 — REQ_*/PHR_*
            header.update({'REQ_VOY': voyage, 'PHR_VOY': voyage,
                           'REQ_PORT': port, 'PHR_PORT': port,
                           'REQ_PORT_NM': None, 'PHR_PORT_NM': None,
                           'REQ_DT': req_dt, 'PHR_DT': req_dt})
        rc = execute_rc("UPDATE reqgen_draft SET status='approved', header_json=?, voyage=?, port=?, "
                        "req_dt=?, decided_at=datetime('now','localtime'), decided_by=? "
                        "WHERE id=? AND status='pending'",
                        (json.dumps(header, ensure_ascii=False), voyage, port, req_dt, user, row['id']))
        if rc:
            n += 1
    rid = _queue_aor('reqgen_save', user)
    return jsonify({'approved': n, 'save_run': rid,
                    'message': f'{n}건 승인 — 맥 러너가 곧 SVMS 일괄 저장(최대 1~2분)'})


@app.route('/api/reqgen/drafts/<int:did>/reset', methods=['POST'])
@admin_required
def api_reqgen_reset(did):
    """승인 취소 — 저장 전(approved)만 pending 으로 복귀."""
    rc = execute_rc("UPDATE reqgen_draft SET status='pending', decided_at=NULL, decided_by=NULL "
                    "WHERE id=? AND status='approved'", (did,))
    if not rc:
        cur = query('SELECT status FROM reqgen_draft WHERE id=?', (did,), one=True)
        return jsonify({'error': '저장 전(approved)만 취소 가능', 'status': cur['status'] if cur else '?'}), 409
    return jsonify({'id': did, 'status': 'pending'})


@app.route('/api/reqgen/drafts/<int:did>', methods=['DELETE'])
@admin_required
def api_reqgen_delete(did):
    if not query('SELECT id FROM reqgen_draft WHERE id=?', (did,), one=True):
        return jsonify({'error': 'not found'}), 404
    execute('DELETE FROM reqgen_draft WHERE id=?', (did,))
    return jsonify({'id': did, 'deleted': True})


@app.route('/api/reqgen/drafts/decided', methods=['DELETE'])
@admin_required
def api_reqgen_clear_decided():
    """처리완료(saved/failed) 일괄 삭제 — pending/approved/saving 보존."""
    n = execute_rc("DELETE FROM reqgen_draft WHERE status IN ('saved','failed')")
    return jsonify({'ok': True, 'deleted': n})


@app.route('/api/reqgen/drafts/all', methods=['DELETE'])
@admin_required
def api_reqgen_clear_all():
    """전체 카드 삭제 — TRMT 카드 목록만 비움(SVMS에 저장된 청구서는 영향 없음)."""
    n = execute_rc("DELETE FROM reqgen_draft")
    return jsonify({'ok': True, 'deleted': n})


# ---- ext (맥 러너: SVMS DRAFT 저장 실행) ----
@app.route('/api/ext/reqgen/approved')
@api_key_required
def api_ext_reqgen_approved():
    """맥 러너가 저장할 approved 건 → status='saving' 락(조건부)."""
    cols = "id, doc_type, sheet, vsl_cd, vsl_nm, part_tp, header_json, lines_json"
    if request.args.get('peek'):
        rows = query(f"SELECT {cols} FROM reqgen_draft WHERE status='approved' ORDER BY id ASC")
        return jsonify({'count': len(rows), 'drafts': [dict(r) for r in rows], 'peek': True})
    out = [dict(r) for r in query(f"SELECT {cols} FROM reqgen_draft WHERE status='saving' ORDER BY id ASC")]
    for r in query(f"SELECT {cols} FROM reqgen_draft WHERE status='approved' ORDER BY id ASC"):
        if execute_rc("UPDATE reqgen_draft SET status='saving' WHERE id=? AND status='approved'", (r['id'],)):
            out.append(dict(r))
    return jsonify({'count': len(out), 'drafts': out})


@app.route('/api/ext/reqgen/drafts/<int:did>/result', methods=['POST'])
@api_key_required
def api_ext_reqgen_result(did):
    """저장 결과: ok=True → saved(+req_no), else failed(사람 재검토)."""
    d = request.get_json(silent=True) or {}
    ok = bool(d.get('ok'))
    rc = execute_rc("UPDATE reqgen_draft SET status=?, req_no=?, done_at=datetime('now','localtime'), "
                    "result=? WHERE id=? AND status='saving'",
                    ('saved' if ok else 'failed', (d.get('req_no') or None),
                     (d.get('result') or '')[:2000], did))
    return jsonify({'id': did, 'ok': ok, 'applied': bool(rc)})


AUTOMATION_TASKS = {
    'soa_g1':   'SOA 실버 G1 (ATBG·ATGR·ATGV·ATMT)',
    'soa_g2':   'SOA 실버 G2 (ATNH·ATSH·ATSL·JATX)',
    'soa_g3':   'SOA 실버 G3 (PCBJ·PCBS·PCGV·PCMC)',
    'soa_skrt': 'SOA 장금 (CPPS·INPS·KWPS·SAPS) +출금상신',
    'jeonja':   '전자결재 자동상신',
    'fundreq':  '비용청구(Fund Request) 자동상신 — 장금·Technical·Submitted',
    'soa_resend': '리젝 통보메일 재발송 (실패분)',
    'aor_prep':   'AOR(Technical) prep — Submitted AOR 카드화 (/aor 큐 적재)',
    'aor_submit': 'AOR 상신 — 승인된 건 SVMS 제출 (approve 시 자동큐)',
    'aor_reject': 'AOR 리젝 — STATUS=R + 관리사 통보메일 (reject 시 자동큐)',
    'reqgen_save': '구매청구 DRAFT 저장 — 승인된 입거 requisition 시트 SVMS 저장 (approve 시 자동큐)',
}
# verify=읽기전용 / live=자동승인·상신 / reject_dry=리젝후보표시 / reject_mark=리젝라인체크 / reject_submit=리젝제출+메일
AUTOMATION_MODES = ('verify', 'live', 'reject_dry', 'reject_mark', 'reject_submit')


def _automation_enabled():
    row = query("SELECT v FROM api_settings WHERE k='automation_enabled'", one=True)
    return (row['v'] if row else '1') != '0'


@app.route('/automation')
@admin_required
def automation_page():
    return render_template('automation.html')


@app.route('/api/automation/run', methods=['POST'])
@admin_required
def api_automation_run():
    _ensure_api_table()
    d = request.get_json(silent=True) or {}
    task = (d.get('task') or '').strip()
    mode = (d.get('mode') or 'verify').strip()
    if task not in AUTOMATION_TASKS or mode not in AUTOMATION_MODES:
        return jsonify({'error': 'bad task/mode'}), 400
    if not _automation_enabled():
        return jsonify({'error': 'killswitch ON — 자동화 정지중. 마스터 스위치 먼저 켜세요.'}), 409
    # lock: 같은 task가 queued/running이면 거부(중복클릭·동시실행 방지)
    busy = query("SELECT 1 FROM automation_run WHERE task=? AND status IN ('queued','running') LIMIT 1",
                 (task,), one=True)
    if busy:
        return jsonify({'error': '이미 실행 대기/진행중입니다.'}), 409
    import uuid
    rid = uuid.uuid4().hex[:12]
    execute("INSERT INTO automation_run (run_id, task, mode, status, requested_by) "
            "VALUES (?,?,?, 'queued', ?)", (rid, task, mode, session.get('username', '')))
    return jsonify({'ok': True, 'run_id': rid})


@app.route('/api/automation/runs')
@admin_required
def api_automation_runs():
    rows = query("SELECT run_id,task,mode,status,requested_at,started_at,finished_at,exit_code,summary "
                 "FROM automation_run ORDER BY id DESC LIMIT 40")
    total = query("SELECT COUNT(*) c FROM automation_run", one=True)['c']
    cleared = None
    try:
        r = query("SELECT v FROM api_settings WHERE k='automation_log_cleared'", one=True)
        if r and r['v']:
            cleared = json.loads(r['v'])
    except (sqlite3.Error, ValueError):
        pass
    return jsonify({
        'enabled': _automation_enabled(),
        'tasks': AUTOMATION_TASKS,
        'runs': [dict(r) for r in rows],
        'total': total,
        'cleared': cleared,
    })


@app.route('/api/automation/runs', methods=['DELETE'])
@admin_required
def api_automation_runs_clear():
    """완료/실패 로그만 삭제(진행중 보존). 삭제 행위 자체는 api_settings 에 기록."""
    _ensure_api_table()
    n = execute_rc("DELETE FROM automation_run WHERE status IN ('done','failed')")
    user = session.get('username', '')
    now = query("SELECT datetime('now','localtime') t", one=True)['t']
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('automation_log_cleared', ?)",
            (json.dumps({'at': now, 'by': user, 'n': n}, ensure_ascii=False),))
    return jsonify({'ok': True, 'deleted': n})


@app.route('/api/automation/killswitch', methods=['POST'])
@admin_required
def api_automation_killswitch():
    _ensure_api_table()
    d = request.get_json(silent=True) or {}
    on = bool(d.get('enabled'))
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('automation_enabled', ?)",
            ('1' if on else '0',))
    return jsonify({'ok': True, 'enabled': on})


# ---- ext (맥미니 launchd 폴링) ----
@app.route('/api/ext/automation/claim', methods=['POST'])
@api_key_required
def api_ext_automation_claim():
    if not _automation_enabled():
        return jsonify({'run': None, 'disabled': True})
    # 진행중이 있으면 신규 claim 안 함(스크립트 순차 실행 — SVMS 세션 충돌 방지)
    running = query("SELECT 1 FROM automation_run WHERE status='running' LIMIT 1", one=True)
    if running:
        return jsonify({'run': None, 'busy': True})
    row = query("SELECT id,run_id,task,mode FROM automation_run WHERE status='queued' ORDER BY id ASC LIMIT 1",
                one=True)
    if not row:
        return jsonify({'run': None})
    execute("UPDATE automation_run SET status='running', started_at=datetime('now','localtime') "
            "WHERE id=? AND status='queued'", (row['id'],))
    return jsonify({'run': {'run_id': row['run_id'], 'task': row['task'], 'mode': row['mode']}})


@app.route('/api/ext/automation/<run_id>/done', methods=['POST'])
@api_key_required
def api_ext_automation_done(run_id):
    d = request.get_json(silent=True) or {}
    status = 'failed' if (d.get('status') == 'failed' or d.get('exit_code')) else 'done'
    summary = (d.get('summary') or '')[:4000]
    execute("UPDATE automation_run SET status=?, finished_at=datetime('now','localtime'), "
            "exit_code=?, summary=? WHERE run_id=?",
            (status, d.get('exit_code'), summary, run_id))
    return jsonify({'ok': True})


# ═════════════════════════════════════════════════════════════════
#  mail_card — WF1+WF2 통합 (메일 1건 = 카드 1개: 이슈등록 + 회신작성)
#   · 이슈측: 기존 WF1 로직(이름→id 리졸브, 신규/append)
#   · 회신측: 손유석 한글지시 → 서버 Gemini 영문번역(스타일 하네스) → 맥미니 Outlook Draft
#   · 자동발송 절대 없음. 회신 LLM = Gemini(무료).
# ═════════════════════════════════════════════════════════════════
def _gemini_text(prompt, model=None):
    """plain-text Gemini 호출(번역용). returns (text, err)."""
    if not GEMINI_API_KEY:
        return None, 'NO_API_KEY'
    import urllib.request
    mdl = model or GEMINI_MODEL
    body = {'contents': [{'parts': [{'text': prompt}]}]}
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent'
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'),
        headers={'content-type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return None, str(e)[:200]
    try:
        t = ''
        for p in (data['candidates'][0]['content'].get('parts') or []):
            if isinstance(p.get('text'), str):
                t += p['text']
        t = t.strip()
        return (t if t else None), (None if t else 'EMPTY')
    except Exception as e:
        return None, 'parse:' + str(e)[:120]

_MAIL_REPLY_HARNESS = (
 "You render You Seok Son's (Owner's Technical Superintendent, Sinokor Tanker Mgmt Team 3) Korean reply "
 "instruction into a polished English business email, matching his actual writing style.\n"
 "RULES:\n"
 "- The Korean instruction is the SOURCE OF TRUTH for content. Render it faithfully. "
 "Do NOT invent facts, attachments, numbers, dates, names, or requests not in the instruction.\n"
 "- Open with 'Dear [recipient named in the instruction],'. For routine approvals/acknowledgements add a "
 "'Good day.' line; for firm/urgent or pure document-transmittal you may go straight to the point (no 'Good day.').\n"
 "- **Be terse — match his brevity.** A simple approval is ONE line, not a paragraph. Do NOT pad or restate. "
 "Prefer his stock phrasings: approval='Well noted. No objection to [...].' or 'Go ahead as per [...].'; "
 "instruction='Please raise the spare requisition accordingly.' / 'I arranged [service] at [port].'; "
 "rejection='[reason]. Reject your offer by AOR.'; closing requests='Please acknowledge receipt.' / 'Await prompt response.'\n"
 "- Use a numbered list '1) 2) 3)' ONLY for multi-item requests or document lists — not for a single action.\n"
 "- Firm tone when chasing/correcting: state fact → clarify the counterpart's responsibility → expected action + deadline "
 "(e.g. 'This should have been clearly stated in the original AOR submission. Please ensure such details are included upfront in future.').\n"
 "- NO pleasantries ('Thank you for your email', 'I hope this finds you well', 'Regarding...', 'Approval is granted.').\n"
 "- Preserve ALL numbers, dates, ports, vessel names, reference numbers and abbreviations EXACTLY "
 "(M/E, A/E, T/C, AOR, SIRE, SOA, BWTS, ETA/ETB/ETD, OPEX, PO etc.).\n"
 "- Do NOT write any signature. Output ONLY the email body text (plain), nothing else.\n\n"
 "EXAMPLES (his real style)\n"
 "1) Korean: Nektarios에게. 제안대로 진행하라고 승인.\n"
 "English:\nDear Nektarios,\nGood day.\nGo ahead as per your offer.\n\n"
 "2) Korean: Giorgos에게. 피스톤링 8세트 추가 공급 승인(이의 없음).\n"
 "English:\nDear Giorgos,\nGood day.\nWell noted. No objection to supply 8sets of piston ring additionally.\n\n"
 "3) Korean: Master에게. Busan에서 Total 서비스 수배했음(통보).\n"
 "English:\nDear Master,\nGood day.\nI arranged Total service at Busan.\n\n"
 "4) Korean: Captain에게. 싱가포르 국적 Ship Station License와 Certificate of Registry 첨부 송부. 수령확인 요청.\n"
 "English:\nDear Captain,\nPlease find attached the following documents for your reference and records:\n"
 "1) Ship Station License under Singapore flag\n2) Certificate of Registry under Singapore flag\nPlease acknowledge receipt.\n\n"
 "5) Korean: Nektarios에게. riding crew 입회가 1차 Fujairah인지 2차인지, 각 ETA 명시해 회신하라. 원래 AOR 제출 때 명확히 했어야. 앞으로 기본 계획사항은 미리 포함하라. (강경)\n"
 "English:\nDear Nektarios,\nPlease clarify whether the riding crew attendance is planned for the 1st Fujairah call or the 2nd Fujairah call, and provide the corresponding ETA for each port.\n"
 "This should have been clearly stated in the original AOR submission.\nPlease ensure such basic planning details are included upfront in future.\n\n"
 "6) Korean: Giorgos에게. 6/4 Sikka항 SIRE observation 7건 — 첨부 엑셀 'Action Plan'란에 진행사항·조치예정 작성, 'Status'란에 Open/Close 기록해 회신 요청. 미결은 ERD도 기재.\n"
 "English:\nDear Giorgos,\nGood day.\nPlease refer to the SIRE inspection carried out at Sikka on 04 June, which raised 7 observations.\n"
 "Kindly complete the attached Excel file and revert as follows:\n"
 "1) \"Action Plan\" column - describe the progress to date and the corrective action planned for each observation.\n"
 "2) \"Status\" column - record either Open or Close for each item.\n"
 "3) For any open item, state the Estimated Rectification Date in the Action Plan column.\nAwait prompt response."
)

def _mail_translate_card(card):
    """card(dict) reply_ko → 영문 reply_en. returns (en, err)."""
    ko = (card.get('reply_ko') or '').strip()
    if not ko:
        return None, 'NO_INSTRUCTION'
    style = (card.get('reply_style') or '').strip()
    ctx = []
    if card.get('email_subject'): ctx.append(f"(Context only — original mail subject: {card['email_subject']})")
    if card.get('summary_ko'):    ctx.append(f"(Context only — mail summary: {card['summary_ko']})")
    prompt = (_MAIL_REPLY_HARNESS + "\n\nNOW DO THIS ONE.\n" +
              ("\n".join(ctx) + "\n" if ctx else "") +
              (f"Tone/style: {style}\n" if style else "") +
              f"Korean: {ko}\nEnglish:")
    en, err = _gemini_text(prompt)
    if err:
        return None, err
    # faithful 가드(라이트): 지시에 숫자 있는데 결과에 하나도 없으면 의심
    import re as _re
    if _re.search(r'\d', ko) and not _re.search(r'\d', en or ''):
        return en, 'WARN_NO_DIGITS'
    return en, None


@app.route('/mail')
@admin_required
def mail_page():
    return render_template('mailcard.html')


@app.route('/api/mail/cards')
@admin_required
def api_mail_list():
    status = (request.args.get('status') or 'active').strip()
    if status == 'all':
        rows = query("SELECT * FROM mail_card ORDER BY card_status, pending DESC, id DESC")
    elif status == 'pending':
        rows = query("SELECT * FROM mail_card WHERE card_status='active' AND pending=1 ORDER BY id DESC")
    elif status == 'active':
        rows = query("SELECT * FROM mail_card WHERE card_status='active' AND pending=0 ORDER BY id DESC")
    else:  # archived 등
        rows = query("SELECT * FROM mail_card WHERE card_status=? ORDER BY id DESC", (status,))
    act = query("SELECT COUNT(*) c FROM mail_card WHERE card_status='active' AND pending=0", one=True)
    pnd = query("SELECT COUNT(*) c FROM mail_card WHERE card_status='active' AND pending=1", one=True)
    return jsonify({'count': len(rows), 'active': act['c'], 'pending': pnd['c'],
                    'cards': [dict(r) for r in rows]})


def _mail_get(cid):
    return query("SELECT * FROM mail_card WHERE id=?", (cid,), one=True)


def _mail_maybe_archive(cid):
    """이슈/회신 둘 다 종결이면 자동 archive."""
    r = _mail_get(cid)
    if not r:
        return
    # 이슈측·회신측 둘 다 종결돼야 archive(처리중에서 제거).
    # 이슈를 해당없음/리젝/등록 처리해도 회신이 아직 열려있으면(번역 등 더 쓸 수 있음) 처리중 유지.
    # 회신을 안 쓸 거면 회신 섹션의 '회신 안함'(dismiss) 1클릭으로 종결 → 그때 archive.
    # (2026-06-15: 해당없음만 눌러도 카드가 처리중에서 사라지던 문제 수정. 올마이트 approve.)
    issue_done = r['issue_status'] in ('registered', 'rejected', 'not_applicable')
    reply_done = r['reply_status'] in ('draft_created', 'dismissed')
    if issue_done and reply_done:
        execute("UPDATE mail_card SET card_status='archived' WHERE id=?", (cid,))


# ---- 이슈측 (WF1) ----
@app.route('/api/mail/<int:cid>/issue/register', methods=['POST'])
@admin_required
def api_mail_issue_register(cid):
    from datetime import date as _date
    r = _mail_get(cid)
    if not r:
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    mode = (d.get('mode') or 'new').strip()
    user = session.get('username') or 'web'
    if mode == 'append':
        mid = d.get('match_id') or r['issue_match_id']
        if not mid or not query('SELECT id FROM issues WHERE id=?', (mid,), one=True):
            return jsonify({'error': 'match issue not found'}), 400
        prog = (d.get('desc') or r['issue_desc'] or r['issue_item'] or '').strip()
        if not prog:
            return jsonify({'error': 'action text empty'}), 400
        arow = query('SELECT actions FROM issues WHERE id=?', (mid,), one=True)
        try:
            acts = json.loads(arow['actions']) if arow['actions'] else []
            if not isinstance(acts, list): acts = []
        except Exception:
            acts = []
        acts.append({'date': _date.today().isoformat(), 'progress': prog, 'important': False})
        execute('UPDATE issues SET actions=?, updated_at=datetime("now","localtime") WHERE id=?',
                (json.dumps(acts, ensure_ascii=False), mid))
        iid = mid
    else:
        item = (d.get('item') if 'item' in d else r['issue_item']) or ''
        item = item.strip()
        if not item:
            return jsonify({'error': 'item empty'}), 400
        desc = (d.get('desc') if 'desc' in d else r['issue_desc']) or ''
        ves = d.get('vessel') if 'vessel' in d else r['issue_vessel']
        sup = d.get('supervisor') if 'supervisor' in d else r['issue_supervisor']
        prio = d.get('priority') or r['issue_priority'] or 'Normal'
        if prio not in ('Normal', 'Urgent', 'COC & Flag', 'Next DD'):
            prio = 'Normal'
        vid = _resolve_vessel_id({'vessel_name': ves})
        sid = _resolve_supervisor_id({'supervisor_name': sup}) or session.get('supervisor_id')
        if not vid:
            return jsonify({'error': 'vessel unresolved', 'field': 'vessel',
                            'hint': '선박명 고쳐 다시'}), 400
        if not sid:
            return jsonify({'error': 'supervisor unresolved', 'field': 'supervisor'}), 400
        iid = execute("""INSERT INTO issues
            (supervisor_id, vessel_id, issue_date, due_date, item_topic, description,
             actions, priority, status, created_by)
            VALUES (?, ?, ?, NULL, ?, ?, '[]', ?, 'Open', ?)""",
            (sid, vid, _date.today().isoformat(), item, desc, prio, 'mail:' + user))
    execute("UPDATE mail_card SET issue_status='registered', issue_id=?, "
            "decided_at=datetime('now','localtime'), decided_by=? WHERE id=?", (iid, user, cid))
    _mail_maybe_archive(cid)
    return jsonify({'id': cid, 'issue_status': 'registered', 'issue_id': iid, 'ref': _ref('issue', iid)})


@app.route('/api/mail/<int:cid>/issue/<action>', methods=['POST'])
@admin_required
def api_mail_issue_status(cid, action):
    if action not in ('reject', 'na'):
        return jsonify({'error': 'bad action'}), 400
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    st = 'rejected' if action == 'reject' else 'not_applicable'
    execute("UPDATE mail_card SET issue_status=?, reject_reason=?, "
            "decided_at=datetime('now','localtime'), decided_by=? WHERE id=?",
            (st, (d.get('reason') or '').strip() or None, session.get('username') or 'web', cid))
    _mail_maybe_archive(cid)
    return jsonify({'id': cid, 'issue_status': st})


# ---- 회신측 (WF2: 한글지시 → Gemini 영문) ----
@app.route('/api/mail/<int:cid>/reply/save', methods=['POST'])
@admin_required
def api_mail_reply_save(cid):
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    ko = (d.get('reply_ko') or '').strip()
    style = (d.get('reply_style') or '').strip()
    st = 'none' if not ko else 'needs_info'  # 저장만 — 번역 전
    execute("UPDATE mail_card SET reply_ko=?, reply_style=?, reply_status=CASE "
            "WHEN reply_status IN ('draft_created','dismissed') THEN reply_status ELSE ? END WHERE id=?",
            (ko or None, style or None, st, cid))
    return jsonify({'id': cid, 'saved': True})


@app.route('/api/mail/<int:cid>/reply/translate', methods=['POST'])
@admin_required
def api_mail_reply_translate(cid):
    r = _mail_get(cid)
    if not r:
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    ko = (d.get('reply_ko') or r['reply_ko'] or '').strip()
    style = (d.get('reply_style') if 'reply_style' in d else r['reply_style']) or ''
    if not ko:
        execute("UPDATE mail_card SET reply_status='needs_info' WHERE id=?", (cid,))
        return jsonify({'error': 'reply_ko empty', 'reply_status': 'needs_info'}), 400
    card = dict(r); card['reply_ko'] = ko; card['reply_style'] = style
    en, err = _mail_translate_card(card)
    if err in ('NO_API_KEY', 'NO_INSTRUCTION') or en is None:
        return jsonify({'error': 'translate failed', 'detail': err}), 502
    execute("UPDATE mail_card SET reply_ko=?, reply_style=?, reply_en=?, "
            "reply_en_at=datetime('now','localtime'), reply_status='translated' WHERE id=?",
            (ko, style or None, en, cid))
    return jsonify({'id': cid, 'reply_en': en, 'reply_status': 'translated',
                    'warn': err if err == 'WARN_NO_DIGITS' else None})


@app.route('/api/mail/translate-all', methods=['POST'])
@admin_required
def api_mail_translate_all():
    rows = query("SELECT * FROM mail_card WHERE card_status='active' AND reply_ko IS NOT NULL "
                 "AND reply_ko<>'' AND reply_status IN ('none','needs_info') ORDER BY id LIMIT 20")
    done = 0; errs = []
    for r in rows:
        en, err = _mail_translate_card(dict(r))
        if en and err in (None, 'WARN_NO_DIGITS'):
            execute("UPDATE mail_card SET reply_en=?, reply_en_at=datetime('now','localtime'), "
                    "reply_status='translated' WHERE id=?", (en, r['id']))
            done += 1
        else:
            errs.append({'id': r['id'], 'err': err})
    return jsonify({'translated': done, 'errors': errs})


@app.route('/api/mail/<int:cid>/reply/draft-request', methods=['POST'])
@admin_required
def api_mail_reply_draft_request(cid):
    r = _mail_get(cid)
    if not r:
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    en = (d.get('reply_en') or r['reply_en'] or '').strip()
    if not en:
        return jsonify({'error': 'no english draft — translate first'}), 400
    execute("UPDATE mail_card SET reply_en=?, reply_en_at=datetime('now','localtime'), "
            "reply_status='draft_requested' WHERE id=?", (en, cid))
    return jsonify({'id': cid, 'reply_status': 'draft_requested'})


@app.route('/api/mail/<int:cid>/reply/dismiss', methods=['POST'])
@admin_required
def api_mail_reply_dismiss(cid):
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    execute("UPDATE mail_card SET reply_status='dismissed' WHERE id=?", (cid,))
    _mail_maybe_archive(cid)
    return jsonify({'id': cid, 'reply_status': 'dismissed'})


@app.route('/api/mail/<int:cid>/archive', methods=['POST'])
@admin_required
def api_mail_archive(cid):
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    execute("UPDATE mail_card SET card_status='archived' WHERE id=?", (cid,))
    return jsonify({'id': cid, 'card_status': 'archived'})


@app.route('/api/mail/<int:cid>/delete', methods=['POST', 'DELETE'])
@admin_required
def api_mail_delete(cid):
    """카드 영구삭제. 등록된 이슈(issue_id)는 건드리지 않고 카드만 제거."""
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    execute("DELETE FROM mail_card WHERE id=?", (cid,))
    return jsonify({'id': cid, 'deleted': True})


@app.route('/api/mail/<int:cid>/pending', methods=['POST'])
@admin_required
def api_mail_pending(cid):
    """보류 토글. body {off:true} 면 보류 해제(처리중 복귀), 없으면 보류 설정."""
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    d = request.get_json(silent=True) or {}
    val = 0 if d.get('off') else 1
    execute("UPDATE mail_card SET pending=? WHERE id=?", (val, cid))
    return jsonify({'id': cid, 'pending': val})


@app.route('/api/mail/cards/delete-all', methods=['POST'])
@admin_required
def api_mail_delete_all():
    """현재 보기(scope) 범위의 카드 일괄 영구삭제. 등록된 이슈(issue_id)는 보존 — 카드만 제거."""
    d = request.get_json(silent=True) or {}
    scope = (d.get('scope') or '').strip()
    where = {
        'all': "",
        'pending': "WHERE card_status='active' AND pending=1",
        'active': "WHERE card_status='active' AND pending=0",
        'archived': "WHERE card_status='archived'",
    }.get(scope)
    if where is None:
        return jsonify({'error': 'bad scope'}), 400
    n = execute_rc(f"DELETE FROM mail_card {where}")
    return jsonify({'deleted': n, 'scope': scope})


# ---- ext (맥미니) ----
@app.route('/api/ext/mail/cards', methods=['POST'])
@api_key_required
def api_ext_mail_create():
    """맥미니 ingest: 스캔한 메일 + 요약 + 이슈제안 적재."""
    d = request.get_json(silent=True) or {}
    msg_id = (d.get('email_msg_id') or '').strip() or None
    if msg_id:
        dup = query("SELECT id FROM mail_card WHERE email_msg_id=? AND card_status='active'",
                    (msg_id,), one=True)
        if dup:
            return jsonify({'id': dup['id'], 'dedup': True}), 200
    issue_status = (d.get('issue_status') or 'pending').strip()
    if issue_status not in ('pending', 'not_applicable'):
        issue_status = 'pending'
    prio = d.get('issue_priority') or 'Normal'
    if prio not in ('Normal', 'Urgent', 'COC & Flag', 'Next DD'):
        prio = 'Normal'
    cid = execute("""INSERT INTO mail_card
        (email_subject, email_from, email_date, email_msg_id, summary_ko, thread_summary_ko, body_en,
         issue_item, issue_desc, issue_match_id, issue_priority, issue_vessel, issue_supervisor,
         issue_status, reply_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'none')""", (
        d.get('email_subject') or None, d.get('email_from') or None, d.get('email_date') or None,
        msg_id, d.get('summary_ko') or None, d.get('thread_summary_ko') or None, d.get('body_en') or None,
        d.get('issue_item') or None, d.get('issue_desc') or None,
        d.get('issue_match_id'), prio, d.get('issue_vessel') or None, d.get('issue_supervisor') or None,
        issue_status))
    return jsonify({'id': cid}), 201


@app.route('/api/ext/mail/draft-queue')
@api_key_required
def api_ext_mail_draft_queue():
    """맥미니 폴링: 회신 Outlook Draft 만들 카드(reply_status=draft_requested)."""
    rows = query("SELECT id, email_msg_id, reply_en, reply_en_at FROM mail_card "
                 "WHERE reply_status='draft_requested' ORDER BY id")
    return jsonify({'count': len(rows), 'queue': [dict(r) for r in rows]})


@app.route('/api/ext/mail/<int:cid>/mark-draft', methods=['POST'])
@api_key_required
def api_ext_mail_mark_draft(cid):
    if not _mail_get(cid):
        return jsonify({'error': 'not found'}), 404
    execute("UPDATE mail_card SET reply_status='draft_created', "
            "decided_at=datetime('now','localtime') WHERE id=?", (cid,))
    _mail_maybe_archive(cid)
    return jsonify({'id': cid, 'reply_status': 'draft_created'})


# ═════════════════════════════════════════════════════════════════
#  CLASS STATUS (선급 Class Status Report 업로드/추출/매칭)
# ═════════════════════════════════════════════════════════════════
import re as _re_cls


def _norm_vessel_name(name):
    """선명 정규화: 대문자, M/T·M/V 접두 제거, 공백 단일화."""
    if not name:
        return ''
    s = str(name).upper().strip()
    s = _re_cls.sub(r'^(M[\./]?\s*[TV][\./]?|MT|MV)\s+', '', s)  # M/T, M.V., MT, MV ...
    s = _re_cls.sub(r'[^A-Z0-9 ]+', ' ', s)
    s = _re_cls.sub(r'\s+', ' ', s).strip()
    return s


def _match_vessel_by_name(name):
    """보고서 선명 → vessels 행 매칭. 정확 일치 우선, 없으면 부분포함. 실패 시 None."""
    target = _norm_vessel_name(name)
    if not target:
        return None
    rows = query('SELECT * FROM vessels WHERE active=1')
    norm = [(v, _norm_vessel_name(v['name'])) for v in rows]
    for v, n in norm:
        if n == target:
            return v
    # 부분 포함 (한쪽이 다른 쪽을 포함)
    for v, n in norm:
        if n and (n in target or target in n):
            return v
    return None


def _class_status_prompt():
    return (
        "다음은 선박 선급(Classification Society)의 'Class Status Report' 또는 "
        "'Survey Status Report'다. (선급 예: DNV, BV, KR, ABS, LR, NK 등 — 포맷이 다를 수 있다.)\n"
        "아래 정보를 추출해 지정한 JSON으로만 답하라.\n"
        "■ 공통 정보\n"
        "- vessel_name: 보고서의 선명(Name of vessel / Ship name). 대문자 원문.\n"
        "- class_society: 발행 선급 약어 (DNV / BV / KR / ABS / LR / NK 중 하나, 식별 가능하면).\n"
        "- report_date: 보고서 발행일/생성일 (Date of issue / Generated on). 가능하면 YYYY-MM-DD.\n"
        "■ 추출 대상 — 'Open(미해소)' 상태인 항목만:\n"
        "  (1) coc  = Condition of Class / 선급지적. 선급별 명칭 예:\n"
        "      DNV 'Conditions related to class', BV 'Conditions of Class', "
        "ABS 'Conditions of Class / Outstanding', LR 'Conditions of Class(COC)', "
        "또한 BV 'Planned Inspection Items'의 Recommendation(R)/Condition of Class 도 포함.\n"
        "  (2) statutory = Condition of Statutory / 기국(법정)사항. 예:\n"
        "      DNV 'Conditions related to statutory certificates', "
        "BV 'Statutory Recommendations' 및 'Planned Inspection Items'의 Observation(Obs)/Statutory 항목.\n"
        "■ 제외: 단순 Survey 예정표(1-Year Planner/Surveys 목록), 인증서 목록, "
        "Memoranda(메모란다/Class Memoranda)는 추출하지 마라. 이미 Closed/Cleared/Deleted "
        "되었거나 조치 확인 완료된 항목도 제외. 'None'이면 빈 배열.\n"
        "■ 각 항목 필드:\n"
        "- issued_date: 발행/기재일 (가능하면 YYYY-MM-DD, 없으면 빈 문자열)\n"
        "- description: 지적/기국 본문을 원문 그대로 복사(영문이면 영문 그대로). 요약·변형 금지.\n"
        "- due_date: 마감/처리기한 (Due/Limit date, 가능하면 YYYY-MM-DD, 없으면 빈 문자열)\n"
        "- remark: description의 핵심을 한국어 1~2문장으로 간결히 요약(전체 직역 금지). "
        "문장은 '~함/~됨/~음' 음슴체(개조식). 기술 명칭·장비명·약어·인증명(예: COC, SEEMP, IHM, "
        "BNWAS, Load Line, Plimsoll Mark, EGCS, BWTS)은 영문 그대로 둔다." + _MARITIME_TERMS + "\n"
        "없는 내용을 지어내지 말 것.\n"
        '형식: {"vessel_name":"","class_society":"","report_date":"",'
        '"coc":[{"issued_date":"","description":"","due_date":"","remark":""}],'
        '"statutory":[{"issued_date":"","description":"","due_date":"","remark":""}]}'
    )


def _cls_item(it):
    if not isinstance(it, dict):
        return None
    rec = {
        'issued_date': (it.get('issued_date') or '').strip(),
        'description': (it.get('description') or '').strip(),
        'due_date':    (it.get('due_date') or '').strip(),
        'remark':      (it.get('remark') or '').strip(),
    }
    return rec if rec['description'] else None


def _normalize_class_status(parsed):
    if not isinstance(parsed, dict):
        return None
    def lst(key):
        out = []
        for it in (parsed.get(key) or []):
            r = _cls_item(it)
            if r:
                out.append(r)
        return out
    return {
        'vessel_name':   (parsed.get('vessel_name') or '').strip(),
        'class_society': (parsed.get('class_society') or '').strip().upper(),
        'report_date':   (parsed.get('report_date') or '').strip(),
        'coc':           lst('coc'),
        'statutory':     lst('statutory'),
    }


def _xlsx_to_text(raw_bytes):
    import io
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        for r in ws.iter_rows(values_only=True):
            cells = ['' if c is None else str(c).strip() for c in r]
            if any(cells):
                lines.append('\t'.join(cells))
            if len(lines) > 600:
                break
    return '\n'.join(lines)


def _extract_class_status_from_upload(f):
    """업로드 FileStorage → (data, err). data = _normalize_class_status 결과."""
    name = (f.filename or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    raw = f.read()
    size_mb = len(raw) / (1024 * 1024)
    prompt = _class_status_prompt()

    if ext == 'pdf':
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE',
                          'message': f'PDF가 너무 큽니다({size_mb:.1f}MB). 15MB 이하로 줄여주세요.'}
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': 'application/pdf', 'data': b64}},
            {'text': prompt},
        ], model=_model_for('findings'))
    elif ext in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'):
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'이미지가 너무 큽니다({size_mb:.1f}MB).'}
        import mimetypes
        media = mimetypes.guess_type(name)[0] or 'image/jpeg'
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': media, 'data': b64}},
            {'text': prompt},
        ], model=_model_for('findings'))
    elif ext in ('xlsx', 'xls'):
        try:
            txt = _xlsx_to_text(raw)
        except Exception as e:
            return None, {'reason': 'XLSX_PARSE_FAILED', 'message': f'엑셀을 읽지 못했습니다: {e}'}
        parsed = _gemini_call_json([{'text': prompt + '\n\n[보고서 표 내용]\n' + txt}],
                                   model=_model_for('findings'))
    else:
        return None, {'reason': 'BAD_TYPE', 'message': 'PDF · 이미지 · 엑셀(xlsx) 파일만 지원합니다.'}

    if isinstance(parsed, dict) and parsed.get('error') == 'NO_API_KEY':
        return None, {'reason': 'no_api_key', 'message': 'AI 자동추출이 설정되지 않았습니다(키 미설정).'}
    if isinstance(parsed, dict) and parsed.get('error'):
        return None, {'reason': parsed['error'], 'message': '자동 추출에 실패했습니다.',
                      'detail': parsed.get('detail') or parsed.get('raw')}
    data = _normalize_class_status(parsed)
    if data is None:
        return None, {'reason': 'PARSE_FAILED', 'message': '추출 결과를 해석하지 못했습니다.'}
    return data, None


def _cls_snapshot_dict(cs_row, items_by_cs):
    items = items_by_cs.get(cs_row['id'], [])
    coc = [dict(i) for i in items if i['category'] == 'COC']
    stat = [dict(i) for i in items if i['category'] == 'STATUTORY']
    return {
        'id':              cs_row['id'],
        'vessel_id':       cs_row['vessel_id'],
        'vessel_name_raw': cs_row['vessel_name_raw'],
        'class_society':   cs_row['class_society'],
        'report_date':     cs_row['report_date'],
        'source_filename': cs_row['source_filename'],
        'updated_at':      cs_row['updated_at'],
        'coc':             coc,
        'statutory':       stat,
    }


def _cls_save_snapshot(vessel_id, vessel_name_raw, data, filename):
    """선박 스냅샷 교체(최신만 유지). vessel_id None 이면 미매칭으로 저장
    (같은 정규화 선명의 기존 미매칭은 제거 후 삽입)."""
    conn = get_db()
    user = session.get('username')
    if vessel_id is not None:
        conn.execute('DELETE FROM class_status WHERE vessel_id=?', (vessel_id,))
    else:
        # 같은 (정규화) 선명의 기존 미매칭 스냅샷 제거
        tgt = _norm_vessel_name(vessel_name_raw)
        for r in conn.execute('SELECT id, vessel_name_raw FROM class_status WHERE vessel_id IS NULL').fetchall():
            if _norm_vessel_name(r['vessel_name_raw']) == tgt:
                conn.execute('DELETE FROM class_status WHERE id=?', (r['id'],))
    cur = conn.execute(
        '''INSERT INTO class_status
             (vessel_id, vessel_name_raw, class_society, report_date, source_filename, uploaded_by)
           VALUES (?,?,?,?,?,?)''',
        (vessel_id, vessel_name_raw, data.get('class_society'),
         data.get('report_date'), filename, user))
    cs_id = cur.lastrowid
    for cat, key in (('COC', 'coc'), ('STATUTORY', 'statutory')):
        for n, it in enumerate(data.get(key) or [], start=1):
            conn.execute(
                '''INSERT INTO class_status_items
                     (cs_id, category, no, issued_date, description, due_date, remark)
                   VALUES (?,?,?,?,?,?,?)''',
                (cs_id, cat, n, it.get('issued_date'), it.get('description'),
                 it.get('due_date'), it.get('remark')))
    conn.commit()
    return cs_id


@app.route('/api/class-status', methods=['GET'])
@login_required
def api_class_status_list():
    """매칭 선박별 스냅샷 + 미매칭 버킷.
    Query: ?supervisor_id=N (지정 시 해당 감독 담당선박만, 미매칭은 미포함)"""
    sup_id = request.args.get('supervisor_id', type=int)

    all_cs = query('SELECT * FROM class_status ORDER BY updated_at DESC')
    cs_ids = [r['id'] for r in all_cs]
    items_by_cs = {cid: [] for cid in cs_ids}
    if cs_ids:
        ph = ','.join('?' * len(cs_ids))
        for it in query(f'SELECT * FROM class_status_items WHERE cs_id IN ({ph}) '
                        f'ORDER BY cs_id, category, no', tuple(cs_ids)):
            items_by_cs[it['cs_id']].append(it)

    snap_by_vessel = {r['vessel_id']: r for r in all_cs if r['vessel_id'] is not None}

    # 대상 선박: 스냅샷 보유 선박만 (감독 필터 적용)
    vessel_ids = list(snap_by_vessel.keys())
    vessels = []
    if vessel_ids:
        ph = ','.join('?' * len(vessel_ids))
        sql = f'SELECT * FROM vessels WHERE id IN ({ph})'
        params = list(vessel_ids)
        if sup_id:
            sql += (' AND EXISTS (SELECT 1 FROM supervisor_vessels sv '
                    'WHERE sv.vessel_id=vessels.id AND sv.supervisor_id=?)')
            params.append(sup_id)
        sql += ' ORDER BY name'
        vessels = query(sql, tuple(params))

    sv_map = {}
    if vessels:
        vids = [v['id'] for v in vessels]
        ph2 = ','.join('?' * len(vids))
        for r in query(f'SELECT vessel_id, supervisor_id FROM supervisor_vessels '
                       f'WHERE vessel_id IN ({ph2})', tuple(vids)):
            sv_map.setdefault(r['vessel_id'], []).append(r['supervisor_id'])

    vessel_out = []
    for v in vessels:
        vd = dict(v)
        vd['supervisor_ids'] = sv_map.get(v['id'], [])
        vessel_out.append({
            'vessel': vd,
            'snapshot': _cls_snapshot_dict(snap_by_vessel[v['id']], items_by_cs),
        })

    unmatched = []
    if not sup_id:
        for r in all_cs:
            if r['vessel_id'] is None:
                unmatched.append(_cls_snapshot_dict(r, items_by_cs))

    return jsonify({'vessels': vessel_out, 'unmatched': unmatched})


def _cls_handle_files(files):
    """업로드 파일들 → AI추출 → 선박매칭 → 저장. (UI 버튼·BV Pushing 공용)"""
    results = []
    for f in [x for x in files if x and x.filename]:
        fname = f.filename
        data, err = _extract_class_status_from_upload(f)
        if err:
            results.append({'filename': fname, 'ok': False, **err})
            continue
        vname = data.get('vessel_name') or ''
        v = _match_vessel_by_name(vname)
        vessel_id = v['id'] if v else None
        _cls_save_snapshot(vessel_id, vname, data, fname)
        results.append({
            'filename': fname, 'ok': True,
            'vessel_name': vname,
            'matched': bool(v),
            'vessel_id': vessel_id,
            'matched_name': v['name'] if v else None,
            'class_society': data.get('class_society'),
            'report_date': data.get('report_date'),
            'coc_count': len(data.get('coc') or []),
            'statutory_count': len(data.get('statutory') or []),
        })
    return results


@app.route('/api/class-status/upload', methods=['POST'])
@login_required
def api_class_status_upload():
    files = request.files.getlist('files') or (
        [request.files['file']] if 'file' in request.files else [])
    if not [f for f in files if f and f.filename]:
        return jsonify({'ok': False, 'message': '파일이 없습니다.'}), 400
    results = _cls_handle_files(files)
    return jsonify({'ok': any(r.get('ok') for r in results), 'results': results})


@app.route('/api/class-status/push', methods=['POST'])
@admin_required
def api_class_status_push():
    """'BV에서 Pushing' 버튼 — 맥 러너가 폴링해서 BV→Class Status 동기화하도록 플래그."""
    _ensure_api_table()
    now = query("SELECT datetime('now','localtime') t", one=True)['t']
    execute("INSERT OR REPLACE INTO api_settings (k, v) VALUES ('cls_push_flag', ?)", (now,))
    return jsonify({'ok': True, 'flagged_at': now})


@app.route('/api/class-status/items/<int:iid>', methods=['PUT'])
@login_required
def api_class_status_item_update(iid):
    row = query('SELECT * FROM class_status_items WHERE id=?', (iid,), one=True)
    if not row:
        abort(404)
    d = request.get_json(silent=True) or {}
    fields, params = [], []
    for col in ('importance', 'remark', 'description', 'issued_date', 'due_date'):
        if col in d:
            val = d[col]
            if col == 'importance' and val not in ('', 'Urgent'):
                val = 'Urgent' if val else ''
            fields.append(f'{col}=?'); params.append(val)
    if not fields:
        return jsonify({'ok': True})
    fields.append("updated_at=datetime('now','localtime')")
    params.append(iid)
    execute(f'UPDATE class_status_items SET {", ".join(fields)} WHERE id=?', tuple(params))
    return jsonify({'ok': True})


@app.route('/api/class-status/<int:cs_id>', methods=['DELETE'])
@login_required
def api_class_status_delete(cs_id):
    if not query('SELECT id FROM class_status WHERE id=?', (cs_id,), one=True):
        abort(404)
    execute('DELETE FROM class_status WHERE id=?', (cs_id,))
    return jsonify({'ok': True})


@app.route('/api/class-status/<int:cs_id>/assign', methods=['POST'])
@login_required
def api_class_status_assign(cs_id):
    """미매칭 스냅샷을 특정 선박에 수동 배정(기존 선박 스냅샷은 교체)."""
    snap = query('SELECT * FROM class_status WHERE id=?', (cs_id,), one=True)
    if not snap:
        abort(404)
    d = request.get_json(silent=True) or {}
    vessel_id = d.get('vessel_id')
    if not vessel_id or not query('SELECT id FROM vessels WHERE id=?', (vessel_id,), one=True):
        return jsonify({'ok': False, 'message': '유효한 선박을 선택하세요.'}), 400
    conn = get_db()
    # 대상 선박의 기존 스냅샷 제거 후 배정
    conn.execute('DELETE FROM class_status WHERE vessel_id=? AND id<>?', (vessel_id, cs_id))
    conn.execute("UPDATE class_status SET vessel_id=?, updated_at=datetime('now','localtime') "
                 "WHERE id=?", (vessel_id, cs_id))
    conn.commit()
    return jsonify({'ok': True})


@app.route('/api/class-status/<int:cs_id>/export')
@login_required
def api_class_status_export(cs_id):
    from flask import send_file
    snap = query('SELECT * FROM class_status WHERE id=?', (cs_id,), one=True)
    if not snap:
        abort(404)
    vname = snap['vessel_name_raw'] or ''
    if snap['vessel_id']:
        vrow = query('SELECT name FROM vessels WHERE id=?', (snap['vessel_id'],), one=True)
        if vrow:
            vname = vrow['name']
    items = query('SELECT * FROM class_status_items WHERE cs_id=? ORDER BY category, no', (cs_id,))
    cat_ko = {'COC': '선급지적(COC)', 'STATUTORY': '기국(Statutory)'}
    rows = []
    for it in items:
        rows.append([
            cat_ko.get(it['category'], it['category']),
            it['no'],
            it['issued_date'] or '',
            it['description'] or '',
            it['due_date'] or '',
            it['remark'] or '',
            it['importance'] or '',
        ])
    headers = ['Category', 'No', 'Issued', 'Description', 'Due', '한글 요약', 'Urgent']
    subtitle = f"{snap['class_society'] or ''}  ·  발행 {snap['report_date'] or '-'}"
    bio = _findings_workbook(
        f'{vname} Class Status', subtitle, headers, rows,
        wrap_cols={4, 6}, widths=[16, 5, 13, 60, 13, 40, 8])
    safe = _re_cls.sub(r'[^A-Za-z0-9가-힣 _-]', '', vname).strip() or 'class_status'
    return send_file(bio, as_attachment=True,
                     download_name=f'{safe}_ClassStatus.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ═════════════════════════════════════════════════════════════════
#  CLI entry
# ═════════════════════════════════════════════════════════════════
def _auto_migrate():
    """기존 DB에 대한 idempotent 스키마 보강 — 배포 시 마이그레이션 누락 방지.
    · schema.sql 의 CREATE TABLE/INDEX IF NOT EXISTS 재적용(누락 테이블 생성)
    · ALTER 가 필요한 신규 컬럼은 개별 점검 후 추가
    """
    if not os.path.exists(DATABASE):
        return
    conn = sqlite3.connect(DATABASE)
    try:
        try:
            with open(SCHEMA_FILE, encoding='utf-8') as fh:
                conn.executescript(fh.read())   # 전부 IF NOT EXISTS → 무해
        except Exception as e:
            print(f'[auto_migrate] schema 재적용 건너뜀: {e}')
        # vt_findings.user_remark (자율 입력 Remark), priority (중요 체크)
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(vt_findings)').fetchall()]
            if cols and 'user_remark' not in cols:
                conn.execute("ALTER TABLE vt_findings ADD COLUMN user_remark TEXT NOT NULL DEFAULT ''")
                print('[auto_migrate] vt_findings.user_remark 추가됨')
            if cols and 'priority' not in cols:
                conn.execute("ALTER TABLE vt_findings ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
                print('[auto_migrate] vt_findings.priority 추가됨')
        except Exception as e:
            print(f'[auto_migrate] vt_findings 컬럼 점검 건너뜀: {e}')

        # mail_card.pending (보류 플래그)
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(mail_card)').fetchall()]
            if cols and 'pending' not in cols:
                conn.execute("ALTER TABLE mail_card ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
                print('[auto_migrate] mail_card.pending 추가됨')
            if cols and 'thread_summary_ko' not in cols:
                conn.execute("ALTER TABLE mail_card ADD COLUMN thread_summary_ko TEXT")
                print('[auto_migrate] mail_card.thread_summary_ko 추가됨')
            if cols and 'body_en' not in cols:
                conn.execute("ALTER TABLE mail_card ADD COLUMN body_en TEXT")
                print('[auto_migrate] mail_card.body_en 추가됨')
        except Exception as e:
            print(f'[auto_migrate] mail_card.pending 점검 건너뜀: {e}')

        # vettings.valid: 옛 CHECK(valid IN ('Valid','Invalid')) 제거 → 'Next Plan'/'Last Result' 허용
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='vettings'"
            ).fetchone()
            ddl = (row[0] if row else '') or ''
            if "'Valid','Invalid'" in ddl.replace(' ', ''):
                print('[auto_migrate] vettings.valid CHECK 제약 갱신 중...')
                conn.execute('PRAGMA legacy_alter_table=ON')
                conn.execute('PRAGMA foreign_keys=OFF')
                conn.execute('ALTER TABLE vettings RENAME TO _vettings_old')
                with open(SCHEMA_FILE, encoding='utf-8') as fh:
                    conn.executescript(fh.read())   # 새 vettings(CHECK 없음) 생성, 나머지 no-op
                conn.execute("""
                    INSERT INTO vettings
                        (id, vessel_id, report_number, inspection_date, inspection_company,
                         inspector, port, operation, sire_type, valid, overall_remark,
                         manual_observation_count, manual_open_count, manual_close_count,
                         created_by, created_at, updated_at)
                    SELECT
                         id, vessel_id, report_number, inspection_date, inspection_company,
                         inspector, port, operation, sire_type, valid, overall_remark,
                         manual_observation_count, manual_open_count, manual_close_count,
                         created_by, created_at, updated_at
                    FROM _vettings_old
                """)
                conn.execute('DROP TABLE _vettings_old')
                conn.execute('PRAGMA legacy_alter_table=OFF')
                conn.execute('PRAGMA foreign_keys=ON')
                conn.commit()
                print('[auto_migrate] vettings.valid CHECK 제약 갱신 완료')
        except Exception as e:
            print(f'[auto_migrate] vettings 재생성 건너뜀: {e}')

        conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--init-db':
        init_db(drop=True)
        sys.exit(0)

    if not os.path.exists(DATABASE):
        print('[INFO] DB 파일이 없어 자동 초기화합니다.')
        init_db(drop=False)
    else:
        _auto_migrate()

    # 개발 환경
    app.run(host='0.0.0.0', port=5000, debug=True)

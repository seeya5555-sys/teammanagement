"""
TRMT3 Ship Management System
────────────────────────────────────────────────────────────────
Flask 메인 (DD Manager 스타일 — 단일 파일, 순수 SQL, ORM 없음)

로컬 실행        :  python app.py
DB 재초기화     :  python app.py --init-db
"""
import os
import re
import math
import sys
import uuid
import json
import sqlite3
import secrets
import threading
import time
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request
import mimetypes
from io import BytesIO
from functools import wraps
from datetime import timedelta, date, datetime

from flask import (
    Flask, g, request, jsonify, session, render_template,
    redirect, url_for, send_from_directory, send_file, abort, make_response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
import hmac, hashlib
from itsdangerous import BadData

import csrf
import migration_steps

from app_core import (
    ALLOWED_EXT, AOR_PDF_DIR, BASE_DIR, DATABASE, DOCKATT_FILE_DIR, FUNDREQ_FILE_DIR,
    INSTANCE_DIR, INVOICE_PDF_DIR, JEONJA_PDF_DIR, SCHEMA_FILE, SECRET_KEY_FILE, SEED_FILE,
    SOA_REVIEW_PDF_DIR, STT_AUDIO_DIR, STT_AUDIO_EXT, STT_LEASE_SEC, STT_MAX_ATTEMPTS,
    STT_MAX_BYTES, UPLOAD_DIR, _NON_STT_UPLOAD_MAX, _SOA_REVIEW_SNAPSHOT_MAX,
    _load_or_create_secret_key, app, close_db, execute, execute_rc, get_db, init_runtime, query,
)
from token_auth import (
    _DUMMY_PW_HASH, _TOKEN_MAXAGE, _issue_token, _load_token, _pw_fingerprint,
    _token_note_fail, _token_rate_limited, _token_reset_fails,
)


@app.before_request
def _limit_non_stt_upload():
    """회의록 오디오(장시간 회의)만 200MB 허용, 그 외 모든 업로드는 20MB로 조임.
    전역 MAX_CONTENT_LENGTH를 200MB로 올렸으므로 나머지 경로를 여기서 되조인다.

    구현: Werkzeug 3.1+ per-request setter에 의존하지 않고 Content-Length를 직접 검사
    (버전 무관·fail-closed). declared length가 상한 초과면 즉시 413.
    setter가 있는 버전에선 추가로 스트리밍 상한도 조여 chunked/누락 헤더도 방어."""
    if request.method not in ('POST', 'PUT', 'PATCH'):
        return
    if request.path == '/api/stt/jobs':   # 회의록 업로드만 200MB 허용
        return
    if request.path == '/api/ext/soa/reviews/snapshot':
        cl = request.content_length
        if cl is not None and cl > _SOA_REVIEW_SNAPSHOT_MAX:
            abort(413)
        try:
            request.max_content_length = _SOA_REVIEW_SNAPSHOT_MAX
        except (AttributeError, TypeError):
            pass
        return
    cl = request.content_length
    if cl is not None and cl > _NON_STT_UPLOAD_MAX:
        abort(413)
    # 있으면 스트리밍 상한도 조임(Content-Length 누락/chunked 방어). 없으면 전역 200MB로 폴백.
    try:
        request.max_content_length = _NON_STT_UPLOAD_MAX
    except (AttributeError, TypeError):
        pass

# static(css/js) URL에 파일 수정시각을 ?v= 로 자동 부착 — 파일 변경 시 URL이 바뀌어
# 브라우저(특히 iOS Safari) 캐시를 강제 무효화. 템플릿 수정 불필요(모든 url_for('static') 적용).
@app.url_defaults
def _add_static_version(endpoint, values):
    if endpoint == 'static' and values.get('filename'):
        try:
            fp = os.path.join(app.static_folder, values['filename'])
            values['v'] = int(os.path.getmtime(fp))
        except OSError:
            app.logger.debug('add-static-version: static mtime miss', exc_info=True)



def init_db(drop=False):
    """schema + seed 실행, 기본 admin 계정 자동 생성.

    재실행 안전: 이미 데이터가 있어도 schema는 IF NOT EXISTS 라 무해.
    옛 priority 값(Critical/High/Low)이 남아있으면 새 분류로 자동 마이그레이션.
    """
    init_runtime()
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

        # 우리자산 전용 계정은 users 행은 재사용하되 업무 API 권한과는 분리한다.
        # 기존 운영 DB에는 CREATE TABLE IF NOT EXISTS가 컬럼을 보태지 못하므로 명시 migration.
        _user_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
        if 'app_scope' not in _user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN app_scope TEXT NOT NULL DEFAULT 'business'")
        migration_steps.run_family_asset_migrations(conn)
        print('  · 스키마 적용 완료')

        cal_cols = [r[1] for r in conn.execute('PRAGMA table_info(calendar_events)').fetchall()]
        if cal_cols and 'completed' not in cal_cols:
            conn.execute('ALTER TABLE calendar_events ADD COLUMN completed INTEGER NOT NULL DEFAULT 0')
            print('  · calendar_events.completed 컬럼 추가')
        if cal_cols and 'leave_type' not in cal_cols:
            conn.execute('ALTER TABLE calendar_events ADD COLUMN leave_type TEXT')
            print('  · calendar_events.leave_type 컬럼 추가')

        leave_allowance_cols = [
            r[1] for r in conn.execute(
                'PRAGMA table_info(calendar_leave_allowances)'
            ).fetchall()
        ]
        if leave_allowance_cols and 'manual_used' not in leave_allowance_cols:
            conn.execute(
                'ALTER TABLE calendar_leave_allowances '
                'ADD COLUMN manual_used REAL NOT NULL DEFAULT 0'
            )
            print('  · calendar_leave_allowances.manual_used 컬럼 추가')

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
                summary       TEXT,
                params        TEXT,
                progress      TEXT
            )
        """)
        try:                                            # 마이그: 기존 DB에 params 추가(선박별 SOA 검증 버튼)
            _cols = [r[1] for r in conn.execute("PRAGMA table_info(automation_run)").fetchall()]
            if 'params' not in _cols:
                conn.execute("ALTER TABLE automation_run ADD COLUMN params TEXT")
            if 'progress' not in _cols:                 # 러너 중간보고(굳음 vs 도는중 구분)
                conn.execute("ALTER TABLE automation_run ADD COLUMN progress TEXT")
        except Exception:
            app.logger.debug('automation_run params/progress 마이그 skip', exc_info=True)

        # Daily 사이드바 선박 커스텀 순서 (유저별, 드래그앤드롭 저장)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_vessel_order (
                user_id     INTEGER PRIMARY KEY,
                order_json  TEXT NOT NULL DEFAULT '[]',
                updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)

        # Fleet Map manual Next Port override. Snapshot tracks the automatic source identity
        # so a changed upstream next-port automatically invalidates stale manual entries.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fleet_next_port_override (
                vessel_key     TEXT PRIMARY KEY,
                vessel_name    TEXT NOT NULL,
                manual_label   TEXT NOT NULL,
                manual_code    TEXT,
                manual_lat     REAL NOT NULL,
                manual_lng     REAL NOT NULL,
                auto_snapshot  TEXT NOT NULL,
                active         INTEGER NOT NULL DEFAULT 1,
                inactivated_at TEXT,
                inactivated_reason TEXT,
                created_by     TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_by     TEXT,
                updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
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
        # aor_cd is the SVMS document identity. Older deployments only performed a
        # SELECT-before-INSERT check, so overlapping prep runs could both insert an
        # active row. Normalize legacy keys, keep the most advanced active row, and
        # make the invariant database-enforced for future concurrent requests.
        # 상태군의 정본은 `_AOR_ACTIVE_STATUSES` 하나뿐이다. 예전엔 여기 리터럴을 따로 적어둬서
        # 둘이 어긋나면 index predicate 와 dedup 정리 범위가 조용히 갈라질 수 있었다.
        _aor_active = _aor_status_list_sql(_AOR_ACTIVE_STATUSES)
        conn.execute(f"""
            UPDATE aor_draft AS loser
               SET status='duplicate',
                   submit_result=COALESCE(
                     submit_result,
                     '자동 중복 정리: 동일 SVMS AOR의 진행 상태가 더 높은 행을 보존함'
                   )
            WHERE loser.status IN ({_aor_active})
              AND EXISTS (
                SELECT 1 FROM aor_draft AS winner
                WHERE upper(trim(winner.aor_cd)) = upper(trim(loser.aor_cd))
                  AND winner.status IN ({_aor_active})
                  AND (
                    CASE winner.status
                      WHEN 'submitted' THEN 70 WHEN 'submitting' THEN 60
                      WHEN 'approved' THEN 50 WHEN 'reject_submitting' THEN 40
                      WHEN 'rejecting' THEN 30 WHEN 'hold' THEN 20 ELSE 10 END
                    >
                    CASE loser.status
                      WHEN 'submitted' THEN 70 WHEN 'submitting' THEN 60
                      WHEN 'approved' THEN 50 WHEN 'reject_submitting' THEN 40
                      WHEN 'rejecting' THEN 30 WHEN 'hold' THEN 20 ELSE 10 END
                    OR (
                      CASE winner.status
                        WHEN 'submitted' THEN 70 WHEN 'submitting' THEN 60
                        WHEN 'approved' THEN 50 WHEN 'reject_submitting' THEN 40
                        WHEN 'rejecting' THEN 30 WHEN 'hold' THEN 20 ELSE 10 END
                      =
                      CASE loser.status
                        WHEN 'submitted' THEN 70 WHEN 'submitting' THEN 60
                        WHEN 'approved' THEN 50 WHEN 'reject_submitting' THEN 40
                        WHEN 'rejecting' THEN 30 WHEN 'hold' THEN 20 ELSE 10 END
                      AND winner.id > loser.id
                    )
                  )
              )
        """)
        # 유일성은 **canonical key 기준**으로 DB 가 강제한다(아래 정규화는 관례일 뿐이라
        # 정규화를 잊은 writer 가 하나만 생겨도 뚫린다). 바로 위 dedup 정리가 끝난 직후라
        # canonical 중복 활성행은 없다 — 교체가 실패할 수 없는 유일한 지점이다.
        # 🔴 index 교체를 **정규화보다 먼저** 한다(2026-07-30). 옛 raw-컬럼 index 가 남은 DB 에서
        #    먼저 정규화하면, 대소문자만 다르던 두 행의 raw key 가 같아지면서 그 옛 index 를
        #    때린다(UNIQUE constraint failed: aor_draft.aor_cd → 부팅 자체가 죽음).
        #    'submitted' 가 활성군에서 빠진 뒤로는 위 dedup 정리가 그런 쌍을 더는 안 걷어내므로
        #    (이력행 + 새 활성행은 정상 상태다) 실제로 재현되는 경로가 됐다.
        #    표현식 index 를 먼저 깔면 정규화는 canonical key 를 바꾸지 않아 충돌이 불가능하다.
        _aor_active_index_install(conn)
        conn.execute("UPDATE aor_draft SET aor_cd=upper(trim(aor_cd)) "
                     "WHERE aor_cd<>upper(trim(aor_cd))")
        # absorbing 상태 이탈 금지 — 러너 skip 안전성의 근거를 DB 층에 고정한다.
        # (정의·검증은 _aor_absorbing_trigger_sql / _aor_absorbing_trigger_ok)
        _aor_absorbing_trigger_install(conn)

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
                attach_files  TEXT,                                -- SVMS 첨부(인보이스·증빙) 파일명 JSON 배열
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
        # 기존 DB 마이그레이션 — SVMS 첨부 파일명 목록(preview 인덱스는 디스크가 정본)
        _fr_cols = [r[1] for r in conn.execute('PRAGMA table_info(fundreq_draft)').fetchall()]
        if 'attach_files' not in _fr_cols:
            conn.execute('ALTER TABLE fundreq_draft ADD COLUMN attach_files TEXT')

        # 인보이스 자동컨펌(SVMS Invoice Confirm) 2단게이트 draft 큐 (prep 엔진 ingest → 사람이 /invoice 탭서 opt-out 승인/리젝 결정 → 맥 invoice_confirm 러너가 SVMS 교정·컨펌)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoice_draft (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                inv_cd        TEXT NOT NULL,                       -- SVMS 인보이스코드(dedup 키)
                vsl_cd        TEXT,
                vsl_nm        TEXT,
                vndr_cd       TEXT,
                vndr_nm       TEXT,
                amt           REAL,                                -- 송장 금액
                cur_cd        TEXT,
                vat           REAL,
                inv_no        TEXT,                                -- SVMS 입력 송장번호
                inv_dt        TEXT,                                -- SVMS 입력 송장일자
                cur_sup       TEXT,                                -- 현재 SVMS SUP(교정 전)
                cur_pic       TEXT,                                -- 현재 SVMS PIC(교정 전)
                cur_pay_dt    TEXT,                                -- 현재 SVMS Remit/지급일(교정 전)
                set_pic       TEXT,                                -- 자동화가 넣을 PIC(박은미)
                set_sup       TEXT,                                -- 자동화가 넣을 SUP(손유석)
                set_pay_dt    TEXT,                                -- 자동화가 넣을 Remit(동월말)
                exp_cd        TEXT,                                -- 라인 expense code
                exp_nm        TEXT,                                -- 라인 expense 명
                exp_conf      REAL,                                -- expense 분류 신뢰도
                exp_reason    TEXT,                                -- expense 분류 근거
                subject       TEXT,                                -- 라인 적요
                inv_no_match  INTEGER,                             -- PDF 대조: 송장번호 일치 0/1/NULL
                amt_match     INTEGER,                             -- PDF 대조: 금액 일치 0/1/NULL
                date_match    INTEGER,                             -- PDF 대조: 날짜 일치 0/1/NULL
                match_src     TEXT,                                -- 3자 동시검출된 PDF 파일명
                had_lines     INTEGER,                             -- 기존 라인 존재 여부
                attachments   TEXT,                                -- 첨부 파일명 JSON
                flags         TEXT,                                -- 플래그 JSON 배열
                gate          TEXT,                                -- PASS/HOLD (PASS=디폴트 자동상신 대상)
                raw_card      TEXT,                                -- 카드 전체 JSON(컨펌때 재조회 키만 사용)
                status        TEXT NOT NULL DEFAULT 'pending',     -- pending/approved/submitting/submitted/rejecting/rejected/failed/reject_failed
                reject_reason TEXT,
                decided_at    TEXT,
                decided_by    TEXT,
                done_at       TEXT,
                result        TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_draft_status ON invoice_draft(status)")

        # 기국 인보이스 신규등록(Case 2) 큐 — /liscr 탭에 PDF 업로드 → 맥 러너가 파싱/생성.
        #   TRMT 서버는 SVMS 자격증명을 갖지 않는다(시크릿은 맥 로컬에만 둔다는 방침).
        #   따라서 서버는 큐·화면만 맡고 SVMS 쓰기는 전부 맥 러너가 한다. 기존 인보이스
        #   자동컨펌(invoice_draft)과는 별개 테이블이다 — 그쪽은 "이미 있는 인보이스"를 다루고
        #   이쪽은 "없는 인보이스를 만든다". 생성이 끝나면 SVMS STATUS='S'가 되어 기존
        #   ingest 가 그 인보이스를 집어가므로, 두 파이프라인은 코드가 아니라 SVMS 상태로 이어진다.
        #   status: queued → parsing → parsed|hold → approved → creating → created|failed (rejected=사람이 취소)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS liscr_job (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT NOT NULL,                      -- 업로드 원본 파일명(표시용)
                sha256        TEXT,                               -- PDF 내용 해시(동일파일 재업로드 감지)
                status        TEXT NOT NULL DEFAULT 'queued',
                claim_token   TEXT,                               -- 러너 claim CAS 토큰
                claimed_at    TEXT,
                profile       TEXT NOT NULL DEFAULT 'liscr',      -- 등록 유형(liscr=기국 고정값 / generic=직접지정)
                gate          TEXT,                               -- READY/FIX/HOLD (파싱 결과)
                reasons       TEXT,                               -- 사유 JSON 배열
                hard_json     TEXT,                               -- 그중 hard 사유(사람이 채워도 승인 불가)
                vsl_cd        TEXT,
                vsl_nm        TEXT,
                inv_no        TEXT,
                inv_dt        TEXT,
                cur_cd        TEXT,
                amt           REAL,
                pay_dt        TEXT,
                vndr_cd       TEXT,                               -- Vendor 코드(기국=V25081 고정, 기타=사람이 지정)
                vndr_nm       TEXT,
                exp_cd        TEXT,
                exp_nm        TEXT,
                subject       TEXT,                               -- 라인 적요(사람이 수정 가능)
                sup_user_id   TEXT,
                sup_user_nm   TEXT,
                inv_user_id   TEXT,                               -- Invoice PIC
                oversea_tp    TEXT,
                header_json   TEXT,                               -- 러너가 SVMS로 보낼 헤더 전체
                lines_json    TEXT,                               -- 라인 전체
                parsed_json   TEXT,                               -- PDF 파서 원본 결과(감사용)
                edited_json   TEXT,                               -- 사람이 승인 때 고친 필드만(감사용)
                inv_cd        TEXT,                               -- 생성된 SVMS 인보이스코드
                error         TEXT,
                decided_at    TEXT,
                decided_by    TEXT,
                done_at       TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_liscr_job_status ON liscr_job(status)")

        # SVMS 마스터 스냅샷 — "기타 인보이스" 를 올릴 때 화면에서 Vendor/Expense/통화/선박을
        # 고르려면 목록이 필요한데, 이 서버는 SVMS 를 부를 수 없다(자격증명은 맥에만 있다).
        # 그래서 맥 러너가 떠서 밀어주고(/api/ext/liscr/master), 화면은 이 표만 본다.
        # 🔴 벤더 전체(94,384종/127MB)는 담지 않는다 — 최근 1년 실사용분(563종)만.
        #    목록에 없으면 화면에서 코드를 직접 치고, 진짜인지는 러너가 SVMS 에 물어 확인한다.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS liscr_master (
                kind        TEXT PRIMARY KEY,                     -- vessels/expenses/vendors/currencies
                payload     TEXT NOT NULL,                        -- [{cd,nm,...}] JSON 배열
                n           INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)

        # 자동화 헬스 보드(하트비트) — 맥측 health_push.py 가 각 러너 신선도를 주기 POST.
        #   러너당 최근 30행만 유지(prune). 읽기=/api/automation/health, 페이지=/health(admin).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS automation_health (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                runner_key  TEXT NOT NULL,                        -- 러너 기술키(예: fundreq-auto)
                status      TEXT NOT NULL,                        -- ok/warn/fail/unknown
                note        TEXT,                                 -- 한글 상태메모(예: 32시간 전 성공)
                ran_at      TEXT,                                 -- 마지막 성공/관측 실행 시각(ISO)
                next_run    TEXT,                                 -- 다음 예정 실행(있으면)
                reported_at TEXT NOT NULL                         -- 이 관측을 적재한 시각(ISO)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_health_key "
                     "ON automation_health(runner_key, reported_at)")

        # SVMS expense code 마스터(PKG_CO.SP_GET_EXP 357개) — 인보이스 라인 EXP_CD 편집 검색용. 맥이 ext로 적재.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expense_code (
                code     TEXT PRIMARY KEY,    -- EXP_CD
                name     TEXT,                -- 국문 명칭
                name_en  TEXT,                -- 영문(EXP_NM1)
                grp      TEXT,                -- GRP_CD
                updated_at TEXT
            )
        """)

        # 전자결재(jeonja) 검증 결과 + 자동상신 제외(보류) 큐
        #   verify(jeonja_review --post) 가 현재 상신대기(P) 전수 검토결과를 ref 단위로 적재 →
        #   사람이 /automation 허브서 항목별 '자동상신 제외' 체크 → live(jeonja_approve) 가 excluded=1 ref 를 skip.
        #   검증 다시 돌려도 보류(excluded) 표시는 ref 기준으로 보존.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jeonja_review_item (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ref         TEXT NOT NULL UNIQUE,                 -- 전자결재 REF_NO (dedup·exclude 키)
                vsl_cd      TEXT,
                subj        TEXT,
                fund        TEXT,                                 -- Fund 구분(AOR/Pre-del/OPEX 등)
                cost        REAL,                                 -- SVMS Cost
                dn          TEXT,                                 -- 첨부 DN/인보이스 판독(금액+통화)
                bucket      TEXT NOT NULL,                        -- pass/costslip/mismatch/escalate/flag/already
                why         TEXT,                                 -- 비-pass 사유
                excluded    INTEGER NOT NULL DEFAULT 0,           -- 1=사용자 보류(검증통과여도 자동상신 제외)
                run_id      TEXT,                                 -- 적재한 verify run_id
                reviewed_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)

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
                stock       TEXT DEFAULT 'service',            -- 수리 공급: unselected/vendor/owner (service=기존 vendor 호환)
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
            app.logger.debug('init-db migration skip', exc_info=True)

        # ── Dock Procurement(입거 발주현황 트래커) ──
        #   입거선박 INDEX 엑셀 업로드 → 라인 큐 자동생성(증분/중복제외).
        #   4단계 체크박스(견적작성→벤더제출→벤더컨펌→발주완료)로 진행추적. dedup 키=(vsl_nm, req_no).
        #   R/S/ST=SVMS 연동대상(Phase 2 svms_pushed), P=페인트/SY=조선소=메일견적(SVMS 무관).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dock_procure_vessel (
                vsl_nm     TEXT PRIMARY KEY,                  -- INDEX VESSEL NAME(그룹 키)
                vsl_cd     TEXT,                              -- SVMS 코드(best-effort lookup, Phase 2)
                owner_co   TEXT,
                vtype      TEXT,                              -- TYPE OF VESSEL
                survey     TEXT,                              -- KIND OF SURVEY
                shipyard   TEXT,
                due_date   TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        # 직접작성 수리신청서는 일반수리도 dock_procure의 견적·상신 엔진을 사용한다.
        # 기능 도입 전에 생성된 신청서까지 선박 선택기에서 고립되지 않게 기동 시 멱등 backfill.
        conn.execute("""
            INSERT OR IGNORE INTO dock_procure_vessel(vsl_nm, vsl_cd, updated_at)
            SELECT vsl_nm, MAX(NULLIF(TRIM(vsl_cd),'')), datetime('now','localtime')
              FROM repair_request
             WHERE COALESCE(vsl_nm,'')<>''
             GROUP BY vsl_nm
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dock_procure (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                vsl_nm       TEXT NOT NULL,                   -- 그룹 키(INDEX VESSEL NAME)
                vsl_cd       TEXT,
                req_no       TEXT NOT NULL,                   -- R1/S1/ST1/P1/SY1 (dedup 키)
                cat_code     TEXT,                            -- R/S/ST/P/SY
                category     TEXT,                            -- SHORE REPAIR/SPARE/STORE/PAINT/SHIPYARD
                equipment    TEXT,
                subject      TEXT,
                prepared_by  TEXT,                            -- OWNER/MANAGER
                source       TEXT,                            -- SVMS / MAIL
                content_hash TEXT,                            -- equipment+subject 해시(내용변경 감지)
                stg_quote    INTEGER NOT NULL DEFAULT 0,      -- 1단계: 견적서 작성
                stg_vendor   INTEGER NOT NULL DEFAULT 0,      -- 2단계: 벤더 제출
                stg_confirm  INTEGER NOT NULL DEFAULT 0,      -- 3단계: 벤더 컨펌 + 결재 상신
                stg_order    INTEGER NOT NULL DEFAULT 0,      -- 4단계: 발주 완료
                stg_manual   INTEGER NOT NULL DEFAULT 0,      -- 사람이 마지막으로 확정한 단계 rank(0=없음). sync 가 이 아래로 못 내림
                remark       TEXT,
                sort_no      INTEGER,                         -- INDEX No.(정렬용)
                rev_batch    TEXT,                            -- 추가된 업로드 배치 id
                svms_pushed  INTEGER NOT NULL DEFAULT 0,      -- Phase 2: SVMS 청구서 생성됨
                svms_req_no  TEXT,                            -- Phase 2: SVMS Inq No/REQ_NO(역추적 핸들)
                svms_status  TEXT,                            -- Phase 2: 마지막 관측 SVMS Status
                svms_submit  TEXT,                            -- Phase 2: 견적제출수/의뢰수 "n/m"
                svms_synced_at TEXT,                          -- Phase 2: 마지막 동기화 시각
                quote_amt    REAL,                            -- 발주업체 확정 견적금액(SVMS Spare/Shore 연동용, 수정가능)
                quote_cur    TEXT DEFAULT 'USD',              -- 견적 통화
                quote_src    TEXT DEFAULT 'auto',             -- auto=SVMS 발주금액 자동입력 / manual=사용자수정 잠금(폴러 안 덮음)
                vendor       TEXT,                            -- 페인트(P) 수동 업체명 → SVMS Dock Paint(02) VNDR_NM
                sub_quotes   TEXT,                            -- 벤더 '제출견적' 스냅샷 JSON [{cd,nm,amt,usd,cur,att,st,best}] — 표시전용(발주금액 quote_amt 과 별개). cd=VNDR_CD(Phase③ 상신 SELETED_VDR 소스)
                att_files    TEXT,                            -- 벤더 견적서 첨부 목록 JSON [{nm,kb,vndr,vnm,dt}] — 배열 위치(idx)가 preview cache 파일명
                ord_vendors  TEXT,                            -- 분할발주 업체별 발주서 스냅샷 JSON [{odr_no,nm,cd,st,amt,cur,ordered}] — 자재/스토어를 업체 2곳으로 나눠 발주한 건. `quote_amt`(단일칸)과 별개이며 통화 혼재 시 합산 불가라 합치지 않는다
                created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(vsl_nm, req_no)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dock_procure_vsl ON dock_procure(vsl_nm)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dock_yard (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                vsl_nm     TEXT NOT NULL,                   -- 그룹 키
                vsl_cd     TEXT,
                category   TEXT NOT NULL,                   -- General/Paint/Steel/Deck/Engine/Electric/Discount
                amount     REAL,
                cur        TEXT DEFAULT 'USD',
                remark     TEXT,
                src        TEXT DEFAULT 'auto',             -- auto(견적파싱) / manual(사용자수정 잠금)
                yard_name  TEXT,                            -- 조선소명(프로파일)
                sort_no    INTEGER,                         -- 7카테고리 표시순서
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(vsl_nm, category)                    -- 선박당 카테고리 1행(7행)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dock_yard_vsl ON dock_yard(vsl_nm)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS yard_vendor (            -- SVMS 조선소 벤더마스터 캐시(맥이 pull→적재)
                vndr_cd     TEXT PRIMARY KEY,                   -- PKG_CM_VNDR VNDR_CD (dock 봉투 DR_CD/VNDR_CD 소스)
                vndr_nm     TEXT,                               -- 국문명
                vndr_nm_eng TEXT,                               -- 영문명
                updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        # ===== Phase ③ 수리 견적 상신 큐 (fundreq_draft 패턴 복제) =====
        #  🔴 이 큐의 1행 = **실제 SVMS 발주벤더 확정 + 결재상신**을 맥 워커에 지시하는 것이다.
        #     생성 경로는 세션 로그인한 사람이 누르는 버튼 **하나뿐**이고(ext 생성 라우트 없음),
        #     `pending` 단계가 없는 이유도 그것 — 초안을 사람이 만들므로 곧바로 approved 로 들어온다.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dock_submit_draft (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                rid           INTEGER NOT NULL,                 -- dock_procure.id
                vsl_nm        TEXT,
                vsl_cd        TEXT,
                req_no        TEXT,                             -- INDEX 요청번호(표시용)
                rep_cd        TEXT NOT NULL,                    -- SVMS REP_CD (= dock_procure.svms_req_no)
                vndr_cd       TEXT NOT NULL,                    -- 봉투 SELETED_VDR = sub_quotes[].cd
                vndr_nm       TEXT,
                amt           REAL,                             -- 승인 시점 제출견적액(감사용 스냅샷)
                cur           TEXT,
                app_no        TEXT NOT NULL,                    -- 결재라인 프리셋(SP_GET_USER_APP APP_NO)
                app_nm        TEXT,
                envelope_json TEXT,                             -- 모달에 보여준 초안 스냅샷(사람이 승인한 내용 그대로)
                status        TEXT NOT NULL DEFAULT 'approved',  -- approved/submitting/submitted/failed/canceled
                decided_at    TEXT,
                decided_by    TEXT,                             -- 버튼 누른 사람(세션) — 비면 워커가 claim 안 함
                done_at       TEXT,                             -- claim 시각 → 결과 시각으로 덮임(fundreq 관행)
                result        TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dock_submit_status ON dock_submit_draft(status)")
        # 같은 수리건을 두 번 큐잉하면 이중 상신 — 부분 유니크로 DB 층에 못박는다(앱 검사와 이중방어).
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_dock_submit_active "
                     "ON dock_submit_draft(rep_cd) WHERE status IN ('approved','submitting')")
        # 🔴 실패기록 소거(2026-08-04 형 지시) — **행을 지우지 않고** 화면에서만 내린다.
        #    `wrote` = 그 실패가 SVMS write 를 실제로 던진 뒤인지(1) 던지기 전인지(0). 워커가 보낸다.
        #    NULL = 구 워커가 보낸 행 → 사유 문자열로 보수 판정(`_dock_submit_dismissable`).
        _sbm_cols = [r[1] for r in conn.execute('PRAGMA table_info(dock_submit_draft)').fetchall()]
        for _c, _t in (('dismissed_at', 'TEXT'), ('wrote', 'INTEGER')):
            if _c not in _sbm_cols:
                conn.execute('ALTER TABLE dock_submit_draft ADD COLUMN %s %s' % (_c, _t))
        # ===== 수리 견적요청 큐 (견적작성 → 벤더제출) =====
        #  🔴 이 큐의 1행 = 맥 워커의 **SVMS write 2회**(Confirm `SP_SET_REP`+STATUS='RC' → 벤더제출
        #     `SP_SET_REP_DTL`). 봉투 규격 정본 = docs/svms/repair-inquiry-envelope.md.
        #     Phase ③(dock_submit_draft)와 **다른 단계**이므로 표를 섞지 않는다 — 같은 rep_cd 가
        #     두 큐에 동시에 있을 수는 있지만(견적요청→나중에 상신) 각자의 부분유니크로 이중발사만 막는다.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dock_inquiry_draft (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                rid           INTEGER NOT NULL,                 -- dock_procure.id
                vsl_nm        TEXT,
                vsl_cd        TEXT,
                req_no        TEXT,                             -- INDEX 요청번호(표시용)
                rep_cd        TEXT NOT NULL,                    -- SVMS 키. 수리=REP_CD(dock_procure.svms_req_no)
                                                                --  구매=REQ_NO(dock_procure.svms_pc_req_no)
                doc_type      TEXT NOT NULL DEFAULT 'MARP',     -- MARP=수리 / PCRQ=구매·자재. 워커가 봉투를 고르는 값
                vndr_json     TEXT NOT NULL,                    -- 선택 업체 [{cd,nm}] (≤5). 🔴 SVMS 봉투용
                                                                --  벤더행이 아니다 — 봉투(CURSOR.P_IC_VNDR)는
                                                                --  맥 워커가 전송 직전 SP_GET_VNDR 재조회로
                                                                --  만든다(브라우저가 준 값 금지). nm 은 표시용.
                vndr_names    TEXT,                             -- 표시용 요약 'A, B'
                envelope_json TEXT,                             -- 사람이 승인한 초안 스냅샷(그대로 보관)
                status        TEXT NOT NULL DEFAULT 'approved',  -- approved/submitting/submitted/failed/canceled
                                                                --  /recalled = SVMS 에서 회수돼 무효화된 이력
                                                                --  (2026-08-03). 어느 표시 버킷에도 안 잡혀
                                                                --  카드에서 '견적요청됨' 이 사라진다. 행은 보존.
                decided_at    TEXT,
                decided_by    TEXT,                             -- 버튼 누른 사람(세션) — 비면 워커가 claim 안 함
                done_at       TEXT,
                result        TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dock_inquiry_status ON dock_inquiry_draft(status)")
        # 🔴 유니크는 `rep_cd` **단독**으로 유지한다. `(doc_type, rep_cd)` 로 넓히면 같은 문서번호가
        #    두 종류로 동시에 큐잉될 수 있어 **느슨해진다**. 수리 REP_CD(…ME…)와 구매 REQ_NO(…ES…/…EC…)는
        #    네임스페이스가 겹치지 않으므로(실측) 단독 유니크가 더 안전한 쪽으로 실패한다.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_dock_inquiry_active "
                     "ON dock_inquiry_draft(rep_cd) WHERE status IN ('approved','submitting')")
        try:                                            # 기존 배포 DB 마이그레이션 — 구매 견적요청(PCRQ)
            _diq = [r[1] for r in conn.execute("PRAGMA table_info(dock_inquiry_draft)").fetchall()]
            if 'doc_type' not in _diq:                  # 기존 행은 전부 수리였으므로 DEFAULT 'MARP' 가 맞다
                conn.execute("ALTER TABLE dock_inquiry_draft ADD COLUMN "
                             "doc_type TEXT NOT NULL DEFAULT 'MARP'")
        except Exception:
            app.logger.debug('init-db dock_inquiry_draft migration skip', exc_info=True)
        # 결재라인 캐시 — 서버는 SVMS 에 못 붙으므로 맥이 밀어준다. **드롭다운 표시 전용**이고
        # 상신 봉투의 `CURSOR.P_IC_APP` 는 워커가 그 시점에 SP_GET_USER_APP_D 를 다시 읽어서 만든다
        # (캐시가 stale 하면 옛 결재자로 상신될 수 있으므로 캐시를 봉투 소스로 쓰지 않는다).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS svms_app_line (
                app_no      TEXT PRIMARY KEY,                   -- SP_GET_USER_APP APP_NO ('0002' 등)
                app_nm      TEXT,                               -- 라인 이름
                user_id     TEXT,                               -- 소유 계정(SS0094)
                approvers   TEXT,                               -- 표시용 요약 JSON [{seq,id,nm}]
                updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        try:                                            # 기존 배포 DB 마이그레이션(Phase 2 컬럼)
            _dpc = [r[1] for r in conn.execute("PRAGMA table_info(dock_procure)").fetchall()]
            if 'svms_status' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN svms_status TEXT")
            if 'svms_synced_at' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN svms_synced_at TEXT")
            if 'svms_submit' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN svms_submit TEXT")
            if 'quote_amt' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN quote_amt REAL")
            if 'quote_cur' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN quote_cur TEXT DEFAULT 'USD'")
            if 'quote_src' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN quote_src TEXT DEFAULT 'auto'")
                # 기존에 수동입력된 금액은 잠가서 폴러가 안 덮게
                conn.execute("UPDATE dock_procure SET quote_src='manual' WHERE quote_amt IS NOT NULL")
            if 'vendor' not in _dpc:
                conn.execute("ALTER TABLE dock_procure ADD COLUMN vendor TEXT")   # 페인트(P) 수동 업체명 → SVMS Dock Paint(02) VNDR_NM
            if 'sub_quotes' not in _dpc:                      # 벤더 제출견적 스냅샷(표시전용) — 발주금액과 혼동 금지
                conn.execute("ALTER TABLE dock_procure ADD COLUMN sub_quotes TEXT")
            if 'att_files' not in _dpc:                       # 벤더 견적서 첨부 목록(파일명/KB/업체) — 파일 자체는 preview cache
                conn.execute("ALTER TABLE dock_procure ADD COLUMN att_files TEXT")
            if 'ord_vendors' not in _dpc:                     # 분할발주 업체별 발주서 스냅샷(업체 2곳 나눠발주 표시용)
                conn.execute("ALTER TABLE dock_procure ADD COLUMN ord_vendors TEXT")
            if 'svms_pc_req_no' not in _dpc:
                # 🔴 구매(S/ST) 견적요청 키 = SVMS **REQ_NO**. `svms_req_no` 를 재사용하면 안 된다 —
                #    구매행의 `svms_req_no` 에는 견적요청 **후** 발급되는 INQ_NO 가 들어가고(실측
                #    2026-08-03: BGBBES2607A1 → 'BGBBES2607A11'), Phase ③ 상신·제출견적·첨부가 전부
                #    그 값을 INQ_NO 로 읽는다. 두 값을 한 칸에 섞으면 상신이 깨진다.
                #    채우는 곳 = 폴러 sync (`inq_alt`=REQ_NO 를 매 sync 마다 이미 보내므로 조회 추가 0).
                conn.execute("ALTER TABLE dock_procure ADD COLUMN svms_pc_req_no TEXT")
            if 'stg_confirm' not in _dpc:                     # 벤더 선택 컨펌 + 결재 상신(발주완료 전단계)
                conn.execute("ALTER TABLE dock_procure ADD COLUMN stg_confirm INTEGER NOT NULL DEFAULT 0")
            if 'stg_manual' not in _dpc:
                # 🔴 사람이 확정한 단계 floor(2026-08-07 형 지시). SVMS 밖에서 발주한 건(메일 발주 등)은
                #    SVMS 라벨이 영원히 '벤더제출' 이라, 사람이 켠 '발주완료' 를 sync 가 매시간 되돌렸다.
                #    backfill 은 하지 않는다 — 기존 행은 사람이 켠 것과 sync 가 켠 것을 구분할 수 없고,
                #    일괄로 floor 를 세우면 SVMS 의 정당한 되돌림(회수·반려)까지 영구히 막힌다.
                conn.execute("ALTER TABLE dock_procure ADD COLUMN stg_manual INTEGER NOT NULL DEFAULT 0")
            # 매 부팅 멱등 보정: ALTER 직후 crash 나도 다음 부팅에 backfill이 다시 돈다.
            # 기존 발주완료/Submit/결재진행 행은 누적 앞단계까지 함께 맞춰 비정상 (0,0,1,0)을 만들지 않는다.
            conn.execute("UPDATE dock_procure SET stg_quote=1, stg_vendor=1, stg_confirm=1 "
                         "WHERE stg_order=1 OR UPPER(TRIM(COALESCE(svms_status,''))) "
                         "IN ('SUBMIT','APPROVAL(PROCSSING)')")
            _dpv = [r[1] for r in conn.execute("PRAGMA table_info(dock_procure_vessel)").fetchall()]
            if 'shipyard_vndr_cd' not in _dpv:                # 선택된 조선소 벤더(SVMS) → dock 봉투 DR_CD/VNDR_CD
                conn.execute("ALTER TABLE dock_procure_vessel ADD COLUMN shipyard_vndr_cd TEXT")
            if 'shipyard_vndr_nm' not in _dpv:
                conn.execute("ALTER TABLE dock_procure_vessel ADD COLUMN shipyard_vndr_nm TEXT")
            if 'dk_cd' not in _dpv:                        # SVMS 입거수리 Dock No(푸싱 대상 draft). 설정된 선박만 자동푸싱 opt-in
                conn.execute("ALTER TABLE dock_procure_vessel ADD COLUMN dk_cd TEXT")
            if 'origin' not in _dpv:
                # 🔴 엔트리 출처. 'repair' = 일반수리 신청서가 MARP 견적·상신 엔진을 쓰기 위해
                #    자동으로 만든 shim 이고 **입거(Dock) 선박이 아니다**. Dock 발주현황 탭은
                #    이 엔트리를 목록에서 제외한다(2026-08-15 형 지적: "일반 수리신청서는
                #    dock발주 탭에서 하는 게 아닌데 이렇게 됨"). NULL = 기존 Dock 엔트리.
                conn.execute("ALTER TABLE dock_procure_vessel ADD COLUMN origin TEXT")
                # 태깅 조건은 **Dock 근거가 하나도 없을 때만**. 하나라도 있으면 진짜 입거선박이고,
                # 잘못 태깅하면 아래 필터가 그 배를 Dock 탭에서 지워 버린다(올마이트 지적 반영):
                #   dock 메타(vtype/survey/shipyard/due_date) · 조선소 견적(dock_yard) ·
                #   Dock No(dk_cd) · 조선소 벤더 · 수리연결이 아닌 dock_procure 라인.
                conn.execute("""
                    UPDATE dock_procure_vessel SET origin='repair'
                     WHERE vsl_nm IN (SELECT DISTINCT vsl_nm FROM repair_request
                                       WHERE COALESCE(vsl_nm,'')<>'')
                       AND COALESCE(vtype,'')='' AND COALESCE(survey,'')=''
                       AND COALESCE(shipyard,'')='' AND COALESCE(due_date,'')=''
                       AND COALESCE(dk_cd,'')='' AND COALESCE(shipyard_vndr_cd,'')=''
                       AND COALESCE(shipyard_vndr_nm,'')='' AND COALESCE(owner_co,'')=''
                       AND NOT EXISTS (SELECT 1 FROM dock_yard y
                                        WHERE y.vsl_nm=dock_procure_vessel.vsl_nm)
                       AND NOT EXISTS (SELECT 1 FROM dock_procure p
                                        WHERE p.vsl_nm=dock_procure_vessel.vsl_nm
                                          AND NOT EXISTS (SELECT 1 FROM repair_request rr
                                                           WHERE rr.dock_rid=p.id))
                """)
            # ⛔ 이미 갈라진 중복 행(예: 'Belgium B' vs 'BELGIUM B')을 **자동 병합하지 않는다.**
            #    `vsl_nm` 은 11개 테이블(dock_procure/dock_yard/dock_submit_draft/dock_inquiry_draft/
            #    reqgen_draft/aor_draft/invoice_draft/fundreq_draft/soa_review_case/repair_request/
            #    dock_procure_vessel)이 문자열로 참조하는 그룹 키라, 이름 재매핑은 그 전부를 UNIQUE
            #    충돌 없이 옮겨야 하는 파괴적 작업이다. 매 부팅 heuristic 으로 돌릴 물건이 아니다
            #    (2026-08-15 올마이트 지적: 같은 코드의 두 shim 이 서로를 지우는 순환도 성립).
            #    화면 노출은 아래 origin 태깅 + Dock 탭 필터로 해소되고, 실제 병합이 필요하면
            #    사람이 승인한 일회성 마이그레이션으로 한다.
        except Exception:
            app.logger.debug('init-db migration skip', exc_info=True)

        # Ship-Issue Wiki — 선박별 이슈 스레드 지식노트 검토/승격 큐 (데쿠 ship-wiki 파이프라인 미러)
        #   맥(push_cards.py)이 pending/<slug>/*.md(Tier2 사람판단 대기) + wiki/<slug>/*.md(auto/confirmed)
        #   를 /api/ext/shipwiki/push 로 적재 → 사람이 /shipwiki 탭서 승격/병합/리젝/신뢰도승격 결정 →
        #   맥(apply_decisions.py)이 decided 카드를 pull → promote.py 로 wiki/ 파일 materialize → result POST.
        #   가드레일: 확정(재명명·병합·연결)은 100% 사람. 자동적재물(auto)은 답변근거 금지(라벨 격리).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shipwiki_card (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,                     -- 선박 slug (indonesia-prosperity)
                ship_nm       TEXT,                              -- 표시용 선명
                fname         TEXT NOT NULL,                     -- 원본 basename(.md 제외) — dedup 키(slug+fname)
                tier          TEXT NOT NULL,                     -- pending(사람판단대기) / auto(자동·미검증) / confirmed(확정)
                title         TEXT,                              -- 현재 제목
                category      TEXT,                              -- DEFECT/AOR/VETTING/NOTICE/INQUIRY/DOCK/OTHER
                confidence    TEXT,                              -- low/medium/high
                llm_conf      INTEGER,                           -- librarian Haiku 신뢰도
                multi         INTEGER NOT NULL DEFAULT 0,        -- multiple_issues_suspected(쪼갤 후보)
                msg_count     INTEGER,
                needs_human   TEXT,                              -- json
                judgment      TEXT,                              -- [감독판단] 제안/현재 본문
                evidence      TEXT,                              -- [원문근거] 요약초안(읽기)
                raw_links     TEXT,                              -- raw 링크 라인(개행구분)
                source_msgids TEXT,                              -- json
                equipment     TEXT,                              -- json
                vendors       TEXT,                              -- json
                ref_numbers   TEXT,                              -- json
                date_first    TEXT,
                date_last     TEXT,
                -- 사람 결정 --
                decision      TEXT,                              -- null/promote/reject/split_flag/upgrade
                merge_group   TEXT,                              -- 병합 묶음 id(같은 group = 한 노트로 합침)
                new_title     TEXT,                              -- 확정 제목(promote/병합)
                new_category  TEXT,
                new_conf      TEXT,                              -- 승격 confidence(medium/high)
                decided_judgment TEXT,                           -- 사람이 확정한 [감독판단]
                card_status   TEXT NOT NULL DEFAULT 'open',      -- open/decided/applying/applied/failed
                result        TEXT,
                decided_by    TEXT,
                decided_at    TEXT,
                done_at       TEXT,
                pushed_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(slug, fname)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shipwiki_card_status ON shipwiki_card(card_status, tier)")

        # 회의록 STT job queue (Phase 0a) — 웹/앱 업로드 → Mac 워커 폴 변환
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stt_job (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner         TEXT NOT NULL,                     -- 소유자(감독 username)
                title         TEXT,
                audio_name    TEXT,                              -- 원본 파일명(표시용)
                stored_name   TEXT NOT NULL,                     -- instance/stt_audio/<uuid>.<ext>
                status        TEXT NOT NULL DEFAULT 'pending',   -- pending|processing|done|error
                duration_sec  REAL,
                transcript    TEXT,
                minutes_json  TEXT,                              -- 가공 결과(Phase 1~)
                lang          TEXT NOT NULL DEFAULT 'auto',      -- 변환 언어(auto|ko|en)
                audio_deleted INTEGER NOT NULL DEFAULT 0,        -- 원본 오디오 삭제됨(transcript 보존)
                error         TEXT,
                attempts      INTEGER NOT NULL DEFAULT 0,
                claim_token   TEXT,                              -- 처리중 워커 클레임 토큰(CAS)
                claimed_at    TEXT,                              -- processing 진입 시각(lease 만료 판정)
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at    TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stt_job_owner ON stt_job(owner, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stt_job_status ON stt_job(status, id)")
        # stt_job additive 컬럼(기존 prod DB 마이그레이션): lang / audio_deleted / 요약(우라라카)
        _stt_cols = [r[1] for r in conn.execute('PRAGMA table_info(stt_job)').fetchall()]
        if _stt_cols and 'lang' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN lang TEXT NOT NULL DEFAULT 'auto'")
        if _stt_cols and 'audio_deleted' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN audio_deleted INTEGER NOT NULL DEFAULT 0")
        # 요약(우라라카) 라이프사이클: summary_status null|pending|processing|done|error + CAS
        if _stt_cols and 'summary_status' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN summary_status TEXT")
        if _stt_cols and 'summary_token' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN summary_token TEXT")
        if _stt_cols and 'summary_claimed_at' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN summary_claimed_at TEXT")
        if _stt_cols and 'summary_error' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN summary_error TEXT")
        # 화자분리(pyannote) 결과: [{start,end,text,speaker}] JSON (없으면 plain transcript)
        if _stt_cols and 'segments_json' not in _stt_cols:
            conn.execute("ALTER TABLE stt_job ADD COLUMN segments_json TEXT")
        # 위키 스레드 stable id (additive) — 메일↔위키↔Daily 연동 포인터
        sw_cols = [r[1] for r in conn.execute('PRAGMA table_info(shipwiki_card)').fetchall()]
        if sw_cols and 'wiki_thread_id' not in sw_cols:
            conn.execute('ALTER TABLE shipwiki_card ADD COLUMN wiki_thread_id TEXT')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shipwiki_card_wtid ON shipwiki_card(slug, wiki_thread_id)")
            print('  - shipwiki_card.wiki_thread_id column added')
        iss_cols2 = [r[1] for r in conn.execute('PRAGMA table_info(issues)').fetchall()]
        if iss_cols2 and 'wiki_thread_id' not in iss_cols2:
            conn.execute('ALTER TABLE issues ADD COLUMN wiki_thread_id TEXT')
            print('  - issues.wiki_thread_id column added')

        # 선박 로스터 SSOT(P0) — 시스템 간 매칭 식별자 흡수 (additive, 전부 nullable/NULL 기본)
        #   vsl_cd: SVMS 4자 코드 / vt_vessel_id: vesseltracker 내부 id / aliases: 구선명·표기 별칭 JSON
        ves_cols = [r[1] for r in conn.execute('PRAGMA table_info(vessels)').fetchall()]
        if ves_cols:
            if 'vsl_cd' not in ves_cols:
                conn.execute('ALTER TABLE vessels ADD COLUMN vsl_cd TEXT')
                print('  - vessels.vsl_cd column added')
            if 'vt_vessel_id' not in ves_cols:
                conn.execute('ALTER TABLE vessels ADD COLUMN vt_vessel_id INTEGER')
                print('  - vessels.vt_vessel_id column added')
            if 'aliases' not in ves_cols:
                conn.execute('ALTER TABLE vessels ADD COLUMN aliases TEXT')
                print('  - vessels.aliases column added')
            if 'gross_tonnage' not in ves_cols:
                conn.execute('ALTER TABLE vessels ADD COLUMN gross_tonnage TEXT')
                print('  - vessels.gross_tonnage column added')
            if 'dead_weight' not in ves_cols:
                conn.execute('ALTER TABLE vessels ADD COLUMN dead_weight TEXT')
                print('  - vessels.dead_weight column added')

        # ── SOA 자동화 그룹 SSOT (P0) ─────────────────────────────────
        #  그룹 = "어느 배치로 언제 검토할지"의 스케줄링 파티션일 뿐.
        #  돈 분기(장금 출금상신 Slip·검증강도·Crew 스킵)는 전부 SVMS owner(OW_COMP_ID)
        #  기반이라 category→owner_comp_id 매핑은 코드 상수(SOA_CATEGORY_OWNER)로만 존재.
        #  DB·UI 어디서도 편집 불가 = 사용자 편집이 돈 분기를 못 건드림.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS soa_group (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT NOT NULL UNIQUE
                           CHECK (length(key) BETWEEN 1 AND 8
                                  AND key NOT GLOB '*[^A-Z0-9]*'),
                label      TEXT NOT NULL,
                category   TEXT NOT NULL CHECK (category IN ('silver','skrt')),
                mode       TEXT NOT NULL DEFAULT 'explicit'
                           CHECK (mode IN ('explicit','dynamic_owner')),
                sort_order INTEGER NOT NULL DEFAULT 0,
                active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime')),
                updated_by TEXT            -- 누가 마지막으로 손댔나(감사 흔적)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS soa_group_vessel (
                group_id INTEGER NOT NULL REFERENCES soa_group(id),
                -- 4자 고정·대문자. 숫자 허용(SVMS 코드에 숫자가 섞일 여지 — SQLite CHECK 는
                -- 나중에 못 바꿔서 지나치게 좁히면 영구 거부가 됨).
                vsl_cd   TEXT NOT NULL CHECK (vsl_cd GLOB '[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'),
                PRIMARY KEY (group_id, vsl_cd)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_soa_group_vessel_cd ON soa_group_vessel(vsl_cd)")
        # SVMS My Vessel owner 스냅샷 — 맥 러너가 push. dynamic_owner 그룹의
        # "현재 편입 선박"을 UI에 보여주기 위한 표시용(러너 판정은 항상 SVMS 실시간 조회 기준).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS soa_vessel_owner (
                vsl_cd        TEXT PRIMARY KEY
                              CHECK (vsl_cd GLOB '[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'),
                owner_comp_id TEXT NOT NULL,
                updated_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # 시드 = 현행 하드코딩값 그대로(전환 직후 동작 동일). 이미 있으면 무시(멱등).
        if not conn.execute('SELECT 1 FROM soa_group LIMIT 1').fetchone():
            _soa_seed = [
                ('G1',   'SOA 실버 G1', 'silver', 'explicit',      10, ['ATBG', 'ATGR', 'ATGV', 'ATMT']),
                ('G2',   'SOA 실버 G2', 'silver', 'explicit',      20, ['ATNH', 'ATSH', 'ATSL', 'JATX']),
                ('G3',   'SOA 실버 G3', 'silver', 'explicit',      30, ['PCBJ', 'PCBS', 'PCGV', 'PCMC']),
                ('SKRT', 'SOA 장금',    'skrt',   'dynamic_owner', 40, []),
            ]
            for k, lab, cat, mode, so, vs in _soa_seed:
                gid = conn.execute(
                    'INSERT INTO soa_group (key,label,category,mode,sort_order,active) '
                    'VALUES (?,?,?,?,?,1)', (k, lab, cat, mode, so)).lastrowid
                for v in vs:
                    conn.execute('INSERT INTO soa_group_vessel (group_id,vsl_cd) VALUES (?,?)', (gid, v))
            # api_settings 는 지연생성(_ensure_api_table)이라 fresh DB 에서는 아직 없을 수 있음
            conn.execute('CREATE TABLE IF NOT EXISTS api_settings (k TEXT PRIMARY KEY, v TEXT)')
            conn.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('soa_groups_version','1')")
            print('  - soa_group seeded (G1/G2/G3/SKRT = 현행 하드코딩 동일)')
        # api_settings 는 위 seed 블록 안에서만 만들어져 왔다(= seeding 안 도는 DB 에는 없을 수
        # 있었다). `_ensure_api_table()` 이 요청마다 만들어주던 걸 캐시로 바꿨으니, 생성 책임을
        # 여기로 옮겨 **무조건** 보장한다. 캐시는 같은 경로의 DB 가 새로 만들어졌을 수 있으므로 비운다.
        conn.execute('CREATE TABLE IF NOT EXISTS api_settings (k TEXT PRIMARY KEY, v TEXT)')
        conn.commit()
        # init 직후에는 테이블 존재가 실측됐으므로 첫 API 인증 요청까지 DDL을 다시 할 이유가 없다.
        _API_TABLE_READY.clear()
        st = os.stat(DATABASE)
        _API_TABLE_READY[DATABASE] = (st.st_dev, st.st_ino)

        if fresh and os.path.exists(SEED_FILE):
            with open(SEED_FILE, encoding='utf-8') as f:
                conn.executescript(f.read())
            print('  · 시드 데이터 로드 완료')

        # 기본 admin 계정 자동 생성 — 계정이 0개일 때만(빈 DB 최초 기동·재해복구).
        # 비번은 코드에 박지 않는다(이 저장소는 public). TRMT_ADMIN_INIT_PW 가 있으면 그걸,
        # 없으면 매번 새로 난수 생성하고 이 자리에서 딱 한 번만 표시한다.
        # 표시된 값을 놓쳤으면 DB 를 지우고 다시 초기화하거나 관리자 계정으로 재설정할 것.
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            init_pw = os.environ.get('TRMT_ADMIN_INIT_PW') or secrets.token_urlsafe(12)
            conn.execute(
                'INSERT INTO users (username, password_hash, display_name, role) '
                'VALUES (?, ?, ?, ?)',
                ('admin', generate_password_hash(init_pw),
                 'Administrator', 'admin'),
            )
            if os.environ.get('TRMT_ADMIN_INIT_PW'):
                # 운영자가 준 값은 되풀이해 찍지 않는다(로그·journal 에 남으므로).
                print('  · 기본 관리자 생성: admin / TRMT_ADMIN_INIT_PW 값 (표시 생략)')
            else:
                print(f'  · 기본 관리자 생성: admin / {init_pw}   (자동생성 · 이 줄에서만 표시됨)')
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
#  네이티브 앱 Bearer 인증 (세션쿠키와 병행, /api/ 범위 한정)
# ═════════════════════════════════════════════════════════════════
@app.before_request
def _bearer_auth():
    """/api/ 요청에서 Authorization: Bearer <token> 이면 세션에 유저를 투명 주입.
    static·/login·돈경로/자동화 웹 라우트엔 훅 자체가 안 걸림. 일반 API는
    세션쿠키 로그인을 우선하되, Dock Manager mobile bridge만 Bearer를 강제 재검증한다."""
    if not request.path.startswith('/api/'):
        return                      # ← 범위 제한: /api/ 밖은 미접촉
    if request.path.startswith('/api/ext/'):
        return                      # ← 돈경로/워커 엔드포인트(@api_key_required) 완전 제외
    is_drydock_bridge = request.path == '/api/drydock/mobile-entry'
    if 'user_id' in session and not is_drydock_bridge:
        return                      # ← 브라우저=cookie, 일반 앱 API=Bearer로 분리
    hdr = request.headers.get('Authorization', '')
    parts = hdr.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return                      # scheme은 대소문자 무시(RFC 7235)
    tok = parts[1].strip()
    if not tok:
        return
    try:
        # return_timestamp: 발급시각을 함께 받아 /api/me 가 만료시각을 돌려줄 수 있게 한다.
        # 네이티브 앱이 오프라인 진입 여부를 fail-closed 로 판정하는 근거값(만료 모르면 진입 거부).
        data, issued_at = _load_token(tok)
    except BadData:
        return                      # 무효/만료/위조 → decorator/view가 401 처리
    if not isinstance(data, dict):
        return
    u = query('SELECT * FROM users WHERE id=? AND active=1', (data.get('uid'),), one=True)
    if not u:
        return
    pv = data.get('pv')
    if not isinstance(pv, str) or not hmac.compare_digest(pv, _pw_fingerprint(u['password_hash'])):
        return                      # 비번 변경/위조 → 토큰 폐기
    if is_drydock_bridge:
        session.clear()             # WKWebView에 남은 이전 계정 cookie를 fresh Bearer identity로 교체
        g._bearer_session_bridge = True  # admin/member 모두 stale cookie를 새 identity로 덮어씀
    session['user_id']       = u['id']
    session['username']      = u['username']
    session['display_name']  = u['display_name'] or u['username']
    session['role']          = u['role']
    session['supervisor_id'] = u['supervisor_id']
    session['app_scope']     = u['app_scope'] if 'app_scope' in u.keys() else 'business'
    g._token_auth = True
    g._token_issued_at = issued_at


@app.before_request
def _family_scope_guard():
    """자산 전용 계정은 우리자산 API 외의 TRMT 업무 표면을 fail-closed 차단."""
    uid = session.get('user_id')
    if not uid:
        return
    account = query('SELECT app_scope FROM users WHERE id=? AND active=1', (uid,), one=True)
    if not account:
        if session.get('app_scope') == 'family':
            session.clear()
            if request.path.startswith('/api/'):
                return jsonify({'error': 'unauthorized'}), 401
            abort(403)
        return
    if account['app_scope'] != 'family':
        return
    path = request.path
    if (path == '/api/me' or path == '/api/family-assets'
            or path.startswith('/api/family-assets/') or path == '/logout'):
        return
    if path.startswith('/api/'):
        return jsonify({'error': 'family_scope_only'}), 403
    abort(403)

@app.after_request
def _suppress_bearer_session_cookie(response):
    """Bearer API는 stateless로 유지하되, 명시적인 Dock Manager bridge만 session cookie를 발행."""
    if getattr(g, '_token_auth', False) and not getattr(g, '_bearer_session_bridge', False):
        session.permanent = False
        session.modified  = False
    return response


# ═════════════════════════════════════════════════════════════════
#  입거 Daily Report 첨부 blob 직접접근 차단 — 등록 위치가 계약이다
#  🔴 반드시 `_bearer_auth` **아래**: 위에 두면 g._token_auth 를 못 읽는다.
#     ⚠️ 다만 오늘 이 경로에서 그 값은 절대 세워지지 않는다 -- `_bearer_auth` 는
#     `/api/` 로 시작하지 않는 요청에서 즉시 return 하므로 static 경로엔 안 닿는다.
#     지금 통과 조건은 **쿠키 세션 하나**이고, 그래서 이 게이트가 iOS 를 깨지 않는지는
#     "앱이 이 URL 을 안 쓴다" 로 확인했다(실측: iOS 는 `/api/dock-daily/attachments/
#     <id>` + QuickLook 을 쓰고 `/static/uploads/` 는 dock|boarding|receipt 만 읽는다).
#     토큰 분기는 나중에 Bearer 범위가 넓어질 때를 위한 것이고, 그때 이 순서가 필요하다.
# ═════════════════════════════════════════════════════════════════
_DOCK_DAILY_BLOB_PREFIX = 'dock_daily_'


@app.before_request
def _guard_dock_daily_blobs():
    """`/static/uploads/dock_daily_*` 는 로그인 없이 내주지 않는다.

    첨부 라우트(`/api/dock-daily/attachments/<id>`)에는 `login_required` 가 붙어 있지만
    파일 자체는 `static/uploads/` 안에 있어서 Flask 기본 `/static/<path>` 라우트가
    **인증 없이 그대로 내보낸다**. 실측(라이브): `/static/uploads/<이름>` → 404(게이트
    없음), `/dock-daily` → 302 login. 즉 지금 기밀성은 uuid 파일명이 안 새는 것에만
    걸려 있다 -- 메일 본문·서버 로그·브라우저 히스토리·프록시 어디로든 새면 끝이다.
    입거 첨부는 견적서·검사보고서라 공개돼도 되는 파일이 아니다.

    🔴 **범위를 좁게 잡는다.** `/static/uploads/` 를 통째로 막으면 도크 리포트·승선·
    영수증 사진이 깨진다 -- `routes_calendar_dock.py` 와 DOCX 생성기가 그 URL 을
    **공개 URL 로 일부러** 쓴다(메일 클라이언트·Word 는 세션이 없다). 그래서 이 기능이
    만드는 이름(`dock_daily_` 접두어, `attachment_post` 가 붙인다)만 막는다.
    """
    path = request.path
    if not path.startswith('/static/uploads/'):
        return None
    if not path.rsplit('/', 1)[-1].startswith(_DOCK_DAILY_BLOB_PREFIX):
        return None
    if session.get('user_id') or getattr(g, '_token_auth', None):
        return None
    return jsonify({'error': 'unauthorized'}), 401


# ═════════════════════════════════════════════════════════════════
#  CSRF (쿠키 세션 전용) — 등록 위치가 계약이다
#  🔴 반드시 `_bearer_auth` **아래**: 위에 두면 g._token_auth 가 아직 없어서
#     네이티브 앱의 모든 쓰기가 403 으로 죽는다.
#  🔴 반드시 `_idem_replay` **위**: 아래에 두면 위조 요청이 거절되기 전에
#     멱등 key 를 선점해서, 형이 나중에 보내는 정상 재전송이 그 자리를 못 쓴다.
# ═════════════════════════════════════════════════════════════════
csrf.init_app(app)
# 토큰 발급 라우트(`/api/csrf-token`)는 login_required 를 쓰므로 helpers_shared
# import 뒤에 있다. 훅 등록만 여기서 끝낸다 — 순서 계약은 훅에만 걸린다.


# ═════════════════════════════════════════════════════════════════
#  오프라인 재전송 중복방지(Idempotency-Key) — 네이티브 앱 보관함 전용
#  🔴 등록 위치가 계약이다: 반드시 `_bearer_auth` **아래**에 있어야 한다.
#     before_request 는 등록순으로 도는데, 위에 두면 session['user_id'] 가 아직 없어
#     스코프를 못 잡고 훅이 통째로 무력화된다(중복 생성이 조용히 살아남음).
# ═════════════════════════════════════════════════════════════════
_IDEM_METHODS   = ('POST', 'PUT', 'PATCH', 'DELETE')
_IDEM_KEY_RE    = re.compile(r'^[A-Za-z0-9_.:\-]{8,128}$')
_IDEM_BODY_MAX  = 256 * 1024        # 이보다 큰 성공응답은 본문을 버리고 재생 전용 표식만 남김
_IDEM_TTL_DAYS  = 7


def _idem_key_of(req):
    """이 요청이 중복방지 대상이면 key, 아니면 None. 잘못된 key 는 (None, 400) 로 거절한다.
    🔴 형식 위반을 조용히 무시하면 '멱등이라 믿고 재전송' 하는 클라가 중복을 만든다 — 400 으로 깬다."""
    if req.method not in _IDEM_METHODS:
        return None, None
    if not req.path.startswith('/api/') or req.path.startswith('/api/ext/'):
        return None, None            # /api/ext/ 는 워커·api_key 경로라 앱 보관함과 무관
    raw = (req.headers.get('X-Idempotency-Key') or '').strip()
    if not raw:
        return None, None
    if not _IDEM_KEY_RE.match(raw):
        return None, (jsonify({'error': 'bad_idempotency_key'}), 400)
    return raw, None


@app.before_request
def _idem_replay():
    """같은 key 재요청이면 저장된 성공응답을 그대로 되돌려준다(뷰를 다시 실행하지 않음).

    상태별 처리
      · done        → 저장된 (code, body) 재생 + `X-Idempotent-Replay: 1`
      · in_progress → 409 in_progress. 앞 요청이 아직 도는 중 = 잠시 뒤 재시도하라는 뜻
      · unknown     → 409 idem_unknown. **자동 재실행 절대 금지** — 뷰가 처리 도중 죽어
                      저장됐는지 모르는 상태다. 여기서 재실행하면 돈경로에서 이중집행이 된다.
      · 없음        → claim 을 박고 뷰로 진행
    """
    key, err = _idem_key_of(request)
    if err:
        return err
    if not key:
        return
    uid = session.get('user_id')
    if not uid:
        return                       # 미인증은 어차피 401 — 원장에 남길 이유 없음
    row = query('SELECT method,path,status,code,body,content_type FROM client_idem '
                'WHERE user_id=? AND idem_key=?', (uid, key), one=True)
    if row is not None:
        # 같은 key 를 다른 요청에 재사용하면 남의 응답을 돌려줄 위험 — 경로/메서드까지 대조.
        if row['method'] != request.method or row['path'] != request.path:
            return jsonify({'error': 'idempotency_key_reused'}), 409
        if row['status'] == 'done':
            resp = app.response_class(
                row['body'] or '', status=row['code'] or 200,
                content_type=row['content_type'] or 'application/json')
            resp.headers['X-Idempotent-Replay'] = '1'
            return resp
        if row['status'] == 'unknown':
            return jsonify({'error': 'idem_unknown',
                            'message': '이전 시도의 처리 결과를 알 수 없습니다. 서버에서 확인 후 다시 시도하세요.'}), 409
        return jsonify({'error': 'in_progress'}), 409
    try:
        execute('INSERT INTO client_idem (user_id,idem_key,method,path,status) '
                'VALUES (?,?,?,?,?)', (uid, key, request.method, request.path, 'in_progress'))
    except sqlite3.IntegrityError:
        return jsonify({'error': 'in_progress'}), 409      # 동시 중복요청 — 하나만 통과
    g._idem = (uid, key)
    if secrets.randbelow(100) == 0:                         # 가끔만 청소(인덱스 있어 저렴)
        try:
            execute("DELETE FROM client_idem WHERE created_at < datetime('now','localtime',?)",
                    (f'-{_IDEM_TTL_DAYS} days',))
        except Exception:
            app.logger.debug('client_idem prune skip', exc_info=True)


@app.after_request
def _idem_finalize(response):
    """성공응답만 보관한다. 실패 처리는 4xx 와 5xx 를 **절대 같이 묶지 않는다**:
      · 4xx = 클라이언트가 잘못 보낸 것 → 아무것도 안 바뀜 → claim 삭제(고쳐서 재전송이 정상)
      · 5xx = 서버가 도중에 깨진 것 → **일부 커밋됐을 수 있음** → unknown 으로 남겨 자동 재실행 차단
    🔴 5xx 를 지우면 재전송이 뷰를 다시 돌려 이중집행이 된다(예외는 Flask 가 500 으로 삼켜
       after_request 까지 오므로, teardown 만 믿으면 이 경로가 통째로 빠진다)."""
    claim = getattr(g, '_idem', None)
    if not claim:
        return response
    uid, key = claim
    try:
        if response.status_code >= 500:
            execute("UPDATE client_idem SET status='unknown', code=? "
                    'WHERE user_id=? AND idem_key=?', (response.status_code, uid, key))
        elif 200 <= response.status_code < 300:
            body, ctype = None, response.content_type
            if response.is_streamed or response.direct_passthrough:
                body = None                                 # 스트리밍 응답은 소비하면 안 됨
            else:
                raw = response.get_data()
                body = raw.decode('utf-8', 'replace') if len(raw) <= _IDEM_BODY_MAX else None
            if body is None:
                # 본문을 못 남겨도 **재실행은 막아야 한다** — 재생용 최소 응답으로 대체.
                body, ctype = '{"ok":true,"idempotent_replay":true,"body_omitted":true}', 'application/json'
            execute('UPDATE client_idem SET status=?, code=?, body=?, content_type=? '
                    'WHERE user_id=? AND idem_key=?',
                    ('done', response.status_code, body, ctype, uid, key))
        else:
            execute('DELETE FROM client_idem WHERE user_id=? AND idem_key=?', (uid, key))
        g._idem = None                                      # teardown 이 unknown 으로 덮지 않게
    except Exception:
        app.logger.warning('client_idem finalize 실패(uid=%s)', uid, exc_info=True)
    return response


@app.teardown_request
def _idem_mark_unknown(exc=None):
    """뷰가 예외로 죽었거나 after_request 가 못 돈 경우 = **결과 모름**.
    🔴 여기서 행을 지우면 다음 재전송이 뷰를 다시 실행한다 — 이미 커밋된 쓰기가 있었다면 이중집행.
       모르면 모른다고 남기고(unknown) 사람이 확인하게 한다."""
    claim = getattr(g, '_idem', None)
    if not claim:
        return
    uid, key = claim
    try:
        execute("UPDATE client_idem SET status='unknown' "
                "WHERE user_id=? AND idem_key=? AND status='in_progress'", (uid, key))
    except Exception:
        app.logger.warning('client_idem unknown 표기 실패(uid=%s)', uid, exc_info=True)


# ═════════════════════════════════════════════════════════════════
#  Extracted implementation boundaries
# ═════════════════════════════════════════════════════════════════
# helpers_shared is a real imported module since 2026-08-12; the ``exec`` loader
# that used to run it inside this namespace is gone.  It could only exist because
# the two files needed each other: see ``app_core.py`` for how lowering the
# borrowed primitives removed that cycle.
#
# The consequence worth keeping in mind: the import list below is now the *only*
# statement of what this file needs from the helpers, and a typo in it fails at
# import time instead of surfacing as a NameError mid-request.
#
# Nothing here is exec'd any more, so the development reloader watches every
# boundary on its own — the old ``extra_files`` bookkeeping was deleted with the
# loader rather than left behind as an empty list that looks meaningful.
import helpers_shared
from helpers_shared import (
    _AOR_ACTIVE_STATUSES, _API_TABLE_READY, _aor_absorbing_trigger_install,
    _aor_active_index_install, _aor_status_list_sql,
)
# The five route boundaries are real imported modules since 2026-08-11.  Their
# `from app import ...` works because these lines run after helpers_shared and
# every app.py primitive they need is already bound.
#
# The alias below is what keeps `python app.py` (the dev entry point) alive:
# run as a script this file is module `__main__`, so without the alias
# `from app import ...` would execute app.py a SECOND time as module "app" —
# and that second pass would reach its own module imports while they are still
# half-initialized (`bp` not yet bound) and crash with
# "partially initialized module".  With the alias the import resolves to the
# already-running module.  A no-op under gunicorn/tests, where this file is
# imported as "app" in the first place.
sys.modules.setdefault('app', sys.modules[__name__])


@app.route('/api/csrf-token')
@helpers_shared.login_required
def csrf_token_get():
    """새 토큰 발급 — 열어둔 탭이 만료된 토큰으로 403 을 맞았을 때 쓰는 창구.

    GET 이라 이 라우트 자체는 검사 대상이 아니고, 세션이 없으면
    login_required 가 401 로 끊는다. 브라우저 래퍼가 403 을 한 번 받으면
    여기서 새 토큰을 받아 그 요청만 1회 재전송한다."""
    return jsonify({'token': csrf.csrf_token()})


import routes_core
app.register_blueprint(routes_core.bp)
import ai_gemini
app.register_blueprint(ai_gemini.bp)
import routes_calendar_dock
app.register_blueprint(routes_calendar_dock.bp)
import routes_dock_submit
app.register_blueprint(routes_dock_submit.bp)
import routes_dock_daily
app.register_blueprint(routes_dock_daily.bp)
import routes_repair_request
app.register_blueprint(routes_repair_request.bp)
import routes_family_assets
app.register_blueprint(routes_family_assets.bp)
import routes_liscr
app.register_blueprint(routes_liscr.bp)
import routes_tail
app.register_blueprint(routes_tail.bp)
# Static contract marker: dock sync notifications keep the historical deep link.
# The executable call remains in routes_dock_submit.py.
# link='trmt://dock'
def _auto_migrate():
    """기존 DB에 대한 idempotent 스키마 보강 — 배포 시 마이그레이션 누락 방지.
    · schema.sql 의 CREATE TABLE/INDEX IF NOT EXISTS 재적용(누락 테이블 생성)
    · ALTER 가 필요한 신규 컬럼은 개별 점검 후 추가
    """
    init_runtime()
    if not os.path.exists(DATABASE):
        return
    conn = sqlite3.connect(DATABASE)
    try:
        # absorbing 상태 이탈 금지 trigger — init_db 를 안 타는 기존 DB 에도 반드시 걸려야 한다.
        # 없으면 /api/ext/aor/reingest-statuses 가 skip 근거를 안 내보내 러너가 매 run 전건
        # 재처리한다(안전하지만 낭비). 즉 누락은 위험이 아니라 성능 저하로만 나타나므로
        # 조용히 지나가지 않게 로그를 남긴다.
        try:
            _aor_absorbing_trigger_install(conn)
        except Exception as e:
            print(f'[auto_migrate] aor_draft absorbing trigger 생성 실패: {e}')
        # 활성행 유일성 index 도 같은 이유로 여기서 한 번 더 맞춘다(옛 raw-컬럼 → canonical 표현식).
        # ⚠️ init_db 와 달리 여기엔 앞선 중복 정리가 없다 — 중복이 남아 있으면 install 이 교체를
        #    거부하고 예외를 던진다. 옛 index 는 그대로라 안전하고, 로그로 드러난다.
        try:
            if _aor_active_index_install(conn):
                print('[auto_migrate] uq_aor_draft_active_cd → canonical 표현식 index 로 교체')
        except Exception as e:
            print(f'[auto_migrate] aor_draft 활성행 index 교체 실패: {e}')
        try:
            with open(SCHEMA_FILE, encoding='utf-8') as fh:
                conn.executescript(fh.read())   # 전부 IF NOT EXISTS → 무해
        except Exception as e:
            print(f'[auto_migrate] schema 재적용 건너뜀: {e}')
        try:
            user_cols = {r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
            if user_cols and 'app_scope' not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN app_scope TEXT NOT NULL DEFAULT 'business'")
                print('[auto_migrate] users.app_scope 추가')
        except Exception as e:
            print(f'[auto_migrate] users.app_scope 점검 건너뜀: {e}')
        migration_steps.run_family_asset_migrations(conn)
        # 가장 독립적인 additive 보강부터 순서가 고정된 하위 모듈에서 실행한다.
        # 각 step은 자체 try/except를 유지해 하나의 legacy table 실패가 뒤 step을 막지 않는다.
        migration_steps.run_foundation_migrations(conn)
        # /liscr 범용화(2026-08-19) — 등록 유형·Vendor·Expense 를 행에 남긴다.
        # 🔴 liscr_job 은 **schema.sql 이 아니라 init_db 안에서** 만들어지는 표라, 위의
        #    schema 재적용으로는 손도 안 닿는다. 그리고 CREATE TABLE IF NOT EXISTS 는 이미
        #    있는 표에 컬럼을 안 붙인다 — 여기 ALTER 가 빠지면 라이브에서 "no such column:
        #    profile" 로 /liscr 이 통째로 죽는다.
        # 🔴 독립 try — 다른 마이그레이션과 운명을 묶지 않는다.
        try:
            lcols = [r[1] for r in conn.execute('PRAGMA table_info(liscr_job)').fetchall()]
            if lcols:
                # DEFAULT 'liscr' — 이미 큐에 있던 행은 전부 기국 건이다(그때는 그것뿐이었다).
                # NULL 로 두면 러너가 프리셋을 못 정해 기존 대기건이 조용히 HOLD 로 떨어진다.
                want = (('profile', "TEXT NOT NULL DEFAULT 'liscr'"),
                        ('hard_json', 'TEXT'), ('vndr_cd', 'TEXT'),
                        ('vndr_nm', 'TEXT'), ('exp_nm', 'TEXT'))
                for col, ddl in want:
                    if col not in lcols:
                        conn.execute('ALTER TABLE liscr_job ADD COLUMN %s %s' % (col, ddl))
                        print(f'[auto_migrate] liscr_job.{col} 추가됨')
                # 🔴 ALTER 가 중간에 실패하면 일부만 붙은 채로 부팅이 계속된다. 그 상태의
                #    /liscr 은 'no such column' 으로 죽는데, 로그에 조용히 한 줄만 남으면
                #    아무도 안 본다. 결과를 **다시 읽어** 확인하고 크게 남긴다.
                have = {r[1] for r in conn.execute('PRAGMA table_info(liscr_job)').fetchall()}
                miss = [c for c, _ in want if c not in have]
                if miss:
                    print('[auto_migrate] 🔴 liscr_job 컬럼 누락 %s — /liscr 이 죽습니다' % miss)
        except Exception as e:
            print(f'[auto_migrate] 🔴 liscr_job 컬럼 점검 실패 — /liscr 이 죽을 수 있음: {e}')
        # 마스터 스냅샷 표. init_db 가 매 배포마다 돌긴 하지만, 이 표가 없으면 "기타 인보이스"
        # 업로드 화면이 통째로 비어 기능이 죽으므로 여기서도 명시적으로 만든다.
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS liscr_master (
                    kind TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    n INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
        except Exception as e:
            print(f'[auto_migrate] liscr_master 점검 건너뜀: {e}')
        # 오프라인 보관함 재전송 중복방지 원장. schema 재적용으로도 생기지만, 위 executescript 가
        # 어떤 이유로든 중간에 끊기면 이 표만 없어 **재전송이 중복 생성**으로 이어진다.
        # 🔴 독립 try + 명시 생성 — 중복방지는 조용히 빠지면 안 되는 종류다.
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS client_idem (
                    user_id INTEGER NOT NULL, idem_key TEXT NOT NULL,
                    method TEXT NOT NULL, path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    code INTEGER, body TEXT, content_type TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (user_id, idem_key))""")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_client_idem_created ON client_idem(created_at)')
        except Exception as e:
            print(f'[auto_migrate] client_idem 점검 건너뜀: {e}')
        # SOA 그룹은 기존 prod DB에도 schema 재적용만으로 테이블이 생긴다. 최초 전환 시
        # 현행 G1/G2/G3/SKRT 값을 seed해야 runner가 빈 설정으로 fail-closed 되지 않는다.
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS api_settings (k TEXT PRIMARY KEY, v TEXT)')
            if not conn.execute('SELECT 1 FROM soa_group LIMIT 1').fetchone():
                seed = [
                    ('G1', 'SOA 실버 G1', 'silver', 'explicit', 10, ['ATBG','ATGR','ATGV','ATMT']),
                    ('G2', 'SOA 실버 G2', 'silver', 'explicit', 20, ['ATNH','ATSH','ATSL','JATX']),
                    ('G3', 'SOA 실버 G3', 'silver', 'explicit', 30, ['PCBJ','PCBS','PCGV','PCMC']),
                    ('SKRT', 'SOA 장금', 'skrt', 'dynamic_owner', 40, []),
                ]
                for key, label, category, mode, sort_order, vessels in seed:
                    gid = conn.execute('INSERT INTO soa_group (key,label,category,mode,sort_order,active) '
                                       'VALUES (?,?,?,?,?,1)',
                                       (key, label, category, mode, sort_order)).lastrowid
                    conn.executemany('INSERT INTO soa_group_vessel (group_id,vsl_cd) VALUES (?,?)',
                                     [(gid, vessel) for vessel in vessels])
                conn.execute("INSERT OR REPLACE INTO api_settings (k,v) VALUES ('soa_groups_version','1')")
                conn.commit()
                print('[auto_migrate] SOA 그룹 현행값 seed 완료')
        except Exception as e:
            print(f'[auto_migrate] SOA 그룹 seed 건너뜀: {e}')
        try:    # 감사 흔적 컬럼(먼저 만들어진 DB 보강). 이미 있으면 조용히 통과.
            cols = {r[1] for r in conn.execute('PRAGMA table_info(soa_group)')}
            if cols and 'updated_by' not in cols:
                conn.execute('ALTER TABLE soa_group ADD COLUMN updated_by TEXT')
                conn.commit()
                print('[auto_migrate] soa_group.updated_by 추가')
        except Exception as e:
            print(f'[auto_migrate] soa_group.updated_by 건너뜀: {e}')
        # soa_review_case.status CHECK 완화(C/T/D/S → 대문자 1~2자).
        # SVMS가 R(SM 반려) 등 다른 코드로 전이하면 기존 CHECK 때문에 snapshot ingest가 영구 실패해
        # 로컬 상태가 'S'로 굳었다(승인 끝난 건이 '승인대기'로 남아 중복 승인 시도 유발).
        # 쓰기 권한은 앱의 editable(D/S) 화이트리스트가 계속 담당한다.
        fk_prev = None
        began = False
        try:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='soa_review_case'").fetchone()
            old_sql = (row[0] if row else '') or ''
            # 공백·줄바꿈 변형에 강하게: 정규화 후 status의 좁은 IN 목록을 찾아 원문 그대로 치환한다.
            narrow = re.search(r"CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)", old_sql, re.I)
            if narrow and re.fullmatch(r"'[CTDS]'(,'[CTDS]')*",
                                       re.sub(r'\s+', '', narrow.group(0))[len('CHECK(statusIN('):-2]):
                cols = [r[1] for r in conn.execute('PRAGMA table_info(soa_review_case)').fetchall()]
                col_list = ','.join('"%s"' % x for x in cols)
                # 이 테이블에 딸린 index/trigger DDL은 DROP과 함께 사라지므로 그대로 재생성한다.
                extras = [r[0] for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='soa_review_case' AND type IN ('index','trigger') "
                    "AND sql IS NOT NULL").fetchall()]
                new_sql = (old_sql
                           .replace(narrow.group(0),
                                    "CHECK (status GLOB '[A-Z]' OR status GLOB '[A-Z][A-Z]')")
                           .replace('soa_review_case', 'soa_review_case__new', 1))
                if 'soa_review_case__new' not in new_sql or narrow.group(0) in new_sql:
                    raise RuntimeError('unexpected soa_review_case DDL — 수동 확인 필요')
                conn.commit()                               # 열린 transaction이 있으면 PRAGMA가 무시된다
                fk_prev = conn.execute('PRAGMA foreign_keys').fetchone()[0]
                conn.execute('PRAGMA foreign_keys=OFF')      # 자식 테이블 CASCADE 삭제 방지
                if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
                    raise RuntimeError('foreign_keys OFF 실패 — 마이그레이션 중단(자식행 손실 위험)')
                conn.execute('BEGIN IMMEDIATE')
                began = True
                conn.execute('DROP TABLE IF EXISTS soa_review_case__new')
                conn.execute(new_sql)
                conn.execute('INSERT INTO soa_review_case__new (%s) SELECT %s FROM soa_review_case'
                             % (col_list, col_list))
                moved = conn.execute('SELECT COUNT(*) FROM soa_review_case__new').fetchone()[0]
                before = conn.execute('SELECT COUNT(*) FROM soa_review_case').fetchone()[0]
                if moved != before:
                    raise RuntimeError('행 수 불일치 %s != %s' % (moved, before))
                conn.execute('DROP TABLE soa_review_case')
                conn.execute('ALTER TABLE soa_review_case__new RENAME TO soa_review_case')
                for ddl in extras:
                    conn.execute(ddl)
                bad = conn.execute('PRAGMA foreign_key_check').fetchall()
                if bad:
                    raise RuntimeError('FK 무결성 실패: %s' % bad[:3])
                conn.execute('COMMIT')
                began = False
                print('[auto_migrate] soa_review_case.status CHECK 완화 완료 (rows=%d)' % moved)
        except Exception as e:
            if began:                    # 내가 연 transaction일 때만 되돌린다(앞선 마이그레이션 보호)
                try:
                    conn.execute('ROLLBACK')
                except Exception:
                    pass
            print(f'[auto_migrate] soa_review_case.status CHECK 완화 건너뜀: {e}')
        finally:
            if fk_prev is not None:      # 어떤 경로로 끝나든 FK 강제 복구 + 복구됐는지 재확인
                broken = None
                try:
                    conn.execute('PRAGMA foreign_keys=%d' % (1 if fk_prev else 0))
                    now_fk = conn.execute('PRAGMA foreign_keys').fetchone()[0]
                    if bool(now_fk) != bool(fk_prev):
                        broken = 'PRAGMA 재조회 %s != %s' % (now_fk, fk_prev)
                except Exception as e:
                    broken = str(e)
                if broken:
                    # FK OFF인 채로 남은 connection으로 계속 쓰면 무결성이 깨진다 — 폐기하고 새로 연다.
                    print(f'[auto_migrate] ⚠ foreign_keys 복구 실패({broken}) — connection 폐기 후 재연결')
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = sqlite3.connect(DATABASE)
                    conn.execute('PRAGMA foreign_keys=%d' % (1 if fk_prev else 0))
                    if bool(conn.execute('PRAGMA foreign_keys').fetchone()[0]) != bool(fk_prev):
                        raise RuntimeError('재연결 후 foreign_keys 복구 실패')
        # 마이그레이션 후에도 R을 저장할 수 없으면 '승인 끝난 건이 승인대기로 남는' 사고가 재발한다.
        # DDL 문자열 대신 실제로 넣어보고 되돌리는 실측으로 판정한다(형식 변형에 영향받지 않음).
        # probe 자체가 예외면 마지막 알려진 False를 유지하면 fail-open이다. 성공할 때만 False로 내린다.
        #
        # 🔴 이 플래그의 소유자는 routes_calendar_dock 이다(ingest fail-closed 게이트가 거기 있음).
        # exec 공유 네임스페이스 시절의 globals()['...'] 쓰기는 Blueprint 전환 후 app 모듈에만
        # 반영되고 소유 모듈은 영원히 True(fail-closed)로 남는다 — R 상태 ingest 가 프로덕션에서
        # 영구 차단되는 결함이라 소유 모듈 속성으로 직접 쓴다. import 는 이 함수가 모듈 로드
        # 이후에만 호출되므로 안전하다.
        import routes_calendar_dock as _soa_owner
        _soa_owner.SOA_REVIEW_SCHEMA_DEGRADED = True
        try:
            conn.execute('SAVEPOINT soa_chk_probe')
            try:
                conn.execute("INSERT INTO soa_review_case (sx_cd,status) VALUES ('__PROBE__ZZZZZZZZ','R')")
                degraded = False
            except Exception:
                degraded = True
            finally:
                conn.execute('ROLLBACK TO soa_chk_probe')
                conn.execute('RELEASE soa_chk_probe')
            _soa_owner.SOA_REVIEW_SCHEMA_DEGRADED = degraded
            if degraded:
                print('[auto_migrate] ⚠ soa_review_case.status가 R을 거부함 — 상태 동기화 불가(수동 조치 필요)')
        except Exception as e:
            print(f'[auto_migrate] soa_review_case CHECK 확인 실패: {e}')
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fleet_next_port_override (
                    vessel_key     TEXT PRIMARY KEY,
                    vessel_name    TEXT NOT NULL,
                    manual_label   TEXT NOT NULL,
                    manual_code    TEXT,
                    manual_lat     REAL NOT NULL,
                    manual_lng     REAL NOT NULL,
                    auto_snapshot  TEXT NOT NULL,
                    active         INTEGER NOT NULL DEFAULT 1,
                    inactivated_at TEXT,
                    inactivated_reason TEXT,
                    created_by     TEXT,
                    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    updated_by     TEXT,
                    updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                )
            """)
            cols = [r[1] for r in conn.execute('PRAGMA table_info(fleet_next_port_override)').fetchall()]
            for col, ddl in (
                    ('active', "ALTER TABLE fleet_next_port_override ADD COLUMN active INTEGER NOT NULL DEFAULT 1"),
                    ('inactivated_at', "ALTER TABLE fleet_next_port_override ADD COLUMN inactivated_at TEXT"),
                    ('inactivated_reason', "ALTER TABLE fleet_next_port_override ADD COLUMN inactivated_reason TEXT"),
                    ('updated_by', "ALTER TABLE fleet_next_port_override ADD COLUMN updated_by TEXT")):
                if col not in cols:
                    conn.execute(ddl)
            conn.commit()
        except Exception as e:
            print(f'[auto_migrate] fleet_next_port_override 점검 건너뜀: {e}')
        # vt_findings Remark/priority + 과거 Full report marker 제거
        try:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(vt_findings)').fetchall()]
            if cols and 'user_remark' not in cols:
                conn.execute("ALTER TABLE vt_findings ADD COLUMN user_remark TEXT NOT NULL DEFAULT ''")
                print('[auto_migrate] vt_findings.user_remark 추가됨')
            if cols and 'full_report_remark' not in cols:
                conn.execute("ALTER TABLE vt_findings ADD COLUMN full_report_remark TEXT NOT NULL DEFAULT ''")
                print('[auto_migrate] vt_findings.full_report_remark 추가됨')
            if cols and 'priority' not in cols:
                conn.execute("ALTER TABLE vt_findings ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
                print('[auto_migrate] vt_findings.priority 추가됨')
            marker_re = re.compile(
                r'^[ \t]*\[SIRE Full Report 자동반영\][ \t]*\r?\n(.*?)^[ \t]*'
                r'\[/SIRE Full Report 자동반영\][ \t]*$', re.S | re.M,
            )
            migrated = 0
            for row in conn.execute(
                    "SELECT id,user_remark FROM vt_findings "
                    "WHERE user_remark LIKE '%[SIRE Full Report 자동반영]%'").fetchall():
                raw = row[1] or ''
                blocks = marker_re.findall(raw)
                if not blocks:
                    continue
                cleaned = marker_re.sub('', raw).strip()
                cleaned = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', cleaned)
                automatic = re.sub(r'\s+', ' ', blocks[-1]).strip()
                visible = f'{cleaned}\n\n{automatic}'.strip() if cleaned else automatic
                conn.execute(
                    'UPDATE vt_findings SET user_remark=?, full_report_remark=? WHERE id=?',
                    (visible, automatic, row[0]),
                )
                migrated += 1
            if migrated:
                print(f'[auto_migrate] SIRE Full report marker 제거: {migrated}건')
        except Exception as e:
            print(f'[auto_migrate] vt_findings 컬럼 점검 건너뜀: {e}')
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vt_full_report_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vetting_id INTEGER NOT NULL,
                report_number TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                filename TEXT,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                applied_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (vetting_id) REFERENCES vettings(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vt_full_report_audit_vetting "
                     "ON vt_full_report_audit(vetting_id, created_at DESC)")

        # SVMS SIRE attachment provenance; pre-existing rows remain manual.
        try:
            vt_cols = [r[1] for r in conn.execute('PRAGMA table_info(vettings)').fetchall()]
            for col, ddl in (
                ('svms_full_report_yn', "ALTER TABLE vettings ADD COLUMN svms_full_report_yn TEXT"),
                ('svms_close_report_yn', "ALTER TABLE vettings ADD COLUMN svms_close_report_yn TEXT"),
                ('svms_report_uploaded_yn', "ALTER TABLE vettings ADD COLUMN svms_report_uploaded_yn TEXT"),
                ('svms_status_synced_at', "ALTER TABLE vettings ADD COLUMN svms_status_synced_at TEXT"),
            ):
                if vt_cols and col not in vt_cols:
                    conn.execute(ddl)
            cols = [r[1] for r in conn.execute('PRAGMA table_info(vt_attachments)').fetchall()]
            for col, ddl in (
                ('source', "ALTER TABLE vt_attachments ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"),
                ('source_type', "ALTER TABLE vt_attachments ADD COLUMN source_type TEXT"),
                ('external_sire_cd', "ALTER TABLE vt_attachments ADD COLUMN external_sire_cd TEXT"),
                ('external_file_id', "ALTER TABLE vt_attachments ADD COLUMN external_file_id TEXT"),
                ('sha256', "ALTER TABLE vt_attachments ADD COLUMN sha256 TEXT"),
                ('synced_at', "ALTER TABLE vt_attachments ADD COLUMN synced_at TEXT"),
                ('inactive_at', "ALTER TABLE vt_attachments ADD COLUMN inactive_at TEXT"),
            ):
                if cols and col not in cols:
                    conn.execute(ddl)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_vt_attachments_svms_identity_sha ON vt_attachments(external_file_id, sha256) WHERE source='svms' AND external_file_id IS NOT NULL AND sha256 IS NOT NULL")
        except Exception as e:
            print(f'[auto_migrate] vt_attachments provenance 점검 건너뜀: {e}')

        # 독립적인 표시/분류 메타데이터 보강은 순서와 실패 경계를 하위 모듈에서 고정한다.
        migration_steps.run_management_metadata_migrations(conn)

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

        # stt_job additive 컬럼 (회의록): lang(auto|ko|en) / audio_deleted(원본삭제, transcript보존)
        try:
            _sc = [r[1] for r in conn.execute('PRAGMA table_info(stt_job)').fetchall()]
            if _sc and 'lang' not in _sc:
                conn.execute("ALTER TABLE stt_job ADD COLUMN lang TEXT NOT NULL DEFAULT 'auto'")
                print('[auto_migrate] stt_job.lang 추가됨')
            if _sc and 'audio_deleted' not in _sc:
                conn.execute("ALTER TABLE stt_job ADD COLUMN audio_deleted INTEGER NOT NULL DEFAULT 0")
                print('[auto_migrate] stt_job.audio_deleted 추가됨')
            # 요약(우라라카) 컬럼
            for _col, _ddl in (('summary_status', 'TEXT'), ('summary_token', 'TEXT'),
                               ('summary_claimed_at', 'TEXT'), ('summary_error', 'TEXT')):
                if _sc and _col not in _sc:
                    conn.execute(f"ALTER TABLE stt_job ADD COLUMN {_col} {_ddl}")
                    print(f'[auto_migrate] stt_job.{_col} 추가됨')
        except Exception as e:
            print(f'[auto_migrate] stt_job 컬럼 점검 건너뜀: {e}')

        # Dock Daily Report additive migration.  The complete table contract lives in
        # schema.sql; this column check keeps existing installations compatible when
        # schema.sql CREATE TABLE IF NOT EXISTS cannot alter an already-created table.
        try:
            dd_cols = [r[1] for r in conn.execute(
                'PRAGMA table_info(dock_daily_report)').fetchall()]
            if dd_cols and 'source_changed_after_final' not in dd_cols:
                conn.execute('ALTER TABLE dock_daily_report ADD COLUMN '
                             'source_changed_after_final INTEGER NOT NULL DEFAULT 0')
                print('[auto_migrate] dock_daily_report.source_changed_after_final 추가됨')
        except Exception as e:
            print(f'[auto_migrate] dock_daily_report 컬럼 점검 건너뜀: {e}')

        # SVMS 입거 Daily 승인 큐. 기존 보고서/DB에는 additive 로만 붙인다.
        try:
            # 🔴 이 `conn` 은 row_factory 가 없다 -- `r['name']` 은 TypeError 로 죽고
            #    except 가 그걸 삼켜 **마이그레이션이 조용히 안 돈다**(라이브 실측
            #    2026-08-22: 컬럼이 없어 후보 캐시가 500). 관례대로 `r[1]` 을 쓴다.
            cols = {r[1] for r in conn.execute('PRAGMA table_info(dock_daily_report)').fetchall()}
            for name, ddl in (
                ('svms_claim_token', 'ALTER TABLE dock_daily_report ADD COLUMN svms_claim_token TEXT'),
                ('svms_claimed_at', 'ALTER TABLE dock_daily_report ADD COLUMN svms_claimed_at TEXT'),
                ('svms_approved_by', 'ALTER TABLE dock_daily_report ADD COLUMN svms_approved_by TEXT'),
                ('svms_approved_revision', 'ALTER TABLE dock_daily_report ADD COLUMN svms_approved_revision INTEGER'),
                ('svms_approved_hash', 'ALTER TABLE dock_daily_report ADD COLUMN svms_approved_hash TEXT'),
                ('svms_result_json', 'ALTER TABLE dock_daily_report ADD COLUMN svms_result_json TEXT'),
            ):
                if name not in cols:
                    conn.execute(ddl)
            conn.execute('CREATE INDEX IF NOT EXISTS idx_dock_daily_svms_queue '
                         'ON dock_daily_report(svms_sync_status, svms_claimed_at)')
        except Exception as e:
            print(f'[auto_migrate] dock_daily_report SVMS queue 점검 건너뜀: {e}')

        # SVMS 입거(Dock) 후보 캐시. 맥 runner 가 채우고 사람이 그 중에서 DK_CD 를 고른다.
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(dock_daily_project)').fetchall()}
            for name, ddl in (
                ('svms_dock_candidates_json',
                 'ALTER TABLE dock_daily_project ADD COLUMN svms_dock_candidates_json TEXT'),
                ('svms_dock_synced_at',
                 'ALTER TABLE dock_daily_project ADD COLUMN svms_dock_synced_at TEXT'),
            ):
                if name not in cols:
                    conn.execute(ddl)
                    print('[auto_migrate] dock_daily_project.%s 추가됨' % name)
        except Exception as e:
            print(f'[auto_migrate] dock_daily_project SVMS dock 캐시 점검 건너뜀: {e}')

        # 입거 Daily 섹션의 **일자 스코프**(형 지시 2026-08-23).
        #
        # 🔴 `CREATE TABLE IF NOT EXISTS` 를 여기서 다시 쓴다. 위의 schema.sql 재적용이
        #    어떤 이유로든 건너뛰어졌으면(그 블록은 예외를 삼킨다) 아래 backfill 이
        #    "no such table" 로 죽는데, ALTER 는 이미 이 커넥션에 남아 마지막 commit 으로
        #    들어간다 -- 그 조합은 **scope 는 report 인데 membership 은 하나도 없는** 상태,
        #    즉 형이 만들어 둔 섹션이 전부 화면에서 사라지는 상태다.
        # 🔴 독립 try -- 앞 블록들과 운명을 묶지 않는다.
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS dock_daily_report_section (
                                report_id INTEGER NOT NULL,
                                section_key TEXT NOT NULL,
                                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                                PRIMARY KEY (report_id, section_key),
                                FOREIGN KEY (report_id) REFERENCES dock_daily_report(id) ON DELETE CASCADE)""")
            scols = {r[1] for r in conn.execute(
                'PRAGMA table_info(dock_daily_section_def)').fetchall()}
            if scols and 'scope' not in scols:
                conn.execute("ALTER TABLE dock_daily_section_def ADD COLUMN scope TEXT"
                             " NOT NULL DEFAULT 'project'")
                # 이미 만들어 둔 섹션은 오늘 **모든 일자에 보인다**. 그대로 유지한 뒤
                # 일자별로 지울 수 있게만 바꾼다 = 지금 있는 보고서 전부에 membership 을
                # 심고 scope 를 report 로 올린다. 그래서 화면은 그대로이고, 새로 만드는
                # 보고서에는 안 따라온다(그게 이번 지시의 핵심).
                #
                # 🔴 `sec_%` 만 옮긴다. 프로젝트 생성 때 고른 `egcs` 는 프로젝트 전체를
                #    뜻하는 선택이라 일자 스코프로 바꾸면 앞으로 생성되는 보고서에서
                #    조용히 사라진다. `LIKE 'sec_%'` 의 `_` 는 와일드카드라 ESCAPE 필수.
                conn.execute(r"UPDATE dock_daily_section_def SET scope='report'"
                             r" WHERE kind='special' AND section_key LIKE 'sec\_%' ESCAPE '\'")
                conn.execute("""INSERT OR IGNORE INTO dock_daily_report_section(report_id, section_key)
                                SELECT r.id, d.section_key
                                  FROM dock_daily_report r
                                  JOIN dock_daily_section_def d ON d.project_id=r.project_id
                                 WHERE d.kind='special' AND d.scope='report'""")
                print('[auto_migrate] dock_daily_section_def.scope 추가 + 기존 섹션 membership backfill')
        except Exception as e:
            print(f'[auto_migrate] dock_daily 섹션 일자 스코프 점검 건너뜀: {e}')

        conn.commit()
    finally:
        conn.close()


if __name__ == '__main__':
    init_runtime()
    if len(sys.argv) > 1 and sys.argv[1] == '--init-db':
        init_db(drop=True)
        sys.exit(0)

    if not os.path.exists(DATABASE):
        print('[INFO] DB 파일이 없어 자동 초기화합니다.')
        init_db(drop=False)
    else:
        _auto_migrate()

    # 개발 환경 — debug(Werkzeug 콘솔=원격 코드실행 위험)는 명시적으로 켤 때만.
    # 기본 off. 로컬 개발 시 TRMT_DEBUG=1 로 실행.
    debug = os.environ.get('TRMT_DEBUG') == '1'
    # threaded: 요청을 스레드로 병렬 처리(개발서버 단일요청 병목 해소, 임시 조치).
    # extra_files 는 더 필요 없다 — 모든 경계가 진짜 모듈이라 sys.modules 에 들어가고
    # reloader 가 기본으로 감시한다. exec 경계가 다시 생기면 그때 이 인자도 같이 살아나야
    # 하는데, 그런 일이 없도록 test_boundary_dependency_graph 가 exec 경계 0 을 고정한다.
    app.run(host='0.0.0.0', port=5000, debug=debug, threaded=True)

-- =============================================================
--  TRMT3 Ship Management System — Database Schema
--  SQLite 3
--  Tanker Management Team 3, Sinokor Shipmanagement
-- =============================================================

-- -------------------------------------------------------------
--  감독 (Supervisors)
--   · Daily 업무관리 탭 단위
--   · color 는 탭 닷 색상 (blue / teal / purple / coral / amber / gray)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supervisors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,         -- 예) 손차장
    display_order INTEGER NOT NULL DEFAULT 0,      -- 탭 노출 순서
    color         TEXT    NOT NULL DEFAULT 'blue', -- 탭 닷 색상
    email         TEXT,
    active        INTEGER NOT NULL DEFAULT 1,      -- 1=재직, 0=비활성
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- -------------------------------------------------------------
--  선박 (Vessels)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vessels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,         -- 예) KUWAIT PROSPERITY
    short_name    TEXT,                            -- 표시용 축약 예) KW PROSP
    vessel_type   TEXT,                            -- VLCC / AFRAMAX / CONTAINER 등
    imo           TEXT,
    flag          TEXT,
    class_society TEXT,                            -- BV / KR / LR / ABS / DNV / NK
    manager       TEXT,                            -- 관리사(선박관리사) 텍스트 지정
    manager_supervisor TEXT NOT NULL DEFAULT '',   -- 관리사 측 담당감독 이름(수동 입력)
    vsl_cd        TEXT,                            -- SSOT(P0): SVMS 4자 코드
    vt_vessel_id  INTEGER,                         -- SSOT(P0): vesseltracker 내부 vesselId
    aliases       TEXT,                            -- SSOT(P0): 구선명·표기 별칭 JSON 배열 문자열
    gross_tonnage TEXT,                            -- SVMS 선박상세 GT
    dead_weight   TEXT,                            -- SVMS 선박상세 SUMMER_DW(DWT fallback)
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- -------------------------------------------------------------
--  감독-선박 담당 매핑 (M:N)
--   · 한 선박을 여러 감독이 담당할 수도 있으므로 M:N
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supervisor_vessels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id INTEGER NOT NULL,
    vessel_id     INTEGER NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE CASCADE,
    FOREIGN KEY (vessel_id)     REFERENCES vessels(id)     ON DELETE CASCADE,
    UNIQUE (supervisor_id, vessel_id)
);

-- -------------------------------------------------------------
--  이슈 (Issues) — Daily 업무관리의 각 행
--   · description / action_plan 은 \n 으로 여러 줄 허용
--   · priority : Normal / Urgent / COC & Flag / Next DD
--   · status   : Open / InProgress / Closed
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id INTEGER NOT NULL,
    vessel_id     INTEGER NOT NULL,
    issue_date    TEXT    NOT NULL,                -- YYYY-MM-DD (작성일)
    due_date      TEXT,                            -- YYYY-MM-DD (마감일, NULL 허용)
    item_topic    TEXT    NOT NULL,                -- 이슈 제목
    description   TEXT,                            -- 상세 내용 (여러 줄)
    actions       TEXT    DEFAULT '[]',            -- JSON: [{date, progress, important}]
    priority      TEXT    NOT NULL DEFAULT 'Normal'
                  CHECK (priority IN ('Normal','Urgent','COC & Flag','Next DD')),
    status        TEXT    NOT NULL DEFAULT 'Open'
                  CHECK (status   IN ('Open','InProgress','Closed')),
    created_by    TEXT,                            -- 작성자 username
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id),
    FOREIGN KEY (vessel_id)     REFERENCES vessels(id)
);

-- -------------------------------------------------------------
--  mail_card — WF1+WF2 통합 (메일 1건 = 카드 1개)
--   · 한 카드에서 ① TRMT 이슈 등록(WF1) ② 회신 작성(WF2) 둘 다
--   · 회신: 손유석 한글지시 → 서버 Gemini 영문번역(스타일 하네스) → 맥미니 Outlook Draft
--   · 이슈/회신 독립 상태머신. 둘 다 종결(done/dismissed/na)이면 archive.
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mail_card (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    -- 원본 메일
    email_subject   TEXT,
    email_from      TEXT,
    email_date      TEXT,
    email_msg_id    TEXT,                            -- Outlook 메시지 id (dedup/회신 타겟)
    thread_key      TEXT,                            -- 스레드 upsert 키(폴더|정규화제목). 같은 스레드=1카드 갱신
    summary_ko      TEXT,                            -- 최근 메일 전문 한국어 번역(맥락)
    action_summary  TEXT,                            -- 현안 액션추가용 1~2문장 요약(이 메일 진행핵심)
    thread_summary_ko TEXT,                          -- 스레드 전체 1~2줄 요약(맨 위 표시)
    body_en         TEXT,                            -- 최근 메일 원문(영문, 번역 병기용)
    -- ① 이슈측 (WF1)
    issue_item      TEXT,                            -- 제안 item_topic
    issue_desc      TEXT,                            -- 제안 description (하우스스타일)
    issue_match_id  INTEGER,                         -- dedup 매칭 기존이슈(있으면 append 후보)
    issue_priority  TEXT    DEFAULT 'Normal',
    issue_vessel    TEXT,                            -- 승인 시 vessel 매칭용
    issue_supervisor TEXT,
    issue_status    TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (issue_status IN ('pending','registered','rejected','not_applicable')),
    issue_id        INTEGER,                         -- 등록 결과 연결
    -- ② 회신측 (WF2)
    reply_ko        TEXT,                            -- 손유석 한글 회신 지시(내용 정답)
    reply_style     TEXT,                            -- 간결/강경/정중 + 메모
    reply_en        TEXT,                            -- Gemini 번역 결과(영문, 서명 제외 저장)
    reply_en_at     TEXT,                            -- reply_en 최종 갱신시각(편집중 draft 방지 버전체크)
    reply_status    TEXT    NOT NULL DEFAULT 'none'
                    CHECK (reply_status IN ('none','needs_info','translated','draft_requested','draft_created','dismissed')),
    -- 카드 종합
    card_status     TEXT    NOT NULL DEFAULT 'active'
                    CHECK (card_status IN ('active','archived')),
    pending         INTEGER NOT NULL DEFAULT 0,      -- 보류(나중에 처리) 플래그: active 중 1=보류함으로 분리
    reject_reason   TEXT,
    decided_at      TEXT,
    decided_by      TEXT
);

-- -------------------------------------------------------------
--  첨부파일 (Attachments)
--   · 실제 파일은 static/uploads/ 에 stored_name 으로 저장
--   · 현장에서 핸드폰 사진 업로드 대비
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    INTEGER NOT NULL,
    filename    TEXT    NOT NULL,                  -- 원본 파일명
    stored_name TEXT    NOT NULL UNIQUE,           -- 저장 파일명 (UUID+ext)
    file_size   INTEGER,
    mime_type   TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (issue_id) REFERENCES issues(id) ON DELETE CASCADE
);

-- -------------------------------------------------------------
--  사용자 (Users) — 로그인용
--   · supervisor_id 가 세팅돼 있으면 해당 감독 탭을 기본으로 보여줌
--   · role : admin (감독 추가/삭제 권한) / member
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE,
    password_hash  TEXT    NOT NULL,
    display_name   TEXT,
    supervisor_id  INTEGER,
    role           TEXT    NOT NULL DEFAULT 'member'
                   CHECK (role IN ('admin','member')),
    app_scope      TEXT    NOT NULL DEFAULT 'business'
                   CHECK (app_scope IN ('business','family')),
    active         INTEGER NOT NULL DEFAULT 1,
    last_login_at  TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id)
);

-- -------------------------------------------------------------
--  인덱스
-- -------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_issues_supervisor  ON issues(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_issues_vessel      ON issues(vessel_id);
CREATE INDEX IF NOT EXISTS idx_issues_date        ON issues(issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_issues_due_date    ON issues(due_date);
CREATE INDEX IF NOT EXISTS idx_issues_status      ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_priority    ON issues(priority);
CREATE INDEX IF NOT EXISTS idx_attachments_issue  ON attachments(issue_id);
CREATE INDEX IF NOT EXISTS idx_sv_supervisor      ON supervisor_vessels(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_sv_vessel          ON supervisor_vessels(vessel_id);

-- -------------------------------------------------------------
--  우리자산 (iOS 전용) — 2인 가구 공유 자산 원장
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS family_asset_household (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    invite_code TEXT NOT NULL UNIQUE,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS family_asset_member (
    household_id INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('owner','member')),
    joined_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (household_id,user_id)
);
CREATE INDEX IF NOT EXISTS idx_family_asset_member_household
    ON family_asset_member(household_id);
CREATE TABLE IF NOT EXISTS family_asset_entry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id  INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK(kind IN ('income','cash','saving','stock','property','loan','other')),
    name          TEXT NOT NULL,
    amount        INTEGER NOT NULL CHECK(amount >= 0),
    owner_mode    TEXT NOT NULL CHECK(owner_mode IN ('member','joint')),
    owner_user_id INTEGER REFERENCES users(id),
    joint_share   INTEGER NOT NULL DEFAULT 50 CHECK(joint_share BETWEEN 0 AND 100),
    institution   TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_by    INTEGER NOT NULL REFERENCES users(id),
    updated_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK((owner_mode='member' AND owner_user_id IS NOT NULL) OR
          (owner_mode='joint' AND owner_user_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_family_asset_entry_household
    ON family_asset_entry(household_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS family_asset_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id  INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
    asset_id       INTEGER,
    action         TEXT NOT NULL CHECK(action IN ('create','update','delete')),
    asset_name     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    amount_before  INTEGER,
    amount_after   INTEGER,
    changed_by     INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_family_asset_history_household
    ON family_asset_history(household_id,id DESC);
CREATE TABLE IF NOT EXISTS family_asset_monthly_snapshot (
    household_id  INTEGER NOT NULL REFERENCES family_asset_household(id) ON DELETE CASCADE,
    month          TEXT NOT NULL,
    total_assets   INTEGER NOT NULL,
    total_debt     INTEGER NOT NULL,
    net_worth      INTEGER NOT NULL,
    captured_at    TEXT NOT NULL DEFAULT (datetime('now','+9 hours')),
    PRIMARY KEY (household_id,month)
);
CREATE INDEX IF NOT EXISTS idx_family_asset_snapshot_household
    ON family_asset_monthly_snapshot(household_id,month DESC);

-- =============================================================
--  Dock Daily Report (입거 준비 일일보고)
--  완료보고서(dock_reports)와 분리된 draft/revision/source 도메인
-- =============================================================
CREATE TABLE IF NOT EXISTS dock_daily_project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id INTEGER NOT NULL,
    vsl_cd TEXT,
    imo TEXT,
    title TEXT NOT NULL,
    berthing_date TEXT,
    dock_in_date TEXT,
    dock_out_date TEXT,
    departure_date TEXT,
    active_from TEXT,
    active_to TEXT,
    auto_generate INTEGER NOT NULL DEFAULT 0 CHECK(auto_generate IN (0,1)),
    -- Dock Manager uses opaque string identifiers (for example v_abc123),
    -- not TRMT's integer vessel primary keys.
    drydock_primary_vessel_id TEXT,
    drydock_source_vessel_ids_json TEXT NOT NULL DEFAULT '[]',
    svms_dk_cd TEXT,
    -- SVMS 입거(Dock) 후보 캐시.  TRMT 서버는 사내망 SVMS 에 못 붙으므로 맥 runner 가
    -- `SP_GET_DOCK` 결과를 여기에 넣어 주고, 사람은 그 목록에서 `svms_dk_cd` 를 고른다.
    svms_dock_candidates_json TEXT,
    svms_dock_synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_dock_daily_project_vessel ON dock_daily_project(vessel_id);
CREATE INDEX IF NOT EXISTS idx_dock_daily_project_active ON dock_daily_project(active_from, active_to, auto_generate);

CREATE TABLE IF NOT EXISTS dock_daily_section_def (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    section_key TEXT NOT NULL,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL CHECK(kind IN ('fixed','special')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    -- 어느 일자에 나타나는지. 'project' = 이 프로젝트의 모든 보고서(고정 섹션과
    -- 프로젝트 생성 때 고른 EGCS 류), 'report' = `dock_daily_report_section` 에
    -- 행이 있는 일자에만(형 지시 2026-08-23: 그날 이슈로 추가한 섹션이 다른
    -- 일자에까지 생기고 그 일자에서 지울 수도 없었다).
    scope TEXT NOT NULL DEFAULT 'project' CHECK(scope IN ('project','report')),
    UNIQUE(project_id, section_key),
    FOREIGN KEY (project_id) REFERENCES dock_daily_project(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dock_daily_section_project ON dock_daily_section_def(project_id, sort_order);

-- 일자 스코프 섹션의 소속. 행이 있는 보고서에만 그 섹션이 보인다.
-- 만든 날에만 행이 생기므로 **만든 날짜 이전 보고서에는 구조적으로 없다**.
-- 다른 일자로는 '이전 일자 가져오기'(copy-from)가 옮기고, 각 일자에서 따로 지운다.
CREATE TABLE IF NOT EXISTS dock_daily_report_section (
    report_id INTEGER NOT NULL,
    section_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (report_id, section_key),
    FOREIGN KEY (report_id) REFERENCES dock_daily_report(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dock_daily_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'auto_draft' CHECK(status IN ('auto_draft','editing','final')),
    revision INTEGER NOT NULL DEFAULT 1,
    auto_snapshot_json TEXT NOT NULL DEFAULT '{}',
    email_subject TEXT,
    email_intro TEXT,
    safety_footer TEXT,
    svms_dk_seq TEXT,
    svms_sync_status TEXT NOT NULL DEFAULT 'preview_only',
    svms_synced_at TEXT,
    svms_readback_hash TEXT,
    svms_claim_token TEXT,
    svms_claimed_at TEXT,
    svms_approved_by TEXT,
    svms_approved_revision INTEGER,
    svms_approved_hash TEXT,
    svms_result_json TEXT,
    source_changed_after_final INTEGER NOT NULL DEFAULT 0 CHECK(source_changed_after_final IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(project_id, report_date),
    FOREIGN KEY (project_id) REFERENCES dock_daily_project(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dock_daily_report_project_date ON dock_daily_report(project_id, report_date DESC);

CREATE TABLE IF NOT EXISTS dock_daily_block (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    section_key TEXT NOT NULL,
    parent_id INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0,
    block_type TEXT NOT NULL CHECK(block_type IN ('item','paragraph','table','image')),
    content_json TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'manual' CHECK(origin IN ('manual','dock_auto')),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK(manual_override IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (report_id) REFERENCES dock_daily_report(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES dock_daily_block(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_dock_daily_block_report ON dock_daily_block(report_id, section_key, sort_order, id);

CREATE TABLE IF NOT EXISTS dock_daily_source_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    block_id INTEGER,
    source_system TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_subkey TEXT NOT NULL,
    source_updated_at TEXT,
    source_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    missing_at TEXT,
    UNIQUE(report_id, source_system, source_table, source_id, source_subkey),
    FOREIGN KEY (report_id) REFERENCES dock_daily_report(id) ON DELETE CASCADE,
    FOREIGN KEY (block_id) REFERENCES dock_daily_block(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_dock_daily_source_report ON dock_daily_source_link(report_id, source_hash);

CREATE TABLE IF NOT EXISTS dock_daily_attachment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    block_id INTEGER,
    stored_name TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (report_id) REFERENCES dock_daily_report(id) ON DELETE CASCADE,
    FOREIGN KEY (block_id) REFERENCES dock_daily_block(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_dock_daily_attachment_report ON dock_daily_attachment(report_id, deleted_at);

CREATE TABLE IF NOT EXISTS dock_daily_report_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(report_id, revision),
    FOREIGN KEY (report_id) REFERENCES dock_daily_report(id) ON DELETE CASCADE
);

-- =============================================================
--  Condition Survey 모듈
-- =============================================================

-- 분기별 수검 헤더 (선박 × 연도 × 분기 unique)
CREATE TABLE IF NOT EXISTS cs_surveys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id       INTEGER NOT NULL,
    year            INTEGER NOT NULL,
    quarter         INTEGER NOT NULL CHECK (quarter IN (1,2,3,4)),
    vendor          TEXT,                 -- AALMAR / IDWAL / OTHERS / 자유 입력
    management      TEXT,
    inspection_date TEXT,                 -- YYYY-MM-DD
    overall_remark  TEXT,                 -- 분기별 수검 전체 리마크
    manual_defect_count      INTEGER,      -- 수동 입력 (NULL이면 자동 카운트 사용)
    manual_observation_count INTEGER,
    manual_close_count       INTEGER,
    created_by      TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (vessel_id, year, quarter),
    FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cs_surveys_vessel_year ON cs_surveys(vessel_id, year);

-- 세부 항목 (Defect / Observation)
CREATE TABLE IF NOT EXISTS cs_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id   INTEGER NOT NULL,
    category    TEXT    NOT NULL CHECK (category IN ('Defect','Observation')),
    no          INTEGER NOT NULL,         -- category 내 자동 넘버링
    item        TEXT,                     -- 항목명 (간단)
    description TEXT,                     -- 상세 내용
    remark      TEXT,                     -- 비고
    status      TEXT    NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Closed')),
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (survey_id) REFERENCES cs_surveys(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cs_findings_survey ON cs_findings(survey_id, category, no);

-- Condition Survey 첨부파일
CREATE TABLE IF NOT EXISTS cs_attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id   INTEGER NOT NULL,
    filename    TEXT    NOT NULL,
    stored_name TEXT    NOT NULL UNIQUE,
    file_size   INTEGER,
    mime_type   TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (survey_id) REFERENCES cs_surveys(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cs_attachments_survey ON cs_attachments(survey_id);

-- ═════════════════════════════════════════════════════════════
--  Vetting Status (비정기 검사 — 선박당 0~N건)
--  적용 선박: VLCC, AFRAMAX, LR, MR (CNTR 제외)
-- ═════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS vettings (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id                INTEGER NOT NULL,
    report_number            TEXT,
    inspection_date          TEXT,                 -- YYYY-MM-DD (검사일 기준 연도 필터)
    inspection_company       TEXT,
    inspector                TEXT,
    port                     TEXT,
    operation                TEXT,  -- (구) 사용 안 함, 호환 위해 유지
    sire_type                TEXT CHECK (sire_type IN ('Idle','Bunkering','Discharge') OR sire_type IS NULL OR sire_type = ''),
    valid                    TEXT,                 -- 상태: Next Plan / Last Result (자유 텍스트)
    overall_remark           TEXT,
    manual_observation_count INTEGER,              -- NULL이면 자동 카운트
    manual_open_count        INTEGER,
    manual_close_count       INTEGER,
    svms_full_report_yn      TEXT CHECK (svms_full_report_yn IN ('Y','N') OR svms_full_report_yn IS NULL),
    svms_close_report_yn     TEXT CHECK (svms_close_report_yn IN ('Y','N') OR svms_close_report_yn IS NULL),
    svms_report_uploaded_yn  TEXT CHECK (svms_report_uploaded_yn IN ('Y','N') OR svms_report_uploaded_yn IS NULL),
    svms_status_synced_at    TEXT,
    created_by               TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vettings_vessel_date ON vettings(vessel_id, inspection_date DESC);

-- Vetting Findings (단일 카테고리: Observation)
CREATE TABLE IF NOT EXISTS vt_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vetting_id  INTEGER NOT NULL,
    no          INTEGER NOT NULL,
    item        TEXT,
    description TEXT,
    remark      TEXT,
    user_remark TEXT NOT NULL DEFAULT '',   -- 자율 입력 Remark (번역요약과 별개)
    full_report_remark TEXT NOT NULL DEFAULT '', -- 마지막 Full report 자동 Remark (멱등 교체용, API 비노출)
    priority    INTEGER NOT NULL DEFAULT 0,  -- 1=중요(Priority 체크), 0=일반
    status      TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Closed')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (vetting_id) REFERENCES vettings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vt_findings_vetting ON vt_findings(vetting_id, no);

-- SIRE Full report 자동반영 실행 이력(전건 변경 전/후 스냅샷).
CREATE TABLE IF NOT EXISTS vt_full_report_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    vetting_id    INTEGER NOT NULL,
    report_number TEXT NOT NULL,
    file_sha256   TEXT NOT NULL,
    filename      TEXT,
    before_json   TEXT NOT NULL,
    after_json    TEXT NOT NULL,
    applied_by    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (vetting_id) REFERENCES vettings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vt_full_report_audit_vetting
    ON vt_full_report_audit(vetting_id, created_at DESC);

-- Vetting Attachments
CREATE TABLE IF NOT EXISTS vt_attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vetting_id  INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    file_size   INTEGER,
    mime_type   TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    source      TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','svms')),
    source_type TEXT CHECK (source_type IN ('initial','close') OR source_type IS NULL),
    external_sire_cd TEXT,
    external_file_id TEXT,
    sha256      TEXT,
    synced_at   TEXT,
    inactive_at TEXT,
    FOREIGN KEY (vetting_id) REFERENCES vettings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vt_attachments_vetting ON vt_attachments(vetting_id);

-- ═════════════════════════════════════════════════════════════
--  Calendar Events (일정 모듈)
-- ═════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS calendar_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id   INTEGER,                            -- NULL = 공용/전사
    vessel_id       INTEGER,                            -- 선박 연결 (선택)
    title           TEXT NOT NULL,
    start_date      TEXT NOT NULL,                      -- YYYY-MM-DD
    end_date        TEXT,                               -- NULL = 단일일자
    all_day         INTEGER NOT NULL DEFAULT 1,         -- 1=종일, 0=시간 지정
    start_time      TEXT,                               -- HH:MM (all_day=0일 때만)
    end_time        TEXT,
    category        TEXT,                               -- 회의/출장/ETA/ETD/휴가/DD/검사/기타
    color           TEXT,                               -- gray/red/amber/yellow/green/blue/purple/pink
    location        TEXT,
    notes           TEXT,
    completed       INTEGER NOT NULL DEFAULT 0,         -- 1=완료(모든 미러 화면 취소선)
    leave_type      TEXT CHECK (leave_type IN ('annual','half','quarter') OR leave_type IS NULL),
    -- 다른 모듈에서 가져온 경우 (Phase B에서 사용)
    source_type     TEXT,                               -- 'issue'|'cs'|'vetting'|'manual'(default)|null
    source_id       INTEGER,                            -- 원본 row id
    created_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE SET NULL,
    FOREIGN KEY (vessel_id)     REFERENCES vessels(id)     ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_events_date ON calendar_events(start_date);
CREATE INDEX IF NOT EXISTS idx_cal_events_supervisor ON calendar_events(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_cal_events_source ON calendar_events(source_type, source_id);

CREATE TABLE IF NOT EXISTS calendar_leave_allowances (
    supervisor_id   INTEGER NOT NULL,
    year            INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    days            REAL NOT NULL CHECK (days >= 0 AND days <= 365),
    manual_used     REAL NOT NULL DEFAULT 0 CHECK (manual_used >= 0 AND manual_used <= 365),
    updated_by      TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (supervisor_id, year),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE CASCADE
);


-- ═════════════════════════════════════════════════════════════
--  Dry Dock Report (입거수리 완료 보고)
-- ═════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS dock_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id       INTEGER NOT NULL,
    supervisor_id   INTEGER,
    title           TEXT NOT NULL,                       -- 보고서 제목
    dock_no         TEXT,                                -- "4차 중간", "특별검사" 등
    shipyard        TEXT,                                -- 조선소명
    period_start    TEXT,                                -- YYYY-MM-DD
    period_end      TEXT,
    imo_no          TEXT,
    gross_tonnage   TEXT,
    dead_weight     TEXT,
    -- 결재선 (이름만 저장, 도장은 출력 시 비워서 사람이 채움)
    approval_drafter   TEXT,
    approval_team_lead TEXT,
    approval_director  TEXT,
    approval_ceo       TEXT,

    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','done')),
    -- 템플릿 라이브러리 — 1이면 보고서가 아니라 재사용용 템플릿
    is_template     INTEGER NOT NULL DEFAULT 0,
    template_name   TEXT,                                -- is_template=1일 때 노출 이름

    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    created_by      TEXT,
    FOREIGN KEY (vessel_id)     REFERENCES vessels(id)     ON DELETE RESTRICT,
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_dock_reports_vessel  ON dock_reports(vessel_id);
CREATE INDEX IF NOT EXISTS idx_dock_reports_status  ON dock_reports(status, is_template);
CREATE INDEX IF NOT EXISTS idx_dock_reports_updated ON dock_reports(updated_at DESC);

-- 섹션 (목차 항목) — 계층 구조 (parent_id NULL이면 1단계)
CREATE TABLE IF NOT EXISTS dock_report_sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER NOT NULL,
    parent_id       INTEGER,                             -- NULL이면 최상위
    title           TEXT NOT NULL,
    display_order   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (report_id) REFERENCES dock_reports(id)         ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES dock_report_sections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dock_sections_report ON dock_report_sections(report_id, display_order);
CREATE INDEX IF NOT EXISTS idx_dock_sections_parent ON dock_report_sections(parent_id, display_order);

-- 블록 (각 섹션의 내용) — paragraph / bullet_list / table / image
CREATE TABLE IF NOT EXISTS dock_report_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id      INTEGER NOT NULL,
    block_type      TEXT NOT NULL
                    CHECK(block_type IN ('paragraph','bullet_list','table','image')),
    content_json    TEXT NOT NULL,                       -- 타입별 데이터
    -- block_type별 content_json 스키마:
    --  paragraph   : {"text":"..."}
    --  bullet_list : {"items":["...", "..."]}
    --  table       : {"headers":["..."], "rows":[["..."], ...]}
    --  image       : {"filename":"...", "caption":"...", "width_pct":100}
    display_order   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (section_id) REFERENCES dock_report_sections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dock_blocks_section ON dock_report_blocks(section_id, display_order);


-- ═════════════════════════════════════════════════════════════
--  Boarding Report (방선보고서 + Defect List 통합)
-- ═════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS boarding_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id       INTEGER NOT NULL,
    supervisor_id   INTEGER,
    title           TEXT NOT NULL,                       -- 보고서 제목
    -- 방선 기본 정보 (양식 헤더 표용)
    port            TEXT,                                -- 방선 항구
    boarding_start  TEXT,                                -- YYYY-MM-DD (방선 시작일)
    boarding_end    TEXT,                                -- YYYY-MM-DD (방선 종료일)
    master_name     TEXT,                                -- Master 이름
    master_board_date TEXT,                              -- Master 승선일
    chief_eng_name  TEXT,                                -- C/E 이름
    chief_eng_board_date TEXT,                           -- C/E 승선일
    sv_checklist_score TEXT,                             -- Ship-Visit Checklist Score
    -- 결재선
    approval_drafter   TEXT,
    approval_team_lead TEXT,
    approval_director  TEXT,
    approval_ceo       TEXT,

    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','done')),
    is_template     INTEGER NOT NULL DEFAULT 0,
    template_name   TEXT,

    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    created_by      TEXT,
    FOREIGN KEY (vessel_id)     REFERENCES vessels(id)     ON DELETE RESTRICT,
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_boarding_reports_vessel  ON boarding_reports(vessel_id);
CREATE INDEX IF NOT EXISTS idx_boarding_reports_status  ON boarding_reports(status, is_template);
CREATE INDEX IF NOT EXISTS idx_boarding_reports_updated ON boarding_reports(updated_at DESC);

-- 섹션 (목차 항목) — 계층 구조
CREATE TABLE IF NOT EXISTS boarding_report_sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       INTEGER NOT NULL,
    parent_id       INTEGER,
    title           TEXT NOT NULL,
    display_order   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (report_id) REFERENCES boarding_reports(id)         ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES boarding_report_sections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_boarding_sections_report ON boarding_report_sections(report_id, display_order);
CREATE INDEX IF NOT EXISTS idx_boarding_sections_parent ON boarding_report_sections(parent_id, display_order);

-- 블록 — paragraph / bullet_list / table / image + info_table / defect_table
CREATE TABLE IF NOT EXISTS boarding_report_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id      INTEGER NOT NULL,
    block_type      TEXT NOT NULL
                    CHECK(block_type IN ('paragraph','bullet_list','table','image',
                                          'info_table','defect_table')),
    content_json    TEXT NOT NULL,
    -- info_table   : {"rows":[{"label":"Vessel","value":"MARITIME GLORY"}, ...]}
    -- defect_table : {"items":[{"item":"...","desc":"...","fix":"...","risk":"L/M/H","images":[...]}]}
    display_order   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (section_id) REFERENCES boarding_report_sections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_boarding_blocks_section ON boarding_report_blocks(section_id, display_order);


-- ═════════════════════════════════════════════════════════════════
--  출장 경비 (Business Trip Expense) — 영수증 추출/증빙
-- ═════════════════════════════════════════════════════════════════

-- 출장 카드 (일정당 1개)
CREATE TABLE IF NOT EXISTS biz_trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supervisor_id   INTEGER,                       -- 담당(소유자)
    title           TEXT NOT NULL,                 -- 출장명
    trip_start      TEXT,                          -- 기간 시작 (YYYY-MM-DD)
    trip_end        TEXT,                          -- 기간 종료 (YYYY-MM-DD)
    corp_cards      TEXT,                          -- 법인카드 번호 목록 (JSON 배열 문자열)
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','settled')),   -- 진행 중 / 정산완료
    created_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (supervisor_id) REFERENCES supervisors(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_biz_trips_sup     ON biz_trips(supervisor_id);
CREATE INDEX IF NOT EXISTS idx_biz_trips_status  ON biz_trips(status);
CREATE INDEX IF NOT EXISTS idx_biz_trips_updated ON biz_trips(updated_at DESC);

-- 영수증 (표의 한 줄 = 1건)
CREATE TABLE IF NOT EXISTS biz_receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         INTEGER NOT NULL,
    image_filename  TEXT,                          -- 증빙 사진 파일명 (static/uploads/receipt/)
    image_url       TEXT,
    vendor          TEXT,                          -- 상호 (추출, 갤러리 캡션용)
    cost_type       TEXT,                          -- Bz Trip Cost Type: 교통비/숙박비/접대비/복리후생비/기타
    use_type        TEXT,                          -- Cost Use Type: 법인카드/개인카드/현금
    occur_date      TEXT,                          -- Occur Date (필수, YYYY-MM-DD, 추출)
    card_no         TEXT,                          -- Bz Card No
    remark          TEXT,                          -- Remarks (직접입력)
    currency        TEXT,                          -- Currency Code (필수, 추출, e.g. KRW/CNY/USD)
    amount          REAL,                          -- Occur Amount (필수, 추출)
    extracted_raw   TEXT,                          -- Haiku 원본 JSON (감사/디버그용)
    display_order   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (trip_id) REFERENCES biz_trips(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_biz_receipts_trip ON biz_receipts(trip_id, display_order, id);

-- =============================================================
--  CLASS STATUS (선급 Class Status Report 업로드/추출)
--   · 선박당 "최신 스냅샷 1개"만 유지 (UNIQUE vessel_id)
--   · 미매칭(선명 매칭 실패) 업로드는 vessel_id NULL 로 별도 보관
--     (SQLite UNIQUE 컬럼은 NULL 다중 허용)
-- =============================================================
CREATE TABLE IF NOT EXISTS class_status (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id        INTEGER,                       -- 매칭된 선박 (미매칭이면 NULL)
    vessel_name_raw  TEXT,                          -- 보고서에서 읽은 선명 원문
    class_society    TEXT,                          -- DNV / BV / KR / ABS / LR / NK ...
    report_date      TEXT,                          -- 보고서 발행일 (YYYY-MM-DD)
    source_filename  TEXT,                          -- 업로드 원본 파일명
    source_path      TEXT,                           -- 보관된 원본 파일 경로(선박별 최신만)
    uploaded_by      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (vessel_id),
    FOREIGN KEY (vessel_id) REFERENCES vessels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_class_status_vessel ON class_status(vessel_id);

-- 개별 지적/기국 항목 (Open 케이스만)
CREATE TABLE IF NOT EXISTS class_status_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cs_id        INTEGER NOT NULL,                  -- class_status.id
    category     TEXT NOT NULL CHECK (category IN ('COC','STATUTORY')),  -- 선급지적 / 기국
    no           INTEGER NOT NULL DEFAULT 0,        -- category 내 번호
    issued_date  TEXT,                              -- Issued / 발행일
    description  TEXT,                              -- 원문 그대로
    due_date     TEXT,                              -- Due / 마감일
    remark       TEXT,                              -- 한글 음슴체 요약
    action_taken TEXT NOT NULL DEFAULT '',          -- 조치사항(손유석 수동입력, 스냅샷 교체에도 description 매칭으로 유지)
    importance   TEXT NOT NULL DEFAULT '',          -- 중요도(수동): '' / High / Mid / Low
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (cs_id) REFERENCES class_status(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_class_status_items_cs ON class_status_items(cs_id, category, no);

-- 회의록 STT job queue (Phase 0a) — 웹/앱 업로드 → Mac 워커 폴 변환 → 표시
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
    lang          TEXT NOT NULL DEFAULT 'auto',      -- 변환 언어(auto|ko|en) — auto=자동감지
    audio_deleted INTEGER NOT NULL DEFAULT 0,        -- 원본 오디오 삭제됨(transcript는 보존)
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    claim_token   TEXT,                              -- 처리중 워커 클레임 토큰(CAS)
    claimed_at    TEXT,                              -- processing 진입 시각(lease 만료 판정)
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_stt_job_owner ON stt_job(owner, id);
CREATE INDEX IF NOT EXISTS idx_stt_job_status ON stt_job(status, id);

-- SOA automation group SSOT (user-editable scheduling partition; category→owner is code-side constant)
CREATE TABLE IF NOT EXISTS soa_group (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE
               CHECK (length(key) BETWEEN 1 AND 8 AND key NOT GLOB '*[^A-Z0-9]*'),
    label      TEXT NOT NULL,
    category   TEXT NOT NULL CHECK (category IN ('silver','skrt')),
    mode       TEXT NOT NULL DEFAULT 'explicit' CHECK (mode IN ('explicit','dynamic_owner')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS soa_group_vessel (
    group_id INTEGER NOT NULL REFERENCES soa_group(id),
    vsl_cd   TEXT NOT NULL CHECK (vsl_cd GLOB '[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'),
    PRIMARY KEY (group_id, vsl_cd)
);
CREATE INDEX IF NOT EXISTS idx_soa_group_vessel_cd ON soa_group_vessel(vsl_cd);
CREATE TABLE IF NOT EXISTS soa_vessel_owner (
    vsl_cd        TEXT PRIMARY KEY CHECK (vsl_cd GLOB '[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'),
    owner_comp_id TEXT NOT NULL,
    updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

-- SOA manual review inbox (snapshot/case/line/attachment/audit)
CREATE TABLE IF NOT EXISTS soa_review_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT,
    source           TEXT NOT NULL DEFAULT 'soa_manual_review',
    scope_json       TEXT,
    captured_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expires_at       TEXT,
    case_count       INTEGER NOT NULL DEFAULT 0,
    line_count       INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    summary_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_soa_review_snapshot_captured ON soa_review_snapshot(captured_at DESC);

CREATE TABLE IF NOT EXISTS soa_review_case (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id        INTEGER REFERENCES soa_review_snapshot(id) ON DELETE SET NULL,
    sx_cd              TEXT NOT NULL UNIQUE,
    -- SVMS header STATUS 원본을 그대로 보관한다(R=SM 반려 등). 좁은 화이트리스트로 막으면
    -- 상태가 바뀐 SOA의 snapshot ingest가 영구 실패해 로컬이 낡은 채 굳는다.
    -- 쓰기 권한 판정은 앱의 editable(D/S) 화이트리스트가 담당(fail-closed).
    status             TEXT NOT NULL CHECK (status GLOB '[A-Z]' OR status GLOB '[A-Z][A-Z]'),
    sl_tp              TEXT,
    dept_nm            TEXT,
    owner_comp_id      TEXT,
    owner_label        TEXT,
    vsl_cd             TEXT,
    vsl_nm             TEXT,
    sl_dm              TEXT,
    subj               TEXT,
    amt                REAL,
    cur_cd             TEXT,
    source_all_confirmed INTEGER NOT NULL DEFAULT 0,
    fresh_until        TEXT,
    draft_version      INTEGER NOT NULL DEFAULT 1,
    draft_dirty        INTEGER NOT NULL DEFAULT 0,
    queued_action      TEXT,
    queued_run_id      TEXT,
    queued_at          TEXT,
    last_action_at     TEXT,
    last_action_result TEXT,
    raw_case           TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_soa_review_case_status ON soa_review_case(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_soa_review_case_snapshot ON soa_review_case(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_soa_review_case_queue ON soa_review_case(queued_run_id);

CREATE TABLE IF NOT EXISTS soa_review_line (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id             INTEGER NOT NULL REFERENCES soa_review_case(id) ON DELETE CASCADE,
    sx_seq              TEXT,
    line_no             INTEGER NOT NULL DEFAULT 0,
    soa_tp              TEXT,
    soa_opex_tp         TEXT,
    exp_cd              TEXT,
    exp_nm              TEXT,
    cur_cd              TEXT,
    soa_amt             REAL,
    amt_usd             REAL,
    inv_no              TEXT,
    file_ref_no         TEXT,
    ref_no              TEXT,
    vendor_nm           TEXT,
    source_hash         TEXT NOT NULL,
    immutable_hash      TEXT,
    machine_state       TEXT,
    machine_reason      TEXT,
    exception           INTEGER NOT NULL DEFAULT 0,
    source_subj         TEXT,
    source_rmk          TEXT,
    source_cfm_yn       TEXT NOT NULL DEFAULT 'N',
    source_rjt_yn       TEXT NOT NULL DEFAULT 'N',
    source_rjt_rmk      TEXT,
    source_status2      TEXT,
    source_status_rmk2  TEXT,
    draft_subj          TEXT,
    draft_rmk           TEXT,
    draft_cfm_yn        TEXT,
    draft_rjt_yn        TEXT,
    draft_rjt_rmk       TEXT,
    raw_line            TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(case_id, sx_seq)
);
CREATE INDEX IF NOT EXISTS idx_soa_review_line_case ON soa_review_line(case_id, line_no, id);

CREATE TABLE IF NOT EXISTS soa_review_attachment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      INTEGER NOT NULL REFERENCES soa_review_case(id) ON DELETE CASCADE,
    line_id      INTEGER REFERENCES soa_review_line(id) ON DELETE CASCADE,
    upload_key   TEXT UNIQUE,
    slot         INTEGER NOT NULL DEFAULT 0,
    file_name    TEXT NOT NULL,
    mime_type    TEXT,
    byte_size    INTEGER,
    sha256       TEXT,
    stored_name  TEXT,
    file_ref_no  TEXT,
    expires_at   TEXT,
    uploaded_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_soa_review_attachment_case ON soa_review_attachment(case_id, line_id, slot);

CREATE TABLE IF NOT EXISTS soa_review_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER REFERENCES soa_review_case(id) ON DELETE CASCADE,
    snapshot_id INTEGER REFERENCES soa_review_snapshot(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    actor       TEXT,
    run_id      TEXT,
    ok          INTEGER,
    detail_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_soa_review_audit_case ON soa_review_audit(case_id, created_at DESC);

-- ═══════════════════════════════════════════════════════════════
--  iOS 푸시알림 (APNs)
-- ═══════════════════════════════════════════════════════════════
-- 디바이스 토큰. token UNIQUE = 같은 폰 재설치/토큰갱신 시 행이 늘지 않게.
-- env: production(Ad Hoc·App Store 서명) / sandbox(Xcode Debug 설치).
--   🔴 환경이 틀리면 APNs 가 400 BadDeviceToken 으로 조용히 거절 → 앱이 빌드구성으로 알려준다.
CREATE TABLE IF NOT EXISTS ios_device (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    env          TEXT NOT NULL DEFAULT 'production',
    app_ver      TEXT,
    device_name  TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    dead_reason  TEXT,                     -- 비활성 사유(Unregistered 등). 일시실패로는 안 끔
    prefs        TEXT,                     -- 알림종류 on/off JSON {kind: 0|1}
    last_push_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ios_device_user ON ios_device(user_id, active);

-- 발송 원장. event_key UNIQUE = 중복발송 차단(폴러가 같은 변화를 재관측해도 1회만).
-- 🔴 INSERT 성공을 발송 자격으로 쓰는 2단 커밋: 먼저 예약(claim) → 발송 → 결과 기록.
CREATE TABLE IF NOT EXISTS push_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key  TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    title      TEXT,
    body       TEXT,
    link       TEXT,
    sent_n     INTEGER NOT NULL DEFAULT 0,
    fail_n     INTEGER NOT NULL DEFAULT 0,
    detail     TEXT,
    -- 화면에서만 감춘 시각. 🔴 행을 **지우지 않는 이유**: event_key 가 중복발송 차단 claim 이라
    -- 하드 삭제하면 지운 직후 같은 이벤트가 다시 발송된다(캘린더 슬롯 재발송·outbox 재시도).
    hidden_at  TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_push_log_kind ON push_log(kind, created_at DESC);
-- 🔴 `hidden_at` partial index 는 **여기 두지 않는다**. 이 파일은 기존 DB 에도 통째로 재적용되는데,
--    컬럼이 아직 없는 구버전 DB 에서 그 index 문이 "no such column" 으로 터지면 executescript 가
--    거기서 멈춰 **뒤쪽 schema 문이 전부 스킵**된다. 컬럼 ALTER 뒤에 만들어야 안전하므로
--    `app.py::_auto_migrate()` 가 만든다(`idx_push_log_visible`).

-- 🔴 발송 대기함(outbox) — "상태는 전이됐는데 알림은 못 갔다" 를 막는 durable queue.
--   푸시 판정은 **DB 의 이전값과 새값을 비교**해서 나온다. 그래서 행을 갱신한 뒤에 발송이 실패하면
--   다음 폴은 "변화 없음" 으로 보고 그 알림은 **영구 미탐**이 된다(APNs 일시장애·프로세스 종료).
--   그래서 판정 즉시 여기 먼저 적재하고(같은 요청에서 dock_procure UPDATE 보다 **앞**), 발송에
--   성공해야 지운다. 프로세스가 중간에 죽어도 다음 sync 가 이 표를 비우며 이어받는다.
--   push_log(=중복발송 차단 원장)와 역할이 다르다: 저건 "이미 보냈나", 이건 "아직 못 보냈나".
CREATE TABLE IF NOT EXISTS push_outbox (
    event_key   TEXT PRIMARY KEY,           -- push_log 와 같은 키 → 재시도해도 중복발송 안 됨
    kind        TEXT NOT NULL,
    title       TEXT,
    body        TEXT,
    link        TEXT,
    collapse_id TEXT,
    tries       INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 🔴 클라이언트 재전송 중복방지 원장(오프라인 보관함 → 재전송). 네이티브 앱이 오프라인에서
--   보관한 쓰기를 연결 복구 후 다시 쏘는데, "서버는 저장됐는데 응답만 못 받은" 경우가 반드시 생긴다
--   (기내모드 해제 직후 링크 플랩·타임아웃). 그때 그냥 재전송하면 현안·영수증이 **두 번 생긴다**.
--   그래서 앱이 요청마다 고정 `X-Idempotency-Key` 를 붙이고, 서버는 (user_id, key) 로 여기에
--   claim 을 박은 뒤 성공응답을 저장해 두 번째 요청엔 그 응답을 그대로 되돌려준다.
--   status: in_progress=처리중 / done=성공응답 보관 / unknown=처리 중 죽어서 결과 모름(자동 재실행 금지).
--   ⚠️ 4xx 는 행을 지운다(클라 잘못 = 아무것도 안 바뀜 → 고쳐서 재전송이 정상 흐름).
--      5xx 는 **지우지 않고 unknown 으로 남긴다** — 뷰가 도중에 죽어 일부 커밋됐을 수 있어
--      자동 재실행이 곧 이중집행이다. 구분해서 다루지 않으면 둘 중 하나가 반드시 사고가 된다.
CREATE TABLE IF NOT EXISTS client_idem (
    user_id     INTEGER NOT NULL,
    idem_key    TEXT    NOT NULL,
    method      TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'in_progress',
    code        INTEGER,
    body        TEXT,
    content_type TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (user_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_client_idem_created ON client_idem(created_at);

-- 일반수리 + Dock 수리 공용 신청서. SVMS DRAFT 저장 전까지만 편집 가능하다.
CREATE TABLE IF NOT EXISTS repair_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_request_id TEXT UNIQUE,
    vessel_id INTEGER NOT NULL REFERENCES vessels(id),
    vsl_cd TEXT NOT NULL, vsl_nm TEXT NOT NULL,
    subject TEXT NOT NULL, category TEXT NOT NULL, equipment TEXT NOT NULL,
    maker TEXT, type_nm TEXT,
    app_voy TEXT NOT NULL, app_port_cd TEXT NOT NULL, app_dt TEXT NOT NULL,
    cause TEXT NOT NULL, inspection TEXT NOT NULL, detail TEXT NOT NULL,
    stock TEXT NOT NULL CHECK(stock IN ('owner','vendor')),
    reason_cd TEXT NOT NULL, dept_cd TEXT NOT NULL,
    dock_yn TEXT NOT NULL DEFAULT 'N' CHECK(dock_yn IN ('Y','N')),
    urgent_yn TEXT NOT NULL DEFAULT 'N' CHECK(urgent_yn IN ('Y','N')),
    critical_yn TEXT NOT NULL DEFAULT 'N' CHECK(critical_yn IN ('Y','N')),
    status TEXT NOT NULL DEFAULT 'pending'
           CHECK(status IN ('pending','approved','saving','saved','failed')),
    rep_cd TEXT UNIQUE,
    dock_rid INTEGER UNIQUE REFERENCES dock_procure(id),
    result TEXT, version INTEGER NOT NULL DEFAULT 1,
    decided_by TEXT, decided_at TEXT, done_at TEXT, created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_repair_request_status ON repair_request(status, id);

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
    vsl_cd        TEXT,                            -- SSOT(P0): SVMS 4자 코드
    vt_vessel_id  INTEGER,                         -- SSOT(P0): vesseltracker 내부 vesselId
    aliases       TEXT,                            -- SSOT(P0): 구선명·표기 별칭 JSON 배열 문자열
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
    priority    INTEGER NOT NULL DEFAULT 0,  -- 1=중요(Priority 체크), 0=일반
    status      TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Closed')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (vetting_id) REFERENCES vettings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vt_findings_vetting ON vt_findings(vetting_id, no);

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

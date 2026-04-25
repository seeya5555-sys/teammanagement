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

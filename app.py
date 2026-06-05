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
from datetime import timedelta

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
)


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
            return redirect(url_for('index'))
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

    nxt = request.args.get('next') or url_for('index')
    # 외부 URL 리다이렉트 방지
    if not nxt.startswith('/'):
        nxt = url_for('index')
    return redirect(nxt)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


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
    HEADERS = ['NO.', '작성일', '마감일', '선박명', 'ITEM',
               'DESCRIPTION', 'ACTION PLAN', '우선순위', '상태', '작성자']
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
    STAT_LABEL = {'Open': 'Open', 'InProgress': '진행중', 'Closed': 'Closed'}

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
        # "2026-05" -> "2026년 5월"
        try:
            y, m = ym.split('-')
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
        sub_chips.append('필터: ' + status_in.replace(',', ' / '))
    elif request.args.get('status'):
        sub_chips.append('상태: ' + request.args.get('status'))
    if request.args.get('priority'):
        sub_chips.append('우선순위: ' + request.args.get('priority'))
    if request.args.get('vessel_type'):
        sub_chips.append('선종: ' + request.args.get('vessel_type'))
    if request.args.get('vessel_id'):
        vname = query('SELECT name FROM vessels WHERE id=?',
                      (request.args.get('vessel_id'),), one=True)
        if vname: sub_chips.append('선박: ' + vname['name'])
    if request.args.get('q'):
        sub_chips.append('검색: ' + request.args.get('q'))
    sub_text = ' | '.join(sub_chips) if sub_chips else '전체 항목'

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
            c1 = ws.cell(row=1, column=1, value=f'Daily 업무관리   |   {sn}')
            c1.font = title_font
            c1.fill = title_fill
            c1.alignment = Alignment(horizontal='left', vertical='center', indent=1)
            ws.row_dimensions[1].height = 30

            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=N_COLS)
            total_cnt = sum(len(v) for m in months.values() for v in m.values())
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
                    # 일 헤더 행
                    ws.merge_cells(start_row=cur_row, start_column=1,
                                   end_row=cur_row, end_column=N_COLS)
                    dc = ws.cell(row=cur_row, column=1,
                                 value=f'   ▸  {dkey}   ({len(items)} item{"s" if len(items)>1 else ""})')
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
    if len(sv_seq) == 1:
        fname = f'TRMT_Daily_{_sheet_safe(sv_seq[0])}_{today}.xlsx'
    else:
        fname = f'TRMT_Daily_{today}.xlsx'

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(
        bio,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=fname,
    )


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
def _gemini_call_json(parts):
    """parts(list) → Gemini generateContent → 파싱된 JSON dict 또는 {'error':...}."""
    if not GEMINI_API_KEY:
        return {'error': 'NO_API_KEY'}
    import urllib.request, urllib.error
    body = {'contents': [{'parts': parts}],
            'generationConfig': {'response_mime_type': 'application/json'}}
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{GEMINI_MODEL}:generateContent')
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


def _findings_prompt(kind):
    if kind == 'cs':
        return (
            "다음은 선박 컨디션 서베이(상태검사) 보고서다. 보고서에 적힌 지적/관찰 항목을 "
            "빠짐없이 추출해 지정한 JSON으로만 답하라. 각 항목 필드:\n"
            "- category: 'Defect' 또는 'Observation' (시정이 필요한 지적은 Defect, 권고/관찰사항은 Observation)\n"
            "- item: 짧은 제목 한 줄 (예: 'Main deck 부식')\n"
            "- description: 상세 내용\n"
            "- remark: 위치/조치/비고 등 부가정보 (없으면 빈 문자열)\n"
            "원문이 영어면 item/description은 영어 그대로 두라. 없는 내용을 지어내지 말 것. "
            "항목이 하나도 없으면 items를 빈 배열로.\n"
            '형식: {"items":[{"category":"Defect","item":"","description":"","remark":""}]}'
        )
    return (
        "다음은 선박 SIRE/베팅 점검 보고서다. 보고서에 적힌 관찰사항(observation)을 "
        "빠짐없이 추출해 지정한 JSON으로만 답하라. 각 항목 필드:\n"
        "- item: 짧은 제목 한 줄\n"
        "- description: 상세 내용 (지적 본문)\n"
        "- remark: 참조번호/장비/비고 등 부가정보 (없으면 빈 문자열)\n"
        "원문이 영어면 그대로 두라. 없는 내용을 지어내지 말 것. 없으면 items를 빈 배열로.\n"
        '형식: {"items":[{"item":"","description":"","remark":""}]}'
    )


def _normalize_findings(parsed, kind):
    out = []
    for it in (parsed.get('items') or []):
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
        'item':        ['item', '항목', 'title', 'subject', '제목'],
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
            return data, None
        parsed = _gemini_call_json([{'text': _findings_prompt(kind) + '\n\n[보고서 표 내용]\n' + data}])
    elif ext == 'pdf':
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'PDF가 너무 큽니다({size_mb:.1f}MB). 15MB 이하로 줄이거나 페이지를 나눠 올려주세요.'}
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': 'application/pdf', 'data': b64}},
            {'text': _findings_prompt(kind)},
        ])
    elif ext in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'):
        if size_mb > 15:
            return None, {'reason': 'TOO_LARGE', 'message': f'이미지가 너무 큽니다({size_mb:.1f}MB).'}
        import mimetypes
        media = mimetypes.guess_type(name)[0] or 'image/jpeg'
        b64 = __import__('base64').standard_b64encode(raw).decode()
        parsed = _gemini_call_json([
            {'inline_data': {'mime_type': media, 'data': b64}},
            {'text': _findings_prompt(kind)},
        ])
    else:
        return None, {'reason': 'BAD_TYPE', 'message': 'PDF, 이미지, 엑셀(xlsx) 파일만 지원합니다.'}

    if parsed.get('error') == 'NO_API_KEY':
        return None, {'reason': 'no_api_key', 'message': 'AI 자동추출이 설정되지 않았습니다(키 미설정).'}
    if parsed.get('error'):
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

    op = d.get('operation') or None
    if op and op not in ('Loading','Discharging','Idle'):
        op = None

    new_id = execute("""
        INSERT INTO vettings
            (vessel_id, report_number, inspection_date, inspection_company,
             inspector, port, operation, overall_remark, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (vid,
          d.get('report_number') or '',
          d.get('inspection_date') or None,
          d.get('inspection_company') or '',
          d.get('inspector') or '',
          d.get('port') or '',
          op,
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
              'port','operation','overall_remark',
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
            'status':      d.get('status') or 'Open',
        }]

    next_no = _vt_next_no(vid)
    created = []
    for it in items:
        st = it.get('status') or 'Open'
        if st not in ('Open','Closed'): st = 'Open'
        fid = execute("""
            INSERT INTO vt_findings (vetting_id, no, item, description, remark, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (vid, next_no,
              it.get('item') or '',
              it.get('description') or '',
              it.get('remark') or '',
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
    for f in ('item','description','remark','status'):
        if f in d:
            sets.append(f'{f} = ?')
            params.append(d[f] or '')
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
           f'{GEMINI_MODEL}:generateContent')
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
    return render_template('index.html'), 404


# ═════════════════════════════════════════════════════════════════
#  CLI entry
# ═════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--init-db':
        init_db(drop=True)
        sys.exit(0)

    if not os.path.exists(DATABASE):
        print('[INFO] DB 파일이 없어 자동 초기화합니다.')
        init_db(drop=False)

    # 개발 환경
    app.run(host='0.0.0.0', port=5000, debug=True)

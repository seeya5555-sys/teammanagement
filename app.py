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

    out = []
    for v in vessels:
        out.append({
            'vessel': dict(v),
            'surveys': by_vessel.get(v['id'], {}),
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
    if not query('SELECT id FROM cs_findings WHERE id=?', (fid,), one=True):
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

    out = []
    for ves in vessels:
        vd = dict(ves)
        vd['supervisor_ids'] = sv_map.get(ves['id'], [])
        out.append({
            'vessel': vd,
            'vettings': by_vessel.get(ves['id'], []),
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
    if not query('SELECT id FROM vt_findings WHERE id=?', (fid,), one=True):
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

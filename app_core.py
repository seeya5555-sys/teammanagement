"""애플리케이션 바닥 층 — config · Flask 인스턴스 · DB 원시 헬퍼.

왜 별도 파일인가
----------------
`helpers_shared.py` 는 이 세 가지(설정 상수·`app`·`get_db/query/execute`)만
`app.py` 에서 빌려 썼고, 반대로 `app.py` 는 `helpers_shared` 의 심볼 5개를 썼다.
서로가 서로를 필요로 해서 **import 로는 표현할 수 없는 순환**이었고, 그래서
`helpers_shared` 는 모듈이 아니라 `exec(..., globals(), globals())` 로 로드됐다.
그 대가가 "의존이 소스 어디에도 안 적힘 + 오타가 요청 시점 NameError" 였다.

빌려 쓰던 것을 여기로 내리면 순환이 사라진다:

    app_core  ←  helpers_shared  ←  app.py  ←  routes_*/ai_gemini

이 파일은 위층을 **절대 참조하지 않는다**(참조하는 순간 순환이 되돌아온다).
층위 위반은 `tests/test_boundary_dependency_graph.py` 가 막는다.

`from app import DATABASE` 같은 기존 표면은 그대로다 — `app.py` 가 아래 이름들을
명시적으로 import 해서 자기 네임스페이스에 다시 묶는다.
"""
import os
import secrets
import sqlite3
import sys
import tempfile
from datetime import timedelta

from flask import Flask, g

# ═════════════════════════════════════════════════════════════════
#  Config
# ═════════════════════════════════════════════════════════════════
BASE_DIR     = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
UPLOAD_DIR   = os.path.join(BASE_DIR, 'static', 'uploads')
INVOICE_PDF_DIR = os.path.join(INSTANCE_DIR, 'invoice_pdfs')  # 인보이스 미리보기 PDF(컨펌/리젝 시 자동삭제)
JEONJA_PDF_DIR = os.path.join(INSTANCE_DIR, 'jeonja_pdfs')    # 전자결재 검토 invoice/DN 미리보기 cache
AOR_PDF_DIR = os.path.join(INSTANCE_DIR, 'aor_pdfs')          # AOR 첨부 견적서 preview cache
FUNDREQ_FILE_DIR = os.path.join(INSTANCE_DIR, 'fundreq_files')  # 비용청구 SVMS 첨부(인보이스·증빙) preview cache
SOA_REVIEW_PDF_DIR = os.path.join(INSTANCE_DIR, 'soa_review_pdfs')  # SOA 수동검토 첨부 PDF cache
DOCKATT_FILE_DIR = os.path.join(INSTANCE_DIR, 'dockproc_files')  # Dock 발주현황 벤더 견적서(SVMS MAOE) preview cache
STT_AUDIO_DIR = os.path.join(INSTANCE_DIR, 'stt_audio')       # 회의록 STT 원본 오디오 cache
LISCR_PDF_DIR = os.path.join(INSTANCE_DIR, 'liscr_pdfs')      # 기국(LISCR) 인보이스 PDF 업로드 원본 — 맥 러너가 받아 SVMS 첨부까지 씀
# 회의록 STT Phase 0a 상수
STT_AUDIO_EXT = {'m4a', 'wav', 'mp3', 'aac', 'caf', 'webm', 'ogg', 'mp4', 'aiff', 'flac'}
STT_MAX_BYTES = 200 * 1024 * 1024   # 200MB 상한
STT_LEASE_SEC = 1800                # processing lease 30분 — 초과 시 stale로 재큐(whisper turbo 실시간 10-30x)
STT_MAX_ATTEMPTS = 5                # 재시도 상한 — 초과 시 error 확정

DATABASE        = os.path.join(INSTANCE_DIR, 'trmt.db')
SCHEMA_FILE     = os.path.join(BASE_DIR, 'schema.sql')
SEED_FILE       = os.path.join(BASE_DIR, 'seed.sql')
SECRET_KEY_FILE = os.path.join(INSTANCE_DIR, '.secret_key')

ALLOWED_EXT = {
    'jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'webp', 'bmp',
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'csv', 'msg'
}

_RUNTIME_DIRS = (
    INSTANCE_DIR, UPLOAD_DIR, INVOICE_PDF_DIR, JEONJA_PDF_DIR, AOR_PDF_DIR,
    FUNDREQ_FILE_DIR, SOA_REVIEW_PDF_DIR, DOCKATT_FILE_DIR, STT_AUDIO_DIR,
    LISCR_PDF_DIR,
)


def _load_or_create_secret_key():
    """Load the durable key, atomically publishing it once when absent."""
    try:
        with open(SECRET_KEY_FILE, 'rb') as f:
            key = f.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        secret_dir = os.path.dirname(SECRET_KEY_FILE)
        fd, candidate = tempfile.mkstemp(prefix='.secret_key.', dir=secret_dir)
        try:
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(key)
                    f.flush()
                    os.fsync(f.fileno())
                # Hard-link publication is atomic and never replaces a winner.
                # Unlike O_EXCL + write, readers can only observe a complete file.
                os.link(candidate, SECRET_KEY_FILE)
            except FileExistsError:
                with open(SECRET_KEY_FILE, 'rb') as f:
                    key = f.read()
        finally:
            try:
                os.unlink(candidate)
            except OSError:
                pass
    if not key:
        raise RuntimeError(f'empty secret key file: {SECRET_KEY_FILE}')
    return key


def init_runtime():
    """Create runtime directories and switch Flask to the persisted key.

    Importing this module deliberately has no filesystem side effects.  Startup
    and migration entry points call this idempotent initializer explicitly.
    """
    for path in _RUNTIME_DIRS:
        os.makedirs(path, exist_ok=True)
    key = _load_or_create_secret_key()
    app.config['SECRET_KEY'] = key
    return key


# Descriptive alias for callers that prefer the full name.
initialize_runtime = init_runtime


# ─────────────────────────────────────────────────────────────────
#  이미지 포맷 등록 (HEIC/HEIF)
# ─────────────────────────────────────────────────────────────────
# 아이폰 기본 촬영 포맷이 HEIC 인데 Pillow 는 이걸 기본으로 못 연다. 그래서 HEIC 첨부는
# `Image.open` 에서 예외가 나고, 메일 본문에서는 사진 대신 사유 문구가, DOCX 에서는
# 원본 .heic 가 그대로 삽입돼(Word 가 못 그린다) 빈 칸이 됐다.
#
# 🔴 왜 프로세스 전역 등록이 여기(app_core, 바닥 층)에 있나
#    디코드가 필요한 곳이 세 군데(메일 본문·업로드 재인코딩·DOCX 삽입)인데 셋은 서로
#    형제 모듈이라 서로를 import 할 수 없다(층위 게이트). 셋 다 이미 app_core 를 보므로
#    여기 두면 새 결합도, 의존 그래프 fixture 변경도 생기지 않는다.
#
# 🔴 import 시점에 등록하지 않는다. 이 모듈은 "import 는 부작용 없음" 이 계약이고,
#    pillow-heif 가 없거나 깨진 서버에서 앱 부팅 자체가 죽으면 안 된다. 실패는 조용히
#    삼키고 예전 동작(= 못 읽는 형식으로 취급)으로 떨어진다.
_heif_registered = None


def ensure_heif_opener():
    """HEIC/HEIF 를 Pillow 가 열 수 있게 등록한다. 여러 번 불러도 실제 등록은 1회.

    돌려주는 값은 "지금 HEIC 를 열 수 있는가" 다. 호출부는 `Image.open` 직전에 부르면
    되고, False 여도 따로 분기할 필요는 없다 -- 기존의 '못 읽는 형식' 경로가 그대로 받는다.
    """
    global _heif_registered
    if _heif_registered is None:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _heif_registered = True
        except Exception as exc:                       # pragma: no cover - 배포 의존성
            # 🔴 결과는 프로세스 수명 내내 캐시되므로 이유를 여기서 한 번은 남긴다.
            # 조용히 False 로 굳으면 "왜 HEIC 만 계속 빠지지" 를 로그로 못 쫓는다.
            _heif_registered = False
            try:
                sys.stderr.write(
                    'WARN: pillow-heif 사용 불가 — HEIC 는 계속 제외됨 (%s: %s)\n'
                    % (type(exc).__name__, exc))
            except Exception:
                pass
    return _heif_registered

app = Flask(__name__)
app.config.update(
    # Import must remain side-effect free, and an entry point that forgot
    # init_runtime() must fail closed.  A random import-time key looks usable
    # but forks per worker and causes intermittent session/token failures.
    SECRET_KEY=None,
    DATABASE=DATABASE,
    UPLOAD_FOLDER=UPLOAD_DIR,
    MAX_CONTENT_LENGTH=STT_MAX_BYTES + (1 << 20),  # 상한=회의록 오디오 200MB. 그 외 업로드는 before_request서 20MB로 조임
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    JSON_AS_ASCII=False,
    SESSION_COOKIE_SAMESITE='Lax',
    SEND_FILE_MAX_AGE_DEFAULT=0,                   # static(css/js) 매번 재검증 — 모바일 캐시 stale 방지
)


@app.before_request
def _require_runtime_initialization():
    """Reject requests from unsupported entry points that skipped init_runtime()."""
    if app.config.get('SECRET_KEY') is None:
        raise RuntimeError(
            'TRMT runtime is not initialized; use wsgi:application or call init_runtime()')

_NON_STT_UPLOAD_MAX = 20 * 1024 * 1024             # 회의록 외 업로드(사진·엑셀 등) 상한 20MB
_SOA_REVIEW_SNAPSHOT_MAX = 100 * 1024 * 1024        # API-key Mac runner가 예외 인보이스 PDF 묶음을 동기화


# ═════════════════════════════════════════════════════════════════
#  DB helpers
# ═════════════════════════════════════════════════════════════════
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
        # 동시성: WAL 은 읽기/쓰기가 서로 안 막음. busy_timeout 으로 잠금 대기 재시도.
        g.db.execute('PRAGMA journal_mode = WAL')
        g.db.execute('PRAGMA busy_timeout = 5000')
        g.db.execute('PRAGMA synchronous = NORMAL')
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
    # 선박 purge는 여러 DELETE를 하나의 명시 transaction으로 묶는다.
    # 그 밖의 기존 호출은 기존처럼 즉시 commit한다.
    if not (getattr(g, '_vessel_purge_transaction', False)
            or getattr(g, '_reqgen_result_transaction', False)):
        db.commit()
    last_id = cur.lastrowid
    cur.close()
    return last_id


def execute_rc(sql, params=()):
    """UPDATE/DELETE 영향 행수 반환 — 조건부(낙관적 락) 갱신 race 판정용."""
    db = get_db()
    cur = db.execute(sql, params)
    if not getattr(g, '_reqgen_result_transaction', False):
        db.commit()
    rc = cur.rowcount
    cur.close()
    return rc

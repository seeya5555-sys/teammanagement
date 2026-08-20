"""
wsgi.py — A1 gunicorn 통합 엔트리 (TRMT + Dock Manager 서브마운트)
배치: /home/opc/app/wsgi.py  (drydock_integration.py 도 같은 폴더)
gunicorn: `wsgi:application`  (기존 `app:app` 대체)

env:
  DRYDOCK_DIR   drydock 코드 위치 (기본 /home/opc/drydock)
  DRYDOCK_MOUNT 마운트 경로       (기본 /drydock)

라우팅: /*  → TRMT,  /drydock/*  → Dock Manager
DB     : trmt.db / fleet.db 각자 유지(방식①). 코드 무병합.
롤백   : ExecStart를 `app:app`으로 되돌리면 즉시 원복.
"""
import os, sys, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
DRYDOCK_DIR = os.environ.get("DRYDOCK_DIR", "/home/opc/drydock")
MOUNT = os.environ.get("DRYDOCK_MOUNT", "/drydock")

# ── 1. TRMT (기본: 같은 폴더 app.py / 로컬테스트: TRMT_DIR) ──────
TRMT_DIR = os.environ.get("TRMT_DIR", HERE)
sys.path.insert(0, HERE)                # drydock_integration.py 위치
sys.path.insert(0, TRMT_DIR)            # TRMT app.py 위치
import app as trmt                      # noqa: E402
trmt.init_runtime()
trmt_app = trmt.app
# CSRF 는 config 없이도 켜지지만(csrf.enforce 기본값 = not app.testing), 배포
# 엔트리에서는 명시적으로 못박는다. 기본값 계산이 나중에 바뀌어도 실서비스가
# 조용히 열리지 않게 하는 fail-closed 핀이다.
trmt_app.config["CSRF_PROTECT"] = True

# ── 2. Dock Manager (drydock/app.py) 를 별도 모듈로 import ──────
#    drydock 자체 SECRET_KEY 하드코딩 fallback 무력화 위해, import 전
#    env SECRET_KEY 를 TRMT 키로 주입(2차 방어; 통합 shim이 최종 동일화).
if trmt_app.secret_key:
    _sk = trmt_app.secret_key
    os.environ["SECRET_KEY"] = _sk.decode("latin-1") if isinstance(_sk, bytes) else str(_sk)

def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

dd = _load(os.path.join(DRYDOCK_DIR, "app.py"), "drydock_app_mod")

# ── 3. 통합 shim 적용 (SSO·admin게이팅·MCP폐기·sqlite하드닝) ────
import drydock_integration as ddi       # noqa: E402
dd_app = ddi.apply(dd, trmt_app)

# ── 4. 서브마운트 ───────────────────────────────────────────────
from werkzeug.middleware.dispatcher import DispatcherMiddleware  # noqa: E402
application = DispatcherMiddleware(trmt_app.wsgi_app, {MOUNT: dd_app.wsgi_app})

# gunicorn 은 module-level callable `application` 을 로드

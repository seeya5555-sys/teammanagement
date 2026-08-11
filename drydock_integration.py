"""
drydock_integration.py — Dock Manager × TRMT 통합 shim (프로덕션)
설계 §0b 확정결정(MCP/OAuth 폐기 + SSO 세션신뢰+admin게이팅)의 실배선.
PoC(poc_merged.py 11/11 PASS)에서 검증된 로직을 프로덕션 모듈로 승격.

원칙: drydock 원본 코드(app.py)는 건드리지 않고 import 후 런타임 패치.
      → drydock git 업데이트 pull 시 충돌 최소화(방식① 코드분리 유지).

사용:
    import drydock_integration as ddi
    dd_app = ddi.apply(dd_module, trmt_app)   # 반환: 통합·잠금 완료된 drydock WSGI app
"""
from functools import wraps


def apply(dd, trmt_app):
    """dd = import된 drydock app.py 모듈, trmt_app = TRMT Flask app.
    반환 = 통합 적용된 dd.app (서브마운트용)."""
    from flask import session, request, jsonify, redirect

    dd_app = dd.app

    # ── 0. 템플릿 자동리로드 (drydock UI 패치 후 gunicorn 재시작 없이 반영) ──
    dd_app.config["TEMPLATES_AUTO_RELOAD"] = True
    dd_app.jinja_env.auto_reload = True

    # ── 1. SECRET_KEY 공유 (결정2: 하드코딩 fallback 제거 대체) ──────────
    # TRMT의 서명키를 그대로 써야 세션 쿠키 상호 인식(SSO). 값 불일치 시 즉시 실패.
    if not trmt_app.secret_key:
        raise RuntimeError("TRMT secret_key 미설정 — SSO 불가, 기동 거부")
    dd_app.secret_key = trmt_app.secret_key

    # ── 2. get_current_user: 세션 신뢰로 패치 (결정2-b, C1 해소) ─────────
    #    drydock users 테이블 조회 X. TRMT 세션(username/role)만 신뢰.
    def _session_user():
        if not session.get("username"):
            return None
        return {
            "username": session.get("username"),
            "role": session.get("role", "editor"),
            "vessels": "[]",          # admin은 전 선박 허용이라 무관
        }
    dd.get_current_user = _session_user
    dd.is_admin = lambda: (_session_user() or {}).get("role") == "admin"
    # is_viewer/can_access_vessel도 세션 유저 기준으로 동작(원본이 get_current_user 참조)

    # ── 3. 전 route admin 게이팅 + login/logout 차단 (결정2, C2·C3 해소) ─
    @dd_app.before_request
    def _sso_admin_gate():
        # 경로 정규화(올마이트: 대소문자·중복슬래시·trailing 우회 차단)
        raw = request.path
        norm = "/" + "/".join(seg for seg in raw.split("/") if seg)   # 중복슬래시 제거
        low = norm.lower().rstrip("/")
        # 3-a. drydock 자체 login/logout 비활성(공유쿠키 session.clear 차단). 모든 method.
        if low in ("/login", "/logout"):
            return ("Dock Manager 로그인은 TRMT 통합 로그인으로 대체됨", 404)
        # 3-b. MCP/OAuth 폐기 (결정1, C5). 접두 변형 포함.
        if low == "/oauth" or low.startswith(("/oauth/", "/mcp", "/.well-known")):
            return ("Not Found", 404)
        # 3-c. 정적파일만 게이트 통과(정확히 /static/ 하위. traversal은 Flask가 차단)
        if low == "/static" or low.startswith("/static/"):
            return None
        # 3-d. admin만 통과 (username+role=admin 동시 요구)
        if session.get("username") and session.get("role") == "admin":
            # login_required 호환 shim — 이미 있으면 재대입 안 함(Set-Cookie 반복 방지, 올마이트)
            if not session.get("logged_in"):
                session["logged_in"] = True
            return None
        # 3-e. 비인가 → API는 401 JSON, 그 외는 TRMT(루트) 로그인으로
        if low.startswith("/api"):
            return jsonify({"error": "Unauthorized (admin only)"}), 401
        return redirect("/login")

    # ── 4. sqlite 안정화 (C6): busy_timeout + WAL ──────────────────────
    #    A1은 gunicorn worker=1이라 경합 낮지만, threads=8 대비 방어.
    _orig_get_db = dd.get_db
    def _get_db_hardened():
        db = _orig_get_db()
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        return db
    dd.get_db = _get_db_hardened

    return dd_app

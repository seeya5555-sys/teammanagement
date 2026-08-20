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
from contextlib import contextmanager
from datetime import date as _date, datetime as _datetime, timezone as _timezone
import hashlib
import json
import os
import re
import sqlite3


DAILY_EVENTS_PATH = "/api/integration/daily-events"
DAILY_EVENT_SOURCES = (
    "jobs",
    "discussions",
    "class_items",
    "steel_repair",
    "pipe_repair",
    "outfitting",
    "wbt_cot",
    "portable_fan",
    "staging",
    "gas_free",
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _text(value):
    """Return a stable, whitespace-normalized text value for event fields."""
    if value is None:
        return ""
    return str(value).strip()


def _bool(value):
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y", "on", "urgent", "critical"}


def _business_date(value):
    """Extract a YYYY-MM-DD business date without accepting a date prefix."""
    raw = _text(value)
    if len(raw) < 10 or not _DATE_RE.match(raw[:10]):
        return None
    return raw[:10]


def _timezone_timestamp(value):
    """Make a Dock Manager timestamp explicit and canonical.

    The Dock Manager schema uses SQLite ``datetime('now')`` defaults, which
    are UTC but do not carry an offset.  Treating those known-naive values as
    UTC preserves their meaning while ensuring the integration contract never
    emits an ambiguous timestamp.
    """
    if isinstance(value, _datetime):
        parsed = value
    else:
        raw = _text(value)
        if not raw:
            raise ValueError("source_updated_at is required")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = _datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("invalid source_updated_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_timezone.utc)
    return parsed.isoformat(timespec="seconds")


def _json_array(value, source):
    if value is None or value == "":
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid JSON in %s" % source) from exc
    if not isinstance(parsed, list):
        raise ValueError("%s must be a JSON array" % source)
    if any(not isinstance(item, dict) for item in parsed):
        raise ValueError("%s contains a non-object item" % source)
    return parsed


def _canonical_hash(event):
    content = {key: event[key] for key in event if key != "source_hash"}
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _section(source_table, values):
    haystack = " ".join(_text(value) for value in values).lower()
    if "egcs" in haystack:
        return "egcs"
    if source_table == "class_items":
        return "survey"
    # Only an explicit vendor/supplier tag or label routes a discussion to
    # Vendor.  Other discussions remain Shipyard instead of being guessed.
    if source_table == "discussions" and re.search(r"(?:^|[\s#\[(:])(?:vendor|supplier)(?:$|[\s#\]\):])", haystack):
        return "vendor"
    return "shipyard"


def _tracking_body(source_table, row):
    labels = {
        "steel_repair": ("description", "location", "status", "priority", "remark"),
        "pipe_repair": ("description", "system_line", "position_tank", "status", "remark"),
        "outfitting": ("description", "location", "status", "priority", "remark"),
        "wbt_cot": ("tank_name", "manhole_status", "remark"),
        "portable_fan": ("location", "qty", "remark"),
        "staging": ("location", "staging_area", "qty", "remark"),
        "gas_free": ("tank", "certificate", "remark"),
    }
    return "; ".join(
        "%s: %s" % (key.replace("_", " "), _text(row[key]))
        for key in labels[source_table]
        if key in row.keys() and _text(row[key])
    )


def _event_base(source_table, row, project_id, source_subkey, event_date,
                source_updated_at, kind, title, body, progress, important,
                values):
    event = {
        "source_project_id": _text(project_id),
        "source_table": source_table,
        "source_id": _text(row["id"]),
        "source_subkey": source_subkey,
        "source_updated_at": _timezone_timestamp(source_updated_at),
        "updated_at": _timezone_timestamp(source_updated_at),
        "date": event_date,
        "kind": kind,
        "title": _text(title),
        "body": _text(body),
        "progress": _text(progress),
        "important": _bool(important),
        "suggested_section": _section(source_table, values),
    }
    event["source_hash"] = _canonical_hash(event)
    return event


def _job_events(row, project_id, wanted):
    out = []
    remarks = [item for item in _json_array(row["remarks"], "jobs.remarks")
               if _business_date(item.get("date")) == wanted]
    prepared = []
    for remark in remarks:
        # There is no child id in the legacy JSON shape.  The business date
        # is stable; same-day duplicates use a content fingerprint, never an
        # array index.
        child = {key: remark[key] for key in sorted(remark) if key != "updated_at"}
        fingerprint = hashlib.sha256(json.dumps(child, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        prepared.append((fingerprint, remark))
    for fingerprint, remark in sorted(prepared, key=lambda item: item[0]):
        progress = remark.get("progress") or remark.get("body") or remark.get("remark")
        # Always include the content fingerprint. If a second same-day remark
        # is inserted later, identities of existing children must not shift.
        subkey = "job:%s:remark:%s:%s" % (row["id"], wanted, fingerprint)
        out.append(_event_base(
            "jobs", row, project_id, subkey, wanted,
            remark.get("updated_at") or row["updated_at"], "job_remark",
            "%s %s" % (_text(row["number"]), _text(row["description"])),
            progress, progress, remark.get("important", False),
            (row["section"], row["category"], row["vendor"], row["description"]),
        ))
    return out


def _discussion_events(row, project_id, wanted):
    out = []
    values = (row["item"], row["description"], row["no"])
    if _business_date(row["date"]) == wanted:
        out.append(_event_base(
            "discussions", row, project_id, "discussion:%s:body:%s" % (row["id"], wanted),
            wanted, row["updated_at"], "discussion", row["item"] or row["no"],
            row["description"], "", row["priority"] in ("Urgent", "Critical"), values,
        ))
    for action in _json_array(row["actions"], "discussions.actions"):
        if _business_date(action.get("date")) != wanted:
            continue
        child = {key: action[key] for key in sorted(action) if key != "updated_at"}
        fingerprint = hashlib.sha256(json.dumps(child, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        progress = action.get("progress") or action.get("body") or action.get("remark")
        out.append(_event_base(
            "discussions", row, project_id,
            "discussion:%s:action:%s:%s" % (row["id"], wanted, fingerprint),
            wanted, action.get("updated_at") or row["updated_at"], "discussion_action",
            row["item"] or row["no"], progress, progress,
            action.get("important", row["priority"] in ("Urgent", "Critical")), values,
        ))
    return out


def _class_events(row, project_id, wanted):
    out = []
    values = (row["finding"], row["description"], row["responsible"], row["priority"])
    for action in _json_array(row["actions"], "class_items.actions"):
        if _business_date(action.get("date")) != wanted:
            continue
        child = {key: action[key] for key in sorted(action) if key != "updated_at"}
        fingerprint = hashlib.sha256(json.dumps(child, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        progress = action.get("progress") or action.get("body") or action.get("remark")
        out.append(_event_base(
            "class_items", row, project_id,
            "class_item:%s:action:%s:%s" % (row["id"], wanted, fingerprint),
            wanted, action.get("updated_at") or row["updated_at"], "class_item_action",
            row["finding"] or row["no"], progress, progress,
            action.get("important", row["priority"] in ("Urgent", "Critical")), values,
        ))
    return out


_TRACKING_DATES = {
    "steel_repair": ("start_date", "completion_date", "last_updated"),
    "pipe_repair": ("start_date", "completion_date", "last_updated"),
    "outfitting": ("start_date", "completion_date", "last_updated"),
    "wbt_cot": ("open_date", "close_date", "bottom_plug_open", "bottom_plug_close", "updated_at"),
    "portable_fan": ("start_date", "stop_date", "updated_at"),
    "staging": ("updated_at",),
    "gas_free": ("date", "updated_at"),
}


def _tracking_events(source_table, row, project_id, wanted):
    date_fields = _TRACKING_DATES[source_table]
    if not any(_business_date(row[field]) == wanted for field in date_fields if field in row.keys()):
        return []
    updated = row["last_updated"] if "last_updated" in row.keys() else row["updated_at"]
    description = row["description"] if "description" in row.keys() else ""
    title = "%s %s" % (_text(row["no"]), _text(description))
    body = _tracking_body(source_table, row)
    important = _text(row["priority"]).lower() in {"urgent", "critical"} if "priority" in row.keys() else False
    values = tuple(row[field] for field in row.keys())
    return [_event_base(
        source_table, row, project_id, "%s:%s:row:%s" % (source_table, row["id"], wanted),
        wanted, updated, "%s_update" % source_table, title, body,
        row["status"] if "status" in row.keys() else "", important, values,
    )]


def _database_path(dd, dd_app):
    candidate = dd_app.config.get("DATABASE")
    if not candidate:
        candidate = getattr(dd, "DATABASE", None) or getattr(dd, "DB_PATH", None)
    if not candidate or candidate == ":memory:":
        return None
    candidate = os.fspath(candidate)
    if candidate.startswith("file:"):
        return candidate
    if not os.path.isabs(candidate):
        candidate = os.path.join(dd_app.instance_path, candidate)
    return candidate


@contextmanager
def _read_connection(dd, dd_app):
    """Open the Dock DB read-only; the fallback exists only for test doubles."""
    path = _database_path(dd, dd_app)
    if path:
        if path.startswith("file:"):
            uri = path + ("&" if "?" in path else "?") + "mode=ro"
        else:
            uri = "file:%s?mode=ro" % path
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
        return
    # A real Dock Manager app has DATABASE.  Test doubles may expose only a
    # connection factory; do not add writes or PRAGMAs to that connection.
    conn = getattr(dd, "_trmt_original_get_db", dd.get_db)()
    try:
        yield conn
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()


def _daily_events_payload(dd, dd_app, project_ids, wanted):
    events = []
    complete_sources = []
    with _read_connection(dd, dd_app) as conn:
        # Check the complete source set before returning anything.  A missing
        # table is an unavailable source, not an empty daily report.
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "(?,?,?,?,?,?,?,?,?,?)", DAILY_EVENT_SOURCES,
        ).fetchall()
        available = {row["name"] for row in table_rows}
        if available != set(DAILY_EVENT_SOURCES):
            missing = [name for name in DAILY_EVENT_SOURCES if name not in available]
            raise RuntimeError("Dock Manager source unavailable: " + ",".join(missing))
        for source_table in DAILY_EVENT_SOURCES:
            allowed = set(project_ids)
            # Keep each identifier literal: source_table is a server-owned
            # allowlist, but this also keeps the read boundary auditable by
            # the repository SQL-construction contract.  Project filtering is
            # intentionally performed after the read; no user value reaches
            # SQL text and the connection is opened mode=ro.
            if source_table == "jobs":
                rows = conn.execute("SELECT * FROM jobs ORDER BY vessel_id, id").fetchall()
            elif source_table == "discussions":
                rows = conn.execute("SELECT * FROM discussions ORDER BY vessel_id, id").fetchall()
            elif source_table == "class_items":
                rows = conn.execute("SELECT * FROM class_items ORDER BY vessel_id, id").fetchall()
            elif source_table == "steel_repair":
                rows = conn.execute("SELECT * FROM steel_repair ORDER BY vessel_id, id").fetchall()
            elif source_table == "pipe_repair":
                rows = conn.execute("SELECT * FROM pipe_repair ORDER BY vessel_id, id").fetchall()
            elif source_table == "outfitting":
                rows = conn.execute("SELECT * FROM outfitting ORDER BY vessel_id, id").fetchall()
            elif source_table == "wbt_cot":
                rows = conn.execute("SELECT * FROM wbt_cot ORDER BY vessel_id, id").fetchall()
            elif source_table == "portable_fan":
                rows = conn.execute("SELECT * FROM portable_fan ORDER BY vessel_id, id").fetchall()
            elif source_table == "staging":
                rows = conn.execute("SELECT * FROM staging ORDER BY vessel_id, id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM gas_free ORDER BY vessel_id, id").fetchall()
            for row in rows:
                project_id = row["vessel_id"]
                if project_id not in allowed:
                    continue
                if source_table == "jobs":
                    events.extend(_job_events(row, project_id, wanted))
                elif source_table == "discussions":
                    events.extend(_discussion_events(row, project_id, wanted))
                elif source_table == "class_items":
                    events.extend(_class_events(row, project_id, wanted))
                else:
                    events.extend(_tracking_events(source_table, row, project_id, wanted))
            complete_sources.append(source_table)
    events.sort(key=lambda item: (item["source_table"], item["source_id"], item["source_subkey"]))
    return {
        "complete": True,
        "partial": False,
        "complete_sources": complete_sources,
        "requested_project_ids": project_ids,
        "date": wanted,
        "events": events,
    }


def _install_daily_events_endpoint(dd, trmt_app, dd_app):
    """Install the one read-only integration route on the mounted app."""
    from flask import jsonify, request

    def check_trmt_api_key():
        # app.py exposes the helper in its module namespace in production;
        # importing the shared symbol is the compatibility path for the Flask
        # object passed by wsgi.py and for lightweight test apps.
        checker = getattr(trmt_app, "_check_api_key", None)
        if not callable(checker):
            from helpers_shared import _check_api_key as checker
        return bool(checker())

    dd_app._trmt_check_daily_api_key = check_trmt_api_key

    if getattr(dd_app, "_trmt_daily_events_installed", False):
        return

    @dd_app.route(DAILY_EVENTS_PATH, methods=["GET"])
    def _trmt_daily_events():
        raw_projects = request.args.get("project_ids", "")
        project_ids = []
        try:
            decoded = json.loads(raw_projects)
            raw_values = decoded if isinstance(decoded, list) else raw_projects.split(",")
        except (TypeError, ValueError):
            raw_values = raw_projects.split(",")
        for value in raw_values:
            value = _text(value)
            if value and value not in project_ids:
                if not re.fullmatch(r"v_[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
                    return jsonify({"error": "project_ids must contain Dock Manager v_ ids"}), 400
                project_ids.append(value)
        wanted = request.args.get("date", "")
        try:
            parsed_date = _date.fromisoformat(wanted)
        except (TypeError, ValueError):
            parsed_date = None
        if not project_ids or not parsed_date or parsed_date.isoformat() != wanted or not _DATE_RE.match(wanted):
            return jsonify({"error": "project_ids and date=YYYY-MM-DD are required"}), 400
        try:
            return jsonify(_daily_events_payload(dd, dd_app, project_ids, wanted))
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            # No partial events are ever returned.  Keep source details out of
            # the response because this endpoint is also reachable by a key.
            dd_app.logger.warning("daily-events unavailable: %s", exc)
            return jsonify({"error": "daily event sources unavailable", "complete": False}), 503

    dd_app._trmt_daily_events_installed = True


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
    _install_daily_events_endpoint(dd, trmt_app, dd_app)
    # is_viewer/can_access_vessel도 세션 유저 기준으로 동작(원본이 get_current_user 참조)

    # ── 3. 전 route admin 게이팅 + login/logout 차단 (결정2, C2·C3 해소) ─
    @dd_app.before_request
    def _sso_admin_gate():
        # The read-only daily adapter is the sole API-key exception.  Keep the
        # comparison exact (path and method) so a trailing-slash, case, or
        # neighbouring Dock Manager API cannot inherit TRMT key access.
        if request.path == DAILY_EVENTS_PATH and request.method == "GET":
            if not dd_app._trmt_check_daily_api_key():
                return jsonify({"error": "Unauthorized"}), 401
            return None
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
    # The daily adapter must remain strictly read-only; retain a connection
    # factory that predates the general Dock Manager hardening wrapper so its
    # fallback test-double path never executes WAL/PRAGMA writes.
    dd._trmt_original_get_db = _orig_get_db
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

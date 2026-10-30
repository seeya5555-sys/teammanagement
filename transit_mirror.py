"""통항 미러 — 외부 통항 모니터링 사이트(소유자 승인)의 읽기전용 미러.

- 외부 GET JSON 3종(/api/vessels, /api/transit-plan/today, /api/transit-plan/registry)만 호출.
  POST·TRMT DB 쓰기 없음.
- 메모리 캐시(TTL 5분). 업스트림 장애 시 직전 캐시를 stale=True 로 반환, 캐시도 없으면 빈 목록.
  어떤 경우에도 500 을 내지 않는다.
- 로그인 감독의 supervisor_vessels 배정선만, 정규화 선명(대문자·영숫자만) 일치로 노출.
- 출력은 화이트리스트 필드만(선원·연료·보너스·용선사 동의 상세 미노출; 동의는 verdict 요약만).
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, session

from app_core import query
from helpers_shared import login_required

bp = Blueprint('transit_mirror', __name__)
log = logging.getLogger(__name__)

DEFAULT_URL = 'http://158.180.94.133:5000'
TTL_SEC = 300
TIMEOUT_SEC = 8
KST = timezone(timedelta(hours=9))
REGISTRY_DAYS = 3  # 오늘 포함 앞으로 N일

VESSEL_FIELDS = ('latitude', 'longitude', 'positionSource', 'positionLastUpdate', 'positionTs',
                 'speed', 'category', 'condition', 'presentLocation', 'nextPort', 'eta', 'etd',
                 'hormuzExit', 'transit48h', 'transitPlan')
CP_FIELDS = ('lt', 'z', 'real')
CONSENT_FIELDS = ('verdict', 'reviewed', 'reviewedAt', 'dateUsed', 'sameDate')

_lock = threading.Lock()
_cache = {'at': 0.0, 'data': None}


def base_url():
    return (os.environ.get('TRANSIT_MIRROR_URL') or DEFAULT_URL).rstrip('/')


def _scalar(x):
    """스칼라만 통과(str/int/float/bool/None). dict/list 등 중첩 객체는 None 으로 드롭."""
    if x is None or isinstance(x, (str, bool, int, float)):
        return x
    return None


def norm_name(s):
    return re.sub(r'[^A-Z0-9]', '', str(s or '').upper())


def _get_json(path):
    req = urllib.request.Request(base_url() + path, method='GET',
                                 headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode('utf-8'))


def _fetch_upstream():
    vessels = _get_json('/api/vessels')
    if not isinstance(vessels, list):
        raise ValueError('vessels payload not a list')
    out = {'vessels': vessels, 'today': {}, 'registry': {}}
    # 보조 엔드포인트 실패는 선위 미러를 막지 않는다.
    try:
        t = _get_json('/api/transit-plan/today')
        out['today'] = t if isinstance(t, dict) else {}
    except Exception:
        pass
    try:
        rg = _get_json('/api/transit-plan/registry')
        out['registry'] = (rg.get('registry') or {}) if isinstance(rg, dict) else {}
    except Exception:
        pass
    return out


def get_upstream(now=None):
    """(data|None, stale:bool, fetched_at:float)."""
    now = time.time() if now is None else now
    with _lock:
        if _cache['data'] is not None and now - _cache['at'] < TTL_SEC:
            return _cache['data'], False, _cache['at']
        try:
            data = _fetch_upstream()
        except Exception:
            return _cache['data'], True, _cache['at']
        _cache['data'] = data
        _cache['at'] = now
        return data, False, now


def reset_cache():
    with _lock:
        _cache['data'] = None
        _cache['at'] = 0.0


def _assigned_names():
    sup_id = session.get('supervisor_id')
    if not sup_id:
        return {}
    # TRMT 선명 정규화 충돌(배정 여부 무관, 전 선박 기준) → 해당 키는 fail-closed 제외.
    counts = {}
    for r in query('SELECT name FROM vessels'):
        k = norm_name(r['name'])
        counts[k] = counts.get(k, 0) + 1
    rows = query('SELECT v.name FROM supervisor_vessels sv JOIN vessels v ON v.id=sv.vessel_id '
                 'WHERE sv.supervisor_id=? AND v.active=1', (sup_id,))
    return {norm_name(r['name']): r['name'] for r in rows
            if norm_name(r['name']) and counts.get(norm_name(r['name'])) == 1}


def _checkpoints(registry, key, today):
    days = {(today + timedelta(days=i)).strftime('%Y%m%d') for i in range(REGISTRY_DAYS)}
    out = []
    for day in sorted(registry or {}):
        if day not in days:
            continue
        for e in registry.get(day) or []:
            if not isinstance(e, dict):
                continue
            if norm_name(e.get('key') or e.get('vessel')) != key:
                continue
            cps = {}
            for k, cp in (e.get('cps') or {}).items():
                if isinstance(cp, dict):
                    cps[str(k)] = {f: _scalar(cp.get(f)) for f in CP_FIELDS}
            out.append({'date': day, 'bound': _scalar(e.get('bound')) or '', 'cps': cps})
    return out


def build_payload(data, assigned, today=None):
    today = today or datetime.now(KST).date()
    if not data:
        return []
    consent = (((data.get('today') or {}).get('consent') or {}).get('byVessel') or {})
    registry = data.get('registry') or {}
    upstream = [v for v in (data.get('vessels') or []) if isinstance(v, dict)]
    seen = {}
    for v in upstream:
        k = norm_name(_scalar(v.get('name')))
        seen[k] = seen.get(k, 0) + 1
    out = []
    for v in upstream:
        key = norm_name(_scalar(v.get('name')))
        # 업스트림 정규화 선명 중복 → fail-closed 제외
        if not key or key not in assigned or seen.get(key) != 1:
            continue
        item = {'name': _scalar(v.get('name')) or '', 'trmtName': assigned[key], 'key': key}
        for f in VESSEL_FIELDS:
            item[f] = _scalar(v.get(f))
        c = consent.get(key) if isinstance(consent, dict) else None
        item['consent'] = {f: _scalar(c.get(f)) for f in CONSENT_FIELDS} if isinstance(c, dict) else None
        item['checkpoints'] = _checkpoints(registry, key, today)
        out.append(item)
    out.sort(key=lambda x: x['name'])
    return out


@bp.route('/api/transit-mirror')
@login_required
def api_transit_mirror():
    try:
        data, stale, at = get_upstream()
        vessels = build_payload(data, _assigned_names())
        resp = jsonify({
            'ok': data is not None,
            'stale': bool(stale),
            'fetchedAt': datetime.fromtimestamp(at, KST).strftime('%Y-%m-%d %H:%M') if at else None,
            'source': base_url() + '/?transit=1',
            'vessels': vessels,
        })
    except Exception:
        log.exception('transit mirror failed')
        resp = jsonify({'ok': False, 'stale': True, 'fetchedAt': None, 'vessels': []})
    resp.headers['Cache-Control'] = 'no-store, private'
    return resp

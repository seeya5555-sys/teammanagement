"""통항 미러 — 외부 통항 모니터링 사이트(소유자 승인)의 읽기전용 미러.

- 외부 GET JSON 3종(/api/vessels, /api/transit-plan/today, /api/transit-plan/registry)만 호출.
  POST·TRMT DB 쓰기 없음.
- 메모리 캐시(TTL 5분). 업스트림 장애 시 직전 캐시를 stale=True 로 반환, 캐시도 없으면 빈 목록.
  어떤 경우에도 500 을 내지 않는다.
- 로그인 감독의 supervisor_vessels 배정선만, 정규화 선명(대문자·영숫자만) 일치로 노출.
- 출력은 화이트리스트 필드만(선원·연료·보너스·용선사 동의 상세 미노출; 동의는 verdict 요약만).
"""
import copy
import json
import math
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


def _get_json(path, timeout=TIMEOUT_SEC):
    req = urllib.request.Request(base_url() + path, method='GET',
                                 headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _fetch_upstream():
    # 3개 호출 전체를 TIMEOUT_SEC 하나의 데드라인 안에 묶는다(Fleet Map 지연 상한).
    deadline = time.monotonic() + TIMEOUT_SEC

    def left():
        rem = deadline - time.monotonic()
        if rem <= 0.2:
            raise TimeoutError('transit mirror deadline')
        return rem

    vessels = _get_json('/api/vessels', timeout=left())
    if not isinstance(vessels, list):
        raise ValueError('vessels payload not a list')
    out = {'vessels': vessels, 'today': {}, 'registry': {}}
    # 보조 엔드포인트 실패는 선위 미러를 막지 않는다.
    try:
        t = _get_json('/api/transit-plan/today', timeout=left())
        out['today'] = t if isinstance(t, dict) else {}
    except Exception:
        pass
    try:
        rg = _get_json('/api/transit-plan/registry', timeout=left())
        out['registry'] = (rg.get('registry') or {}) if isinstance(rg, dict) else {}
    except Exception:
        pass
    return out


def _with_deadline(fn, secs):
    """fn() 을 별도 스레드에서 실행하고 secs(벽시계, 연결+읽기 전체) 안에 끝나지 않으면 TimeoutError.
    느린 read(트리클)도 소켓 timeout 만으로는 못 막으므로 join(timeout) 으로 상한을 강제한다."""
    box = {}

    def run():
        try:
            box['v'] = fn()
        except BaseException as e:  # noqa: BLE001 — 호출측에 그대로 전달
            box['e'] = e
    t = threading.Thread(target=run, name='transit-mirror-fetch', daemon=True)
    t.start()
    t.join(secs)
    if t.is_alive():
        raise TimeoutError('transit mirror wall-clock deadline')
    if 'e' in box:
        raise box['e']
    return box['v']


_vcache = {'at': 0.0, 'vessels': None}


def get_vessels_for_fleet(now=None):
    """Fleet Map 오버레이용 — /api/vessels 만. 전체 캐시가 신선하면 재사용, 아니면 vessels 만
    벽시계 TIMEOUT_SEC 안에서 1회 조회. 실패 시 직전 값(없으면 None)."""
    now = time.time() if now is None else now
    with _lock:
        if _cache['data'] is not None and now - _cache['at'] < TTL_SEC:
            return _cache['data'].get('vessels')
        if _vcache['vessels'] is not None and now - _vcache['at'] < TTL_SEC:
            return _vcache['vessels']
        try:
            vs = _with_deadline(lambda: _get_json('/api/vessels', timeout=TIMEOUT_SEC), TIMEOUT_SEC)
            if not isinstance(vs, list):
                raise ValueError('vessels payload not a list')
        except Exception:
            return _vcache['vessels'] if _vcache['vessels'] is not None else (
                _cache['data'] or {}).get('vessels')
        _vcache['vessels'], _vcache['at'] = vs, now
        return vs


def get_upstream(now=None):
    """(data|None, stale:bool, fetched_at:float)."""
    now = time.time() if now is None else now
    with _lock:
        if _cache['data'] is not None and now - _cache['at'] < TTL_SEC:
            return _cache['data'], False, _cache['at']
        try:
            data = _with_deadline(_fetch_upstream, TIMEOUT_SEC + 0.5)
        except Exception:
            return _cache['data'], True, _cache['at']
        _cache['data'] = data
        _cache['at'] = now
        return data, False, now


def reset_cache():
    with _lock:
        _cache['data'] = None
        _cache['at'] = 0.0
        _vcache['vessels'] = None
        _vcache['at'] = 0.0


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


MIRROR_FRESH_SEC = 72 * 3600


def _valid_epoch(x, now):
    if isinstance(x, bool) or not isinstance(x, (int, float, str)):
        return None
    try:
        ep = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ep) or ep < 946684800 or ep > now + 86400:
        return None
    return ep


def _valid_latlng(v):
    try:
        la, lo = float(v.get('latitude')), float(v.get('longitude'))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(la) and math.isfinite(lo)) or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    if la == 0 and lo == 0:
        return None
    return la, lo


def fleet_positions(now=None):
    """로그인 감독 배정선의 미러 선위 {정규화선명: {...}}. 실패/캐시없음 → {} (기존 동작 유지).

    배정·충돌 fail-closed 규칙은 /api/transit-mirror 와 동일(build_payload 재사용).
    """
    now = time.time() if now is None else now
    vessels = get_vessels_for_fleet(now)
    if not vessels:
        return {}
    data = {'vessels': vessels}
    out = {}
    for item in build_payload(data, _assigned_names()):
        ll = _valid_latlng(item)
        ep = _valid_epoch(item.get('positionTs'), now)
        if not ll or ep is None:
            continue
        spd = item.get('speed')
        try:
            spd = float(spd) if spd not in (None, '') else None
            if spd is not None and not math.isfinite(spd):
                spd = None
        except (TypeError, ValueError):
            spd = None
        out[item['key']] = {'lat': ll[0], 'lng': ll[1], 'ts': ep, 'speed': spd,
                            'source': str(item.get('positionSource') or ''),
                            'last': str(item.get('positionLastUpdate') or '')}
    return out


def _overlay_one(v, m, current_epoch, now):
    """선박 1척의 덮어쓴 사본(dict) 또는 None(유지). 원본 v 는 건드리지 않는다."""
    # 미러가 권위(사용자 지시): 72h 이내(경계 포함)면 무조건 미러.
    # 72h 초과면 현재 선위보다 '엄격히' 최신일 때만(동시각·시각불명이면 유지).
    if now - m['ts'] > MIRROR_FRESH_SEC:
        cur = current_epoch(v)
        if cur is None or m['ts'] <= cur:
            return None
    nv = copy.deepcopy(v)
    nv['lat'], nv['lng'] = m['lat'], m['lng']
    if m['speed'] is not None:
        nv['speed'] = m['speed']
    ts_txt = datetime.fromtimestamp(m['ts'], KST).strftime('%Y-%m-%d %H:%M')
    src = m['source'] or '미상'
    if src.upper() == 'EMAIL':
        src = 'E-mail'
    nv['pos_source'] = 'mirror'
    last = m['last'] or ts_txt
    # 원 사이트 문구가 이미 출처를 품으면('E-mail(…)', '수동(…)') 그대로, 아니면 출처를 앞에 붙인다.
    nv['pos_mirror_label'] = '팀장님 사이트(%s)' % (last if '(' in last else '%s %s' % (src, last))
    nv['position_source'] = 'MIRROR ' + (m['source'] or '')
    nv['position_ts'] = ts_txt
    nv['pos_reported_at'] = ts_txt
    nv['position_ts_epoch'] = m['ts']
    nv['ais_stale'] = False
    nv['email_pending'] = False
    return nv


def apply_fleet_overrides(fleet, current_epoch, skip_keys=(), now=None):
    """Fleet Map 선위를 미러 선위로 덮은 **새 리스트**와 덮은 척수 (fleet, n) 반환. DB 쓰기 없음.

    원본 fleet/항목 dict 는 절대 변경하지 않는다(덮을 항목만 deepcopy). 전체가 성공해야만
    새 리스트를 돌려주고, 중간에 어떤 예외든 나면 원본 fleet 을 그대로 돌려준다.
    """
    now = time.time() if now is None else now
    try:
        mirror = fleet_positions(now)
        if not mirror:
            return fleet, 0
        out, n = [], 0
        for v in fleet:
            key = norm_name(v.get('name'))
            m = mirror.get(key)
            nv = None
            if m and key not in skip_keys:
                nv = _overlay_one(v, m, current_epoch, now)
            if nv is None:
                out.append(v)
            else:
                out.append(nv)
                n += 1
        return out, n
    except Exception:
        log.exception('transit mirror fleet overlay failed')
        return fleet, 0


def _fleet_overlay_hook(fleet, current_epoch, skip_names=()):
    return apply_fleet_overrides(fleet, current_epoch, skip_keys={norm_name(n) for n in skip_names})


@bp.record_once
def _register_overlay(state):
    # routes_tail(Fleet Map)이 형제 모듈을 import 하지 않고 쓰도록 훅 등록.
    state.app.extensions['transit_mirror_overlay'] = _fleet_overlay_hook


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

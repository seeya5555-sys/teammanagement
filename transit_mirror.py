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

from flask import Blueprint, jsonify, request, session

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


_vcache = {'at': 0.0, 'data': None}
_gen = {'n': 0}            # reset_cache 마다 증가 — 진행 중 갱신이 리셋 뒤에 되살리지 못하게
_inflight = {}             # 이름 -> threading.Event (키별 single-flight)


def _single_flight(name, read, commit, fetch):
    """잠금은 캐시 읽기/커밋에만. 업스트림 I/O 는 잠금 밖에서, 같은 키는 한 번만(나머지는 대기).

    read() -> (data, at, fresh) · commit(data, at) · fetch() -> data(예외 가능)
    반환 (data|None, stale, at).
    """
    with _lock:
        data, at, fresh = read()
        if fresh:
            return data, False, at
        gen = _gen['n']
        ev = _inflight.get(name)
        leader = ev is None
        if leader:
            ev = _inflight[name] = threading.Event()
    if not leader:
        ev.wait(TIMEOUT_SEC + 1)
        with _lock:
            data, at, fresh = read()
        return data, not fresh, at
    try:
        try:
            new = fetch()
            ok = True
        except Exception:
            new, ok = None, False
        now = time.time()
        with _lock:
            if ok and gen == _gen['n']:
                commit(new, now)
            data, at, _fresh = read()
        if ok:
            return new, False, now
        return data, True, at
    finally:
        with _lock:
            if _inflight.get(name) is ev:
                _inflight.pop(name, None)
        ev.set()


def _ent_reader(ent, now=None):
    def read():
        t = time.time() if now is None else now
        d = ent.get('data')
        return d, ent.get('at', 0.0), d is not None and t - ent.get('at', 0.0) < TTL_SEC
    return read


def _ent_commit(ent):
    def commit(data, at):
        ent['data'], ent['at'] = data, at
    return commit


def get_vessels_for_fleet(now=None):
    """Fleet Map 오버레이용 — /api/vessels 만. 전체 캐시가 신선하면 재사용, 아니면 vessels 만
    벽시계 TIMEOUT_SEC 안에서 1회 조회(잠금 밖). 실패 시 직전 값(없으면 None)."""
    now = time.time() if now is None else now
    with _lock:
        if _cache['data'] is not None and now - _cache['at'] < TTL_SEC:
            return _cache['data'].get('vessels')

    def fetch():
        vs = _with_deadline(lambda: _get_json('/api/vessels', timeout=TIMEOUT_SEC), TIMEOUT_SEC)
        if not isinstance(vs, list):
            raise ValueError('vessels payload not a list')
        return vs
    data, _stale, _at = _single_flight('vessels', _ent_reader(_vcache, now), _ent_commit(_vcache), fetch)
    if data is None:
        with _lock:
            data = (_cache['data'] or {}).get('vessels')
    return data


def get_upstream(now=None):
    """(data|None, stale:bool, fetched_at:float)."""
    return _single_flight('full', _ent_reader(_cache, now), _ent_commit(_cache),
                          lambda: _with_deadline(_fetch_upstream, TIMEOUT_SEC + 0.5))


def reset_cache():
    with _lock:
        _gen['n'] += 1
        _cache['data'] = None
        _cache['at'] = 0.0
        _vcache['data'] = None
        _vcache['at'] = 0.0
        _mcache.clear()
        _dcache['data'] = None
        _dcache['at'] = 0.0


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


def transit_today_keys(data, today=None):
    """오늘(KST) 통항 목록 선박의 정규화 키 집합 — registry[오늘] ∪ (daily.date==오늘이면) daily.rows."""
    today = today or datetime.now(KST).date()
    tk = today.strftime('%Y%m%d')
    keys = set()
    if not isinstance(data, dict):
        return keys
    regs = [data.get('registry') or {}, ((data.get('today') or {}).get('registry') or {})]
    for reg in regs:
        if isinstance(reg, dict):
            for e in reg.get(tk) or []:
                if isinstance(e, dict):
                    k = norm_name(_scalar(e.get('key')) or _scalar(e.get('vessel')))
                    if k:
                        keys.add(k)
    daily = (data.get('today') or {}).get('daily') or {}
    if isinstance(daily, dict) and str(_scalar(daily.get('date')) or '') == tk:
        for r in daily.get('rows') or []:
            if isinstance(r, dict):
                k = norm_name(_scalar(r.get('vessel')))
                if k:
                    keys.add(k)
    return keys


def build_payload(data, assigned, today=None):
    today = today or datetime.now(KST).date()
    if not data:
        return []
    consent = (((data.get('today') or {}).get('consent') or {}).get('byVessel') or {})
    registry = data.get('registry') or {}
    today_keys = transit_today_keys(data, today)
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
        item['transit_today'] = key in today_keys
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
        assigned = _assigned_names()
        tkeys = {k for k in cached_transit_today_keys() if k in assigned}
        if not mirror and not tkeys:
            return fleet, 0
        out, n = [], 0
        for v in fleet:
            key = norm_name(v.get('name'))
            m = mirror.get(key)
            nv = None
            if m and key not in skip_keys:
                nv = _overlay_one(v, m, current_epoch, now)
                if nv is not None:
                    n += 1
            if key in tkeys:
                nv = nv if nv is not None else copy.deepcopy(v)
                nv['transit_today'] = True
            out.append(v if nv is None else nv)
        return out, n
    except Exception:
        log.exception('transit mirror fleet overlay failed')
        return fleet, 0


_warm = {'running': False}
_warm_lock = threading.Lock()


def _warm_full_cache():
    # 잠금 밖에서 받아 오고 커밋만 잠근다(get_upstream 의 single-flight·세대 토큰 공유).
    try:
        get_upstream()
    except Exception:
        pass
    finally:
        with _warm_lock:
            _warm['running'] = False


def cached_transit_today_keys():
    """Fleet Map 용 — 캐시에 있는 전체 번들에서만 읽는다(요청을 막지 않음).
    캐시가 비었거나 만료면 백그라운드 갱신을 **한 개만** 걸고, 이번 응답은 직전 값(없으면 빈 집합)."""
    with _lock:
        data, at = _cache['data'], _cache['at']
    if data is None or time.time() - at >= TTL_SEC:
        with _warm_lock:
            start = not _warm['running']
            if start:
                _warm['running'] = True
        if start:
            try:
                threading.Thread(target=_warm_full_cache, name='transit-mirror-warm', daemon=True).start()
            except Exception:
                with _warm_lock:
                    _warm['running'] = False
    return transit_today_keys(data) if data else set()


def _fleet_overlay_hook(fleet, current_epoch, skip_names=()):
    return apply_fleet_overrides(fleet, current_epoch, skip_keys={norm_name(n) for n in skip_names})


@bp.record_once
def _register_overlay(state):
    # routes_tail(Fleet Map)이 형제 모듈을 import 하지 않고 쓰도록 훅 등록.
    state.app.extensions['transit_mirror_overlay'] = _fleet_overlay_hook


# ───────────────────────── 통항 모니터링(전체 선박, 읽기전용 미러) ─────────────────────────
# 사용 업스트림(GET only): /api/transit-plan/today[?date=YYYYMMDD], /api/transit-plan/dates?limit=30,
#   /api/vessels(통항 중 선박의 최신 보고시각). 사이트 소유자·사용자 승인 하에 전체 선박을 보인다.
#   선원 명단·지적문구·보너스·연료 등은 내보내지 않는다(동의서는 인원·장수·판정 요약만).
GULF = timezone(timedelta(hours=4))
CP_KEYS = ('a', 's', 'd', 'h', 'z')
DATE_RE = re.compile(r'^\d{8}$')
_mcache = {}          # date('' = 최신) -> {'at', 'data'}
_dcache = {'at': 0.0, 'data': None}


def _monitor_fetch(date_key):
    def go():
        q = '?date=%s' % date_key if date_key else ''
        b = _get_json('/api/transit-plan/today' + q, timeout=TIMEOUT_SEC)
        if not isinstance(b, dict):
            raise ValueError('bundle not dict')
        return b
    return _with_deadline(go, TIMEOUT_SEC)


def get_monitor_bundle(date_key, now=None):
    def read():
        t = time.time() if now is None else now
        ent = _mcache.get(date_key) or {}
        d = ent.get('data')
        return d, ent.get('at', 0.0), d is not None and t - ent.get('at', 0.0) < TTL_SEC

    def commit(data, at):
        if len(_mcache) > 40:
            _mcache.clear()
        _mcache[date_key] = {'at': at, 'data': data}
    return _single_flight('monitor:' + date_key, read, commit, lambda: _monitor_fetch(date_key))


def get_monitor_dates(now=None):
    def fetch():
        j = _with_deadline(lambda: _get_json('/api/transit-plan/dates?limit=30', timeout=TIMEOUT_SEC),
                           TIMEOUT_SEC)
        return [str(d) for d in (j.get('dates') or []) if DATE_RE.match(str(d))] if isinstance(j, dict) else []
    data, _stale, _at = _single_flight('dates', _ent_reader(_dcache, now), _ent_commit(_dcache), fetch)
    return data or []


def _lt_to_utc_epoch(iso, hhmm):
    try:
        dt = datetime.strptime(str(iso) + str(hhmm).replace(':', '').zfill(4), '%Y%m%d%H%M')
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=GULF).timestamp()


def _lt_text_epoch(text, ref_key):
    """'30/11:45LT' · '29 / 21:30' → UTC epoch (걸프 LT=UTC+4, 월은 기준일에서 추론)."""
    m = re.match(r'^\s*(\d{1,2})\s*/\s*(\d{1,2}):?(\d{2})', str(text or ''))
    if not m or not DATE_RE.match(str(ref_key or '')):
        return None
    day, hh, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ref = datetime.strptime(ref_key, '%Y%m%d')
    y, mo = ref.year, ref.month
    if day - ref.day > 15:
        mo -= 1
    elif ref.day - day > 15:
        mo += 1
    if mo == 0:
        y, mo = y - 1, 12
    elif mo == 13:
        y, mo = y + 1, 1
    try:
        return datetime(y, mo, day, hh, mi, tzinfo=GULF).timestamp()
    except ValueError:
        return None


def _cp_out(cp, ref_key):
    if not isinstance(cp, dict):
        return None
    utc = None
    if _scalar(cp.get('iso')) and _scalar(cp.get('hhmm')):
        utc = _lt_to_utc_epoch(cp.get('iso'), cp.get('hhmm'))
    if utc is None:
        utc = _lt_text_epoch(_scalar(cp.get('lt')) or _scalar(cp.get('t')), ref_key)
    lt = str(_scalar(cp.get('lt')) or '').replace('LT', '').strip()
    return {'lt': lt, 'real': bool(_scalar(cp.get('real'))), 'utc': utc,
            't': str(_scalar(cp.get('t')) or '')[:80]}


def _cps_map(raw, ref_key):
    out = {}
    items = raw.items() if isinstance(raw, dict) else (
        ((_scalar(c.get('key')), c) for c in raw if isinstance(c, dict)) if isinstance(raw, list) else ())
    for k, cp in items:
        k = str(k or '').lower()
        if k in CP_KEYS:
            c = _cp_out(cp, ref_key)
            if c:
                out[k] = c
    return out


def _bound(b):
    b = str(_scalar(b) or '').strip().upper()
    if re.match(r'^(E|OUT)', b):
        return 'OUTBOUND'
    if re.match(r'^(W|IN)', b):
        return 'INBOUND'
    return ''


def _consent_out(c):
    if not isinstance(c, dict):
        return None
    return {f: _scalar(c.get(f)) for f in ('crew', 'signs', 'files', 'issues', 'reviewed',
                                           'reviewedAt', 'verdict', 'sameDate', 'dateUsed')}


def _rows_for(bundle, date_key, vessels_by_key, now):
    rows = {}

    def row(name):
        k = norm_name(_scalar(name))
        if not k:
            return None
        if k not in rows:
            rows[k] = {'vessel': str(_scalar(name)), 'key': k, 'registered': False, 'bound': '',
                       'mgr': '', 'cps': {}, 'rec': None, 'consent': None}
        return rows[k]

    reg = (bundle.get('registry') or {}) if isinstance(bundle.get('registry'), dict) else {}
    for e in reg.get(date_key) or []:
        if isinstance(e, dict):
            r = row(e.get('vessel') or e.get('key'))
            if r:
                r['registered'] = True
                r['bound'] = r['bound'] or _bound(e.get('bound'))
                r['mgr'] = r['mgr'] or str(_scalar(e.get('mgr')) or '')
                r['cps'] = r['cps'] or _cps_map(e.get('cps'), date_key)
    daily = bundle.get('daily') if isinstance(bundle.get('daily'), dict) else {}
    prepare = bundle.get('prepare') if isinstance(bundle.get('prepare'), dict) else {}
    if str(_scalar(daily.get('date')) or '') == date_key:
        for src in (daily.get('rows') or [], prepare.get('rows') or []):
            for e in src:
                if isinstance(e, dict):
                    r = row(e.get('vessel'))
                    if r:
                        r['bound'] = r['bound'] or _bound(e.get('bound'))
                        r['mgr'] = r['mgr'] or str(_scalar(e.get('mgr')) or '')
                        r['cps'] = r['cps'] or _cps_map(e.get('cps'), date_key)
                        if r['consent'] is None:
                            r['consent'] = _consent_out(e.get('consent'))
    rec = bundle.get('record') if isinstance(bundle.get('record'), dict) else {}
    for e in rec.get('rows') or []:
        if isinstance(e, dict) and str(_scalar(e.get('startIso')) or '') == date_key:
            r = row(e.get('vessel'))
            if r:
                r['bound'] = r['bound'] or _bound(e.get('bound'))
                r['rec'] = {'done': bool(_scalar(e.get('done'))), 'aborted': bool(_scalar(e.get('aborted'))),
                            'status': str(_scalar(e.get('status')) or '')[:80],
                            'cps': _cps_map(e.get('cps'), date_key)}
    consent = ((bundle.get('consent') or {}).get('byVessel') or {}) if isinstance(bundle.get('consent'), dict) else {}
    out = []
    for k, r in rows.items():
        if r['consent'] is None and isinstance(consent, dict):
            r['consent'] = _consent_out(consent.get(k))
        times = [c['utc'] for c in list(r['cps'].values()) + list(((r['rec'] or {}).get('cps') or {}).values())
                 if c.get('utc')]
        r['startUtc'] = min(times) if times else None
        r['endUtc'] = max(times) if times else None
        done = bool(r['rec'] and (r['rec']['done'] or r['rec']['aborted']))
        r['transiting'] = bool(times and not done and r['startUtc'] <= now <= r['endUtc'])
        r['beforeStart'] = bool(times and now < r['startUtc'])
        v = vessels_by_key.get(k)
        r['report'] = ({'when': str(_scalar(v.get('positionLastUpdate')) or ''),
                        'ts': _valid_epoch(v.get('positionTs'), now),
                        'source': str(_scalar(v.get('positionSource')) or '')} if v else None)
        out.append(r)
    out.sort(key=lambda r: (r['startUtc'] is None, r['startUtc'] or 0, r['vessel']))
    return out


def build_monitor(bundle, dates, vessels, date_key, today_key, now):
    vby, seen = {}, {}
    for v in vessels or []:
        if isinstance(v, dict):
            k = norm_name(_scalar(v.get('name')))
            seen[k] = seen.get(k, 0) + 1
            vby[k] = v
    vby = {k: v for k, v in vby.items() if seen.get(k) == 1}
    bundle = bundle if isinstance(bundle, dict) else {}
    rows = _rows_for(bundle, date_key, vby, now)
    daily = bundle.get('daily') if isinstance(bundle.get('daily'), dict) else {}
    prepare = bundle.get('prepare') if isinstance(bundle.get('prepare'), dict) else {}
    reg = bundle.get('registry') if isinstance(bundle.get('registry'), dict) else {}
    keys = set(d for d in (dates or []) if DATE_RE.match(d)) | {k for k in reg if DATE_RE.match(str(k))}
    base = datetime.strptime(today_key, '%Y%m%d')
    keys |= {(base + timedelta(days=i)).strftime('%Y%m%d') for i in range(3)}
    keys.add(date_key)
    counts = []
    for k in sorted(keys):
        if k == date_key:
            n, nt = len(rows), sum(1 for r in rows if r['transiting'])
        else:
            n = len({norm_name(_scalar(e.get('vessel')) or _scalar(e.get('key')))
                     for e in (reg.get(k) or []) if isinstance(e, dict)}) or None
            nt = None
        counts.append({'date': k, 'count': n, 'transiting': nt})
    memos = []
    if str(_scalar(daily.get('date')) or '') == date_key:
        memos = [str(n)[:2000] for n in (daily.get('notes') or []) if _scalar(n) and str(n).strip()]
    stamp = str(_scalar(prepare.get('stamp')) or '')
    return {
        'date': date_key, 'today': today_key,
        'basis': {'basis': str(_scalar(prepare.get('basis')) or ''),
                  'basisKst': str(_scalar(prepare.get('basisKst')) or ''),
                  'stamp': stamp, 'dailyDate': str(_scalar(daily.get('date')) or '')},
        'dates': counts, 'rows': rows, 'memos': memos,
    }


def _monitor_response(date_arg):
    now = time.time()
    today_key = datetime.now(KST).strftime('%Y%m%d')
    date_key = date_arg or today_key
    bundle, stale, at = get_monitor_bundle(date_arg or '')
    if bundle is None:
        return {'ok': False, 'stale': True, 'fetchedAt': None, 'date': date_key, 'today': today_key,
                'dates': [], 'rows': [], 'memos': [], 'basis': {}}
    try:
        vessels = (_cache['data'] or {}).get('vessels') or get_vessels_for_fleet(now) or []
    except Exception:
        vessels = []
    out = build_monitor(bundle, get_monitor_dates(now), vessels, date_key, today_key, now)
    out.update({'ok': True, 'stale': bool(stale),
                'fetchedAt': datetime.fromtimestamp(at, KST).strftime('%Y-%m-%d %H:%M') if at else None,
                'nowKst': datetime.fromtimestamp(now, KST).strftime('%Y-%m-%d %H:%M')})
    return out


@bp.route('/api/transit-mirror/monitor')
@login_required
def api_transit_mirror_monitor():
    date_arg = (request.args.get('date') or '').strip()
    if date_arg:
        try:
            if not DATE_RE.match(date_arg):
                raise ValueError
            datetime.strptime(date_arg, '%Y%m%d')
        except ValueError:
            resp = jsonify({'ok': False, 'error': 'bad date (YYYYMMDD)'})
            resp.status_code = 400
            resp.headers['Cache-Control'] = 'no-store, private'
            return resp
    try:
        resp = jsonify(_monitor_response(date_arg))
    except Exception:
        log.exception('transit monitor failed')
        resp = jsonify({'ok': False, 'stale': True, 'date': date_arg, 'dates': [], 'rows': [], 'memos': [],
                        'basis': {}})
    resp.headers['Cache-Control'] = 'no-store, private'
    return resp


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

"""
KR-Con (Korean Register KR-CON) 조회 클라이언트 — 서버사이드, stdlib 전용.

TRMT 서버에서 KR선급 KR-CON(클래스룰·IMO·SOLAS·코드·resolution·circular·IACS)
레퍼런스를 검색/추출한다. requests·bs4 의존 없음(urllib + 정규식).

계정: 환경변수 KRCON_USER / KRCON_PW (/etc/trmt.env). 없으면 에러 반환.
⚠️ 공용 단일세션 계정 — 재로그인 시 다른 세션 튕김. 쿠키를 파일에 캐시해
   stale일 때만 재로그인한다.
"""
import os
import re
import json
import base64
import fcntl
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
import html as _html


class KrconError(Exception):
    """KR-CON 접근 실패(네트워크/로그인 등) — route에서 error dict로 정규화."""

BASE = os.environ.get('KRCON_BASE', 'https://krcon.krs.co.kr')
# 쿠키는 예측가능한 /tmp 대신 app-private 디렉터리(0700)에 저장(symlink/race 완화)
_DATA_DIR = os.environ.get(
    'KRCON_DATA',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '.krcon_session'))
try:
    os.makedirs(_DATA_DIR, mode=0o700, exist_ok=True)
    os.chmod(_DATA_DIR, 0o700)
except Exception:
    pass
COOKIE_PATH = os.environ.get('KRCON_COOKIE', os.path.join(_DATA_DIR, 'cookies.txt'))
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
TIMEOUT = 45


def _creds():
    return os.environ.get('KRCON_USER', ''), os.environ.get('KRCON_PW', '')


def _b64(s):
    return base64.b64encode(s.encode('utf-8')).decode('ascii')


def _opener():
    cj = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
    if os.path.exists(COOKIE_PATH):
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [('User-Agent', UA), ('Referer', BASE + '/'),
                     ('X-Requested-With', 'XMLHttpRequest')]
    return op, cj


def _post(op, path, data):
    req = urllib.request.Request(
        BASE + path, data=urllib.parse.urlencode(data).encode('utf-8'),
        method='POST')
    with op.open(req, timeout=TIMEOUT) as r:
        return r.read().decode('utf-8', 'ignore')


def _get(op, path):
    with op.open(BASE + path, timeout=TIMEOUT) as r:
        return r.read().decode('utf-8', 'ignore')


def _save_cookies(cj):
    try:
        old = os.umask(0o077)          # 새 파일을 0600으로
        try:
            cj.save(ignore_discard=True, ignore_expires=True)
        finally:
            os.umask(old)
        os.chmod(COOKIE_PATH, 0o600)   # 기존 파일도 강제 0600
    except Exception:
        pass


def _login(op, cj):
    """강제 로그인(기존 세션 킥 후 재시도). 성공 시 True.
    쿠키파일 flock으로 동시 재로그인 직렬화(중복 kick/파일손상 방지)."""
    uid, pwd = _creds()
    if not uid or not pwd:
        return False
    # flock으로 동시 재로그인 직렬화. 단 락파일을 못 열어도(권한 등) 로그인
    # 자체는 진행(직렬화만 포기) — 500 방지.
    lf = None
    try:
        lf = open(COOKIE_PATH + '.lock', 'w')
        fcntl.flock(lf, fcntl.LOCK_EX)
    except Exception:
        if lf is not None:
            try:
                lf.close()
            except Exception:
                pass
            lf = None
    try:
        # 락 대기 중 다른 프로세스가 이미 로그인했을 수 있음 → 쿠키 재로드
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
        payload = {'UID': _b64(uid), 'PWD': _b64(pwd), 'CATE': 'LOGIN',
                   'ScreenWidth': '1920', 'ScreenHeight': '1080'}
        try:
            res = json.loads(_post(op, '/Generators/ProcessLogin.aspx', payload))
        except Exception:
            res = {}
        if res.get('Result') == 'InvalidSession':
            try:
                _post(op, '/Generators/DeleteLoginSession.ashx', {'UserId': uid})
                res = json.loads(
                    _post(op, '/Generators/ProcessLogin.aspx', payload))
            except Exception:
                res = {}
        ok = res.get('Result') == 'Success'
        if ok:
            _save_cookies(cj)
        return ok
    finally:
        if lf is not None:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            finally:
                lf.close()


def _looks_logged_out(h):
    # 명시 마커만 사용(길이 휴리스틱 제거 — 짧은 정상 문서 오판 방지).
    return any(m in h for m in (
        'txtLoginUser', 'Sign-up for 2 days Free Trial',
        'The resource cannot be found'))


def _authed_get(path):
    """세션 stale이면 1회 강제 로그인 후 재시도하는 GET.
    네트워크/로그인 실패는 KrconError로 정규화."""
    op, cj = _opener()
    try:
        h = _get(op, path)
    except Exception as e:
        raise KrconError('network: %s' % e)
    if _looks_logged_out(h):
        if not _login(op, cj):
            raise KrconError('login failed (계정 미설정/거부)')
        try:
            h = _get(op, path)
        except Exception as e:
            raise KrconError('network: %s' % e)
    return h


_TAG = re.compile(r'<[^>]+>')
_WS = re.compile(r'[ \t\r\f\v]+')
_SCRIPT = re.compile(r'<(script|style)\b.*?</\1>', re.I | re.S)


def _strip(h):
    h = _SCRIPT.sub(' ', h)
    h = _TAG.sub(' ', h)
    h = _html.unescape(h)
    h = _WS.sub(' ', h)
    return h


def login_check():
    """명시적 로그인 헬스체크. (ok:bool, msg:str)."""
    uid, pwd = _creds()
    if not uid or not pwd:
        return False, 'KRCON_USER/KRCON_PW 미설정'
    op, cj = _opener()
    return (_login(op, cj), 'ok')


def search(query, limit=50, locale='en'):
    q = urllib.parse.quote(query)
    path = f'/Functions/WordSearch/List.aspx?LocaleKey={locale}&Search={q}'
    try:
        h = _authed_get(path)
    except KrconError as e:
        return {'error': 'KRCON_UNAVAILABLE', 'detail': str(e), 'query': query}
    text = _strip(h)

    # 카테고리: "SOLAS *** (145)" — 마커 직전 짧은 이름만(look-back)
    cats, seen = [], set()
    for m in re.finditer(r'\*\*\*\s*\((\d+)\)', text):
        pre = text[max(0, m.start() - 40):m.start()]
        nm = re.search(r'([A-Za-z][A-Za-z0-9&/.\-]*(?:\s[A-Za-z0-9&/.\-]+){0,3})\s*$', pre)
        name = (nm.group(1).strip() if nm else '')
        name = re.sub(r'^(?:SEARCH\s+|FILTER\s+|ALL\s+|MORE\s+|RINA\s+)+', '', name)
        if name and name not in seen:
            seen.add(name)
            cats.append({'name': name, 'count': int(m.group(1))})
    tm = re.search(r'Total\s*:\s*(\d+)', text)
    total = int(tm.group(1)) if tm else None

    # 결과: View.aspx?Id=NNN 앵커
    results, seen_id = [], set()
    for m in re.finditer(
            r'<a\b[^>]*View\.aspx\?Id=(\d+)[^>]*>(.*?)</a>', h, re.I | re.S):
        doc_id = m.group(1)
        title = _strip(m.group(2)).strip()
        tail = _strip(h[m.end():m.end() + 600]).strip()
        snippet = tail[:280]
        key = (doc_id, title)
        if key in seen_id:
            continue
        seen_id.add(key)
        results.append({'id': doc_id, 'title': title, 'snippet': snippet})
        if len(results) >= limit:
            break

    return {'query': query, 'total': total, 'categories': cats,
            'returned': len(results), 'results': results}


def view(doc_id, query='', locale='en'):
    doc_id = str(doc_id)
    if not doc_id.isdigit():
        return {'error': 'BAD_ID', 'id': doc_id}
    q = urllib.parse.quote(query)
    path = f'/Functions/TreeView/View.aspx?Id={doc_id}&LocaleKey={locale}&Search={q}'
    try:
        h = _authed_get(path)
    except KrconError as e:
        return {'error': 'KRCON_UNAVAILABLE', 'detail': str(e), 'id': doc_id}
    body = _SCRIPT.sub(' ', h)
    body = _TAG.sub('\n', body)
    body = _html.unescape(body)
    body = re.sub(r'[ \t]+', ' ', body)
    body = re.sub(r'\n\s*\n+', '\n\n', body).strip()

    title = ''
    tm = re.search(r'Title\s*\n?\s*(.+)', body)
    if tm:
        title = tm.group(1).strip()[:200]
    eff = ''
    em = re.search(r'Effective Date\s*\n?\s*([0-9/]+)', body)
    if em:
        eff = em.group(1).strip()
    pdf = ''
    pm = re.search(r'href="([^"]+\.pdf[^"]*)"', h, re.I)
    if pm:
        href = pm.group(1).strip()
        if href.startswith('/'):                 # 상대경로 → KR-CON 호스트
            pdf = BASE + href
        elif href.startswith(BASE + '/'):        # 절대경로는 KR-CON 도메인만 허용
            pdf = href
        elif not re.match(r'[a-zA-Z][a-zA-Z0-9+.\-]*:', href):  # scheme 없는 상대
            pdf = BASE + '/' + href.lstrip('/')
        # 그 외(외부 http/https, javascript: 등)는 드롭

    return {'id': str(doc_id), 'title': title, 'effective_date': eff,
            'pdf': pdf, 'text': body}

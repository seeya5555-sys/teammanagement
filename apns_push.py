"""APNs(Apple Push Notification service) 발송기 — 새 의존성 0개.

왜 이 모양인가:
· APNs 는 **HTTP/2 전용**이라 stdlib(`http.client`=HTTP/1.1)로는 못 보낸다.
  서버(Oracle A1)의 `curl 7.76 + nghttp2` 를 subprocess 로 쓴다(실측: api.push.apple.com h2 도달).
· 인증은 provider JWT(ES256). 서버에 이미 있는 `cryptography` 로 직접 서명한다(PyJWT 불필요).
· 🔴 JWT·디바이스 토큰을 커맨드라인 인자로 주면 `ps` 에 노출된다 → **curl config 를 stdin(-K -)**
  으로 넘겨 인자에 비밀이 안 남게 한다. 본문(payload)은 비밀이 아니라 임시파일로 준다.
· 🔴 키(.p8)는 **git 밖**에 둔다. autodeploy 가 `cp -rf` 로 앱 디렉터리를 덮으므로 저장소 안에
  두면 유출·소실 둘 다 위험하다.

키 설정 파일(`apns.env`, KEY=VALUE 한 줄씩):
    APNS_KEY_ID=XXXXXXXXXX
    APNS_TEAM_ID=P2F2WF9PBJ
    APNS_P8=/home/opc/apns/AuthKey_XXXXXXXXXX.p8
    APNS_TOPIC=kr.co.sinokor.trmt
탐색 순서: 환경변수 APNS_CONF → /home/opc/apns/apns.env → ~/.openclaw/secrets/apns.env
"""
import os
import re
import json
import time
import base64
import subprocess
import tempfile
import threading

# Ad Hoc/App Store 서명 빌드 = production, Xcode Debug 설치 = sandbox.
# 🔴 환경을 틀리면 APNs 가 400 BadDeviceToken 으로 조용히 거절한다 → 디바이스별로 저장해서 고른다.
HOSTS = {
    'production': 'api.push.apple.com',
    'sandbox':    'api.sandbox.push.apple.com',
}

CONF_CANDIDATES = (
    os.environ.get('APNS_CONF') or '',
    '/home/opc/apns/apns.env',
    os.path.expanduser('~/.openclaw/secrets/apns.env'),
)

_lock = threading.Lock()
_jwt_cache = {'token': None, 'iat': 0, 'kid': None}


class APNsNotConfigured(RuntimeError):
    """키 미설정 — 호출측은 이걸 '발송 실패'가 아니라 '미구성'으로 구분해 보고해야 한다."""


def conf_path():
    for p in CONF_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def load_conf():
    p = conf_path()
    if not p:
        raise APNsNotConfigured(
            'apns.env 없음 (탐색: %s)' % ', '.join(x for x in CONF_CANDIDATES if x))
    conf = {}
    with open(p, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in ('APNS_KEY_ID', 'APNS_TEAM_ID', 'APNS_P8') if not conf.get(k)]
    if missing:
        raise APNsNotConfigured('%s 에 %s 누락' % (p, ','.join(missing)))
    if not os.path.exists(conf['APNS_P8']):
        raise APNsNotConfigured('p8 키 파일 없음: %s' % conf['APNS_P8'])
    conf.setdefault('APNS_TOPIC', 'kr.co.sinokor.trmt')
    return conf


def configured():
    """설정 유무만 조용히 확인(예외 없이). 헬스/상태 표시용."""
    try:
        load_conf()
        return True
    except Exception:
        return False


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=')


def _sign_es256(p8_path, msg):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    with open(p8_path, 'rb') as fh:
        key = load_pem_private_key(fh.read(), password=None)
    der = key.sign(msg, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, 'big') + s.to_bytes(32, 'big')


def provider_jwt(conf, ttl=2700):
    """provider JWT. APNs 는 최대 1h 유효 → 45분 캐시(매 발송 서명하면 낭비)."""
    with _lock:
        now = int(time.time())
        c = _jwt_cache
        if c['token'] and c['kid'] == conf['APNS_KEY_ID'] and now - c['iat'] < ttl:
            return c['token']
        header = {'alg': 'ES256', 'kid': conf['APNS_KEY_ID'], 'typ': 'JWT'}
        payload = {'iss': conf['APNS_TEAM_ID'], 'iat': now}
        seg = (_b64u(json.dumps(header, separators=(',', ':')).encode()) + b'.' +
               _b64u(json.dumps(payload, separators=(',', ':')).encode()))
        tok = (seg + b'.' + _b64u(_sign_es256(conf['APNS_P8'], seg))).decode()
        c.update(token=tok, iat=now, kid=conf['APNS_KEY_ID'])
        return tok


class APNsBadValue(ValueError):
    """curl config 에 넣을 수 없는 값 — 헤더/옵션 주입 시도로 간주해 발송을 거부한다."""


def _curl_quote(v):
    """curl config 파일의 큰따옴표 값 이스케이프(\\ 와 " 만 특별문자).

    🔴 제어문자는 이스케이프가 아니라 **거부**한다(올마이트 지적). config 는 줄 단위 파서라
       CR/LF 가 들어가면 새 줄 = 임의 curl 옵션 주입(`--output` 으로 파일 쓰기 등)이 된다.
       collapse_id 처럼 외부(`/api/ext/push`)에서 오는 값이 이 경로를 탄다.
    """
    s = str(v)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in s):
        raise APNsBadValue('제어문자가 포함된 값은 거부: %r' % s[:64])
    return s.replace('\\', '\\\\').replace('"', '\\"')


def send(device_token, payload, env='production', conf=None,
         push_type='alert', priority='10', collapse_id=None, expiration=None,
         timeout=15):
    """단건 발송. 반환 (ok, status, reason).

    status: HTTP 코드(int) 또는 0(=curl 자체 실패). reason: APNs 사유문자열 또는 오류설명.
    🔴 200 이 아니면 절대 성공으로 취급하지 않는다(fail-closed) — 조용한 미탐 방지.
    """
    conf = conf or load_conf()
    host = HOSTS.get(env) or HOSTS['production']
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    # 토큰은 hex 만. 여기서 한 번 더 보는 이유 = 이 함수는 CLI·스크립트에서도 직접 불린다.
    if not re.fullmatch(r'[0-9a-fA-F]{40,200}', str(device_token or '')):
        return False, 0, 'bad device token'

    try:
        lines = [
            '--http2',
            '--request POST',
            '--url "https://%s/3/device/%s"' % (host, _curl_quote(device_token)),
            '--header "authorization: bearer %s"' % _curl_quote(provider_jwt(conf)),
            '--header "apns-topic: %s"' % _curl_quote(conf['APNS_TOPIC']),
            '--header "apns-push-type: %s"' % _curl_quote(push_type),
            '--header "apns-priority: %s"' % _curl_quote(priority),
            '--header "content-type: application/json"',
            '--silent', '--show-error',
            '--max-time %d' % int(timeout),
            '--write-out "\\n%{http_code}"',
        ]
        if collapse_id:
            # APNs 는 collapse-id 를 64바이트로 제한 — 바이트 기준으로 자른다(한글이면 문자수≠바이트수).
            cid = str(collapse_id).encode('utf-8')[:64].decode('utf-8', 'ignore')
            lines.append('--header "apns-collapse-id: %s"' % _curl_quote(cid))
        if expiration is not None:
            lines.append('--header "apns-expiration: %s"' % _curl_quote(expiration))
    except APNsBadValue as e:
        return False, 0, str(e)
    except Exception as e:
        # 🔴 `provider_jwt()` 가 여기서 불린다 — p8 파일이 없거나(load_conf 는 존재만 보고
        #    내용은 안 본다) PEM 이 깨졌으면 ValueError/OSError 가 그대로 올라간다.
        #    이 함수의 계약은 (ok, status, reason) 이고 호출부(_push_dispatch)는 예외를
        #    가정하지 않으므로, 여기서 삼키지 않으면 알림 큐가 통째로 흔들린다.
        return False, 0, 'apns 서명/헤더 준비 실패: %s' % e

    try:
        fd, bpath = tempfile.mkstemp(prefix='apns-', suffix='.json')
    except OSError as e:
        return False, 0, '임시파일 생성 실패: %s' % e
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(body)
        lines.append('--data-binary "@%s"' % _curl_quote(bpath))
        try:
            p = subprocess.run(['curl', '-K', '-'],
                               input=('\n'.join(lines) + '\n').encode('utf-8'),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout + 10)
        except subprocess.TimeoutExpired:
            return False, 0, 'curl timeout'
        except Exception as e:
            return False, 0 , 'curl 실행 실패: %s' % e
    finally:
        try:
            os.unlink(bpath)
        except OSError:
            pass

    out = (p.stdout or b'').decode('utf-8', 'replace')
    err = (p.stderr or b'').decode('utf-8', 'replace').strip()
    # write-out 으로 마지막 줄에 상태코드를 붙였다. 그 앞이 APNs 응답 본문.
    head, _, tail = out.rpartition('\n')
    code = 0
    try:
        code = int((tail or '').strip())
    except ValueError:
        pass
    if code == 0:
        return False, 0, err or ('curl rc=%s out=%r' % (p.returncode, out[:200]))
    if code == 200:
        return True, 200, ''
    reason = ''
    try:
        reason = (json.loads(head or '{}') or {}).get('reason') or ''
    except Exception:
        reason = (head or '').strip()[:200]
    return False, code, reason or ('HTTP %d' % code)


# 🔴 비활성화는 **410 Unregistered 만**(올마이트 지적으로 축소).
#    이유: 400 `BadDeviceToken`·`DeviceTokenNotForTopic` 은 "토큰 사망"과 "환경/토픽 설정 불일치"가
#    같은 응답으로 온다. 설정 문제로 기기를 끄면 형이 눈치채기 전까지 알림이 **조용히** 끊기고,
#    앱은 이미 등록됐다고 알아 재등록도 하지 않는다.
#    반대로 죽은 토큰을 안 끄면 이벤트마다 400 한 번씩 낭비될 뿐이고, 그 사유는 push_log.detail 과
#    앱의 알림설정 화면에 남아 사람이 판별할 수 있다. 미탐보다 낭비를 택한다.
DEAD_REASONS = {'Unregistered'}


def is_dead(status, reason):
    return status == 410


def alert_payload(title, body, link=None, kind=None, badge=None, thread_id=None):
    """표준 alert payload. link=`trmt://…` 딥링크(앱이 탭 시 라우팅)."""
    aps = {
        'alert': {'title': title, 'body': body},
        'sound': 'default',
    }
    if badge is not None:
        aps['badge'] = int(badge)
    if thread_id:
        aps['thread-id'] = str(thread_id)
    out = {'aps': aps}
    if link:
        out['link'] = link
    if kind:
        out['kind'] = kind
    return out


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'check':
        p = conf_path()
        print('conf:', p or '(없음)')
        try:
            c = load_conf()
            print('key_id:', c['APNS_KEY_ID'], 'team:', c['APNS_TEAM_ID'],
                  'topic:', c['APNS_TOPIC'])
            print('jwt ok, len =', len(provider_jwt(c)))
        except Exception as e:
            print('NOT CONFIGURED:', e)
            sys.exit(1)
    elif len(sys.argv) > 3 and sys.argv[1] == 'send':
        env = sys.argv[4] if len(sys.argv) > 4 else 'production'
        ok, st, rs = send(sys.argv[2], alert_payload('TRMT', sys.argv[3], kind='test'), env=env)
        print('ok=%s status=%s reason=%s' % (ok, st, rs))
        sys.exit(0 if ok else 1)
    else:
        print('usage: apns_push.py check | apns_push.py send <token> <msg> [env]')

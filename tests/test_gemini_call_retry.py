import io, json, urllib.error
from unittest import mock
import helpers_shared as H


def _http(code):
    return urllib.error.HTTPError('u', code, 'x', {}, io.BytesIO(b'overloaded'))


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _ok():
    return _Resp(json.dumps({'candidates': [{'content': {'parts': [{'text': '{"a": 1}'}]}}]}).encode())


def test_retries_transient_then_succeeds():
    with mock.patch.object(H, 'GEMINI_API_KEY', 'k'), mock.patch('time.sleep'), \
         mock.patch('urllib.request.urlopen', side_effect=[_http(503), _http(429), _ok()]) as m:
        assert H._gemini_call_json([{'text': 'x'}]) == {'a': 1}
        assert m.call_count == 3


def test_non_transient_4xx_fails_immediately():
    with mock.patch.object(H, 'GEMINI_API_KEY', 'k'), mock.patch('time.sleep'), \
         mock.patch('urllib.request.urlopen', side_effect=[_http(400)]) as m:
        r = H._gemini_call_json([{'text': 'x'}])
        assert r['error'] == 'API_CALL_FAILED' and 'HTTP 400' in r['detail']
        assert m.call_count == 1

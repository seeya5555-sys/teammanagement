"""AI 공통 호출: GPT 메인 → 실패 시 Gemini 자동 백업 (2026-10-29)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers_shared as H


class _MP:
    """pytest 없이 쓰는 최소 monkeypatch(setattr+undo)."""
    def __init__(self):
        self.undo = []
    def setattr(self, target, name, value=None):
        if isinstance(target, str):
            mod, _, attr = target.rpartition('.')
            target = __import__(mod, fromlist=[attr]); name, value = attr, name
        self.undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)
    def close(self):
        for t, n, v in reversed(self.undo):
            setattr(t, n, v)


def _ai():
    mp = _MP()
    mp.setattr(H, 'OPENAI_API_KEY', 'k')
    mp.setattr(H, 'GEMINI_API_KEY', 'g')
    return mp


def test_openai_success_no_gemini(ai):
    ai.setattr(H, '_openai_call_json', lambda parts, budget=130: {'items': [1]})
    def boom(parts, model=None, budget=240):
        raise AssertionError('gemini called')
    ai.setattr(H, '_gemini_call_json_raw', boom)
    assert H._gemini_call_json([{'text': 'x'}]) == {'items': [1]}


def test_openai_error_falls_back_to_gemini(ai):
    calls = []
    ai.setattr(H, '_openai_call_json', lambda parts, budget=130: {'error': 'API_CALL_FAILED', 'detail': 'HTTP 429: insufficient_quota'})
    ai.setattr(H, '_gemini_call_json_raw', lambda parts, model=None, budget=240: calls.append(model) or {'items': [2]})
    assert H._gemini_call_json([{'text': 'x'}], model='gm') == {'items': [2]}
    assert calls == ['gm']


def test_openai_parse_failure_falls_back(ai):
    ai.setattr(H, '_openai_call_json', lambda parts, budget=130: {'error': 'PARSE_FAILED', 'raw': 'zzz'})
    ai.setattr(H, '_gemini_call_json_raw', lambda parts, model=None, budget=240: ['ok'])
    assert H._gemini_call_json([{'text': 'x'}]) == ['ok']


def test_no_openai_key_uses_gemini_only(ai):
    ai.setattr(H, 'OPENAI_API_KEY', '')
    ai.setattr(H, '_gemini_call_json_raw', lambda parts, model=None, budget=240: {'via': 'gemini'})
    assert H._gemini_call_json([{'text': 'x'}]) == {'via': 'gemini'}


def test_openai_part_conversion(ai):
    seen = {}
    class R:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def read(s):
            return b'{"output":[{"content":[{"type":"output_text","text":"{\\"a\\":1}"}]}]}'
    def fake(req, timeout=None):
        import json
        seen['body'] = json.loads(req.data)
        return R()
    ai.setattr('urllib.request.urlopen', fake)
    out = H._openai_call_json([{'inline_data': {'mime_type': 'application/pdf', 'data': 'QQ=='}},
                               {'inline_data': {'mime_type': 'image/png', 'data': 'QQ=='}},
                               {'text': 'hi'}])
    assert out == {'a': 1}
    types = [c['type'] for c in seen['body']['input'][0]['content']]
    assert types == ['input_file', 'input_image', 'input_text', 'input_text']


def test_parse_ai_json_text_fences():
    assert H._parse_ai_json_text('```json\n{"a": 1}\n```', 't') == {'a': 1}
    assert H._parse_ai_json_text('[1,2]', 't') == [1, 2]
    assert H._parse_ai_json_text('nope', 't')['error'] == 'PARSE_FAILED'


def test_openai_exception_falls_back(ai):
    def boom(parts, budget=130):
        raise ValueError('bad part')
    ai.setattr(H, '_openai_call_json', boom)
    ai.setattr(H, '_gemini_call_json_raw', lambda parts, model=None, budget=240: {'via': 'gemini', 'b': budget})
    out = H._gemini_call_json([{'text': 'x'}])
    assert out['via'] == 'gemini' and out['b'] <= 240


def test_budget_shared_with_gemini(ai):
    import time
    t = [1000.0]
    ai.setattr(time, 'monotonic', lambda: t[0])
    def slow(parts, budget=130):
        t[0] += 130
        return {'error': 'API_CALL_FAILED', 'detail': 'timeout'}
    got = {}
    ai.setattr(H, '_openai_call_json', slow)
    ai.setattr(H, '_gemini_call_json_raw', lambda parts, model=None, budget=240: got.update(b=budget) or {'ok': 1})
    H._gemini_call_json([{'text': 'x'}])
    assert 130 + got['b'] <= 270


if __name__ == '__main__':
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            if inspect.signature(fn).parameters:
                mp = _ai()
                try:
                    fn(mp)
                finally:
                    mp.close()
            else:
                fn()
    print('✅ ai provider fallback pass')

"""영수증 자동인식 — provider 선택(Claude 우선·Gemini 폴백)과 응답 타입 정규화 계약 (2026-09-23).

형 스크린샷: iOS 가 `fields.amount` 를 Double 로 디코드하는데 모델이 "1,078,000" 문자열을 돌려주자
DecodingError 로 자동인식 전체가 죽었다. 여기서 못박는 것:
  · /extract 응답의 fields.amount 는 **항상 숫자 또는 null**(문자열 금지), occur_date 는 YYYY-MM-DD 또는 null.
  · ANTHROPIC_API_KEY 있으면 Claude(Haiku) 먼저, 실패하면 Gemini 폴백, 둘 다 없으면 no_api_key.
  · RECEIPT_PROVIDER=gemini 강제 시 Claude 를 호출하지 않는다.
  · 외부 HTTP 는 절대 나가지 않는다(두 provider 함수를 몽키패치).
"""
import json
import os
import tempfile
import unittest

import app as appmod
import routes_calendar_dock as rcd


class ReceiptExtractProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        self.old_upload = appmod.app.config.get('UPLOAD_FOLDER')
        self.old_testing = appmod.app.config.get('TESTING')
        db = os.path.join(self.tmp.name, 't.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True   # CSRF 우회(다른 라우트 테스트와 동일)
        appmod.app.config['UPLOAD_FOLDER'] = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, 'receipt'), exist_ok=True)
        with open(os.path.join(self.tmp.name, 'receipt', 'r.jpg'), 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0fake')
        with appmod.app.app_context():
            appmod.init_db(False)
            self.tid = appmod.execute(
                "INSERT INTO biz_trips(title, created_at) VALUES('t', datetime('now','localtime'))")
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1, username='admin', display_name='A', role='admin', supervisor_id=None)
        self.saved = {k: getattr(rcd, k) for k in ('_claude_vision_extract', '_gemini_vision_extract',
                                                   'ANTHROPIC_API_KEY')}
        self.old_provider = os.environ.pop('RECEIPT_PROVIDER', None)
        self.calls = []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(rcd, k, v)
        if self.old_provider is not None:
            os.environ['RECEIPT_PROVIDER'] = self.old_provider
        else:
            os.environ.pop('RECEIPT_PROVIDER', None)
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        appmod.app.config['UPLOAD_FOLDER'] = self.old_upload
        appmod.app.config['TESTING'] = self.old_testing
        self.tmp.cleanup()

    def _stub(self, name, ret):
        def fn(path, *a, **k):
            self.calls.append(name)
            return dict(ret) if isinstance(ret, dict) else ret
        setattr(rcd, name, fn)

    def _extract(self):
        return self.client.post(f'/api/biz-trips/{self.tid}/extract', json={'filename': 'r.jpg'}).get_json()

    # ---- 타입 정규화 ----
    def test_string_amount_and_dotted_date_become_number_and_iso(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', {'readable': 'true', 'confidence': 'HIGH', 'issues': None,
                                              'vendor': ' Apple 명동 ', 'date': '2026.05.14',
                                              'currency': '₩', 'amount': '₩1,078,000'})
        j = self._extract()
        self.assertTrue(j['ok'], j)
        self.assertEqual(1078000.0, j['fields']['amount'])
        self.assertIsInstance(j['fields']['amount'], float)
        self.assertEqual('2026-05-14', j['fields']['occur_date'])
        self.assertEqual('KRW', j['fields']['currency'])
        self.assertEqual('Apple 명동', j['fields']['vendor'])
        self.assertEqual('high', j['confidence'])
        self.assertIs(True, j['readable'])
        self.assertEqual([], j['issues'])
        self.assertEqual([], j['missing'])
        self.assertFalse(j['need_retake'])
        self.assertEqual('claude', j['provider'])

    def test_unparseable_amount_becomes_null_and_flags_retake(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', {'readable': True, 'confidence': 'medium', 'issues': [],
                                              'vendor': 'X', 'date': 'unknown', 'currency': 'won', 'amount': 'n/a'})
        j = self._extract()
        self.assertTrue(j['ok'])
        self.assertIsNone(j['fields']['amount'])
        self.assertIsNone(j['fields']['occur_date'])
        self.assertEqual('KRW', j['fields']['currency'])
        self.assertEqual(['occur_date', 'amount'], j['missing'])
        self.assertTrue(j['need_retake'])
        self.assertIn('unclear_amount', j['issues'])

    def test_numeric_zero_amount_is_kept_as_number_but_counts_missing(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'X', 'date': '20260514', 'currency': 'KRW', 'amount': 0})
        j = self._extract()
        self.assertEqual(0.0, j['fields']['amount'])
        self.assertEqual('2026-05-14', j['fields']['occur_date'])
        self.assertNotIn('amount', j['missing'])   # 0 은 값이 있는 것(사람이 판단), 문자열/None 만 missing

    # ---- provider 선택 ----
    def test_claude_preferred_when_key_present_and_gemini_not_called(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'V', 'date': '2026-01-02', 'currency': 'USD', 'amount': 12.5})
        self._stub('_gemini_vision_extract', {'vendor': 'G', 'amount': 1})
        j = self._extract()
        self.assertEqual(['_claude_vision_extract'], self.calls)
        self.assertEqual('V', j['fields']['vendor'])

    def test_claude_failure_falls_back_to_gemini(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', {'error': 'API_CALL_FAILED', 'detail': '529'})
        self._stub('_gemini_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'G', 'date': '2026-01-02', 'currency': 'KRW', 'amount': '3,000'})
        j = self._extract()
        self.assertEqual(['_claude_vision_extract', '_gemini_vision_extract'], self.calls)
        self.assertTrue(j['ok'])
        self.assertEqual('gemini', j['provider'])
        self.assertEqual(3000.0, j['fields']['amount'])

    def test_no_claude_key_uses_gemini_directly(self):
        rcd.ANTHROPIC_API_KEY = ''
        self._stub('_claude_vision_extract', {'vendor': 'C', 'amount': 1})
        self._stub('_gemini_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'G', 'date': None, 'currency': 'KRW', 'amount': 5})
        j = self._extract()
        self.assertEqual(['_gemini_vision_extract'], self.calls)
        self.assertEqual('gemini', j['provider'])

    def test_forced_gemini_provider_skips_claude_even_with_key(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        os.environ['RECEIPT_PROVIDER'] = 'gemini'
        self._stub('_claude_vision_extract', {'vendor': 'C', 'amount': 1})
        self._stub('_gemini_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'G', 'date': None, 'currency': 'KRW', 'amount': 5})
        self._extract()
        self.assertEqual(['_gemini_vision_extract'], self.calls)

    def test_both_missing_reports_no_api_key(self):
        rcd.ANTHROPIC_API_KEY = ''
        self._stub('_gemini_vision_extract', {'error': 'NO_API_KEY'})
        j = self._extract()
        self.assertFalse(j['ok'])
        self.assertEqual('no_api_key', j['reason'])

    def test_claude_failure_and_gemini_absent_surfaces_claude_error(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', {'error': 'PARSE_FAILED', 'raw': 'garbage'})
        self._stub('_gemini_vision_extract', {'error': 'NO_API_KEY'})
        j = self._extract()
        self.assertFalse(j['ok'])
        self.assertEqual('PARSE_FAILED', j['reason'])

    # ---- 올마이트 지적: 비객체 JSON·실제 model 표기·deadline ----
    def test_non_dict_provider_result_falls_back_instead_of_crashing(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        self._stub('_claude_vision_extract', [])           # `[]` 같은 비객체
        self._stub('_gemini_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'G', 'date': None, 'currency': 'KRW', 'amount': 5})
        j = self._extract()
        self.assertTrue(j['ok'])
        self.assertEqual('gemini', j['provider'])

    def test_non_dict_from_both_is_parse_failed_200_not_500(self):
        rcd.ANTHROPIC_API_KEY = ''
        self._stub('_gemini_vision_extract', None)
        r = self.client.post(f'/api/biz-trips/{self.tid}/extract', json={'filename': 'r.jpg'})
        self.assertEqual(200, r.status_code)
        self.assertEqual('PARSE_FAILED', r.get_json()['reason'])

    def test_reported_model_is_the_one_actually_used_after_retry(self):
        rcd.ANTHROPIC_API_KEY = ''
        self._stub('_gemini_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                              'vendor': 'G', 'date': None, 'currency': 'KRW', 'amount': 5,
                                              '_model': 'gemini-fallback-model'})
        j = self._extract()
        self.assertEqual('gemini-fallback-model', j['model'])
        self.assertNotIn('_model', j['raw'])

    def test_claude_receives_timeout_within_deadline(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        seen = {}
        def fn(path, *a, **k):
            seen.update(k); return {'readable': True, 'confidence': 'high', 'issues': [],
                                    'vendor': 'V', 'date': None, 'currency': 'KRW', 'amount': 1}
        rcd._claude_vision_extract = fn
        self._extract()
        self.assertLessEqual(seen.get('timeout', 999), 25)
        self.assertGreaterEqual(seen.get('timeout', 0), 5)

    def test_amount_guesses_are_not_promoted(self):
        rcd.ANTHROPIC_API_KEY = 'k'
        for bad in ('2 items Total 12.50', '(12.50)', '12,50', 'approx 3000', True, float('nan'), float('inf')):
            self._stub('_claude_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                                  'vendor': 'X', 'date': '2026-01-01', 'currency': 'KRW', 'amount': bad})
            j = self._extract()
            self.assertIsNone(j['fields']['amount'], bad)
            self.assertIn('unclear_amount', j['issues'], bad)
            self.assertTrue(j['need_retake'], bad)
        for good, want in (('1,078,000', 1078000.0), ('₩48,000', 48000.0), ('USD 12.50', 12.5), ('48,000원', 48000.0),
                           (-3.5, -3.5), (1200, 1200.0), ('1200.75', 1200.75)):
            self._stub('_claude_vision_extract', {'readable': True, 'confidence': 'high', 'issues': [],
                                                  'vendor': 'X', 'date': '2026-01-01', 'currency': 'KRW', 'amount': good})
            self.assertEqual(want, self._extract()['fields']['amount'], good)

    # ---- 실제 HTTP 계층(urlopen 모킹): 503 재시도 경로가 NameError 로 죽었던 실버그(2026-09-23 라이브 probe) ----
    def _mock_urlopen(self, script):
        """script: URL 부분문자열 → ('ok', body_dict) | ('http', code, body_text). 호출 순서를 self.http 에 기록."""
        import urllib.request, urllib.error, io
        self.http = []
        def fake(req, timeout=None):
            url = req.full_url
            self.http.append((url, timeout, json.loads(req.data.decode('utf-8'))))
            for key, resp in script:
                if key in url:
                    if resp[0] == 'http':
                        raise urllib.error.HTTPError(url, resp[1], 'err', {}, io.BytesIO(resp[2].encode()))
                    class R:
                        def __enter__(s): return s
                        def __exit__(s, *a): return False
                        def read(s): return json.dumps(resp[1]).encode()
                    return R()
            raise AssertionError('unexpected url ' + url)
        self._old_urlopen = urllib.request.urlopen
        urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(urllib.request, 'urlopen', self._old_urlopen))

    def _gemini_ok(self, obj):
        return ('ok', {'candidates': [{'content': {'parts': [{'text': json.dumps(obj)}]}}]})

    def test_gemini_503_on_receipt_model_retries_default_model_and_reports_it(self):
        rcd.ANTHROPIC_API_KEY = ''
        old_key, rcd.GEMINI_API_KEY = rcd.GEMINI_API_KEY, 'g'
        self.addCleanup(lambda: setattr(rcd, 'GEMINI_API_KEY', old_key))
        os.environ['MODEL_RECEIPT'] = 'gemini-primary-test'
        self.addCleanup(lambda: os.environ.pop('MODEL_RECEIPT', None))
        self._mock_urlopen([
            ('gemini-primary-test:generateContent', ('http', 503, '{"error":{"code":503}}')),
            (rcd.GEMINI_MODEL + ':generateContent', self._gemini_ok(
                {'readable': True, 'confidence': 'high', 'issues': [], 'vendor': 'V', 'date': '2026-05-14',
                 'currency': 'KRW', 'amount': '1,078,000'})),
        ])
        j = self._extract()
        self.assertTrue(j['ok'], j)
        self.assertEqual(2, len(self.http))
        self.assertIn('gemini-primary-test', self.http[0][0])
        self.assertIn(rcd.GEMINI_MODEL, self.http[1][0])
        self.assertEqual(rcd.GEMINI_MODEL, j['model'])          # 실제 사용 모델
        self.assertEqual(1078000.0, j['fields']['amount'])
        self.assertTrue(all(t is not None and 5 <= t <= 40 for _, t, _ in self.http))

    def test_gemini_400_is_not_retried(self):
        rcd.ANTHROPIC_API_KEY = ''
        old_key, rcd.GEMINI_API_KEY = rcd.GEMINI_API_KEY, 'g'
        self.addCleanup(lambda: setattr(rcd, 'GEMINI_API_KEY', old_key))
        os.environ['MODEL_RECEIPT'] = 'gemini-primary-test'
        self.addCleanup(lambda: os.environ.pop('MODEL_RECEIPT', None))
        self._mock_urlopen([('gemini-primary-test:generateContent', ('http', 400, 'bad'))])
        j = self._extract()
        self.assertFalse(j['ok'])
        self.assertEqual('API_CALL_FAILED', j['reason'])
        self.assertEqual(1, len(self.http))

    def test_claude_request_shape_and_non_object_json_falls_back(self):
        """Anthropic Messages 요청 형식(헤더·image block·model) 확인 + `[]` 응답이면 gemini 폴백."""
        rcd.ANTHROPIC_API_KEY = 'sk-test'
        old_key, rcd.GEMINI_API_KEY = rcd.GEMINI_API_KEY, 'g'
        self.addCleanup(lambda: setattr(rcd, 'GEMINI_API_KEY', old_key))
        self._mock_urlopen([
            ('api.anthropic.com/v1/messages', ('ok', {'stop_reason': 'end_turn',
                                                      'content': [{'type': 'text', 'text': '[]'}]})),
            (':generateContent', self._gemini_ok({'readable': True, 'confidence': 'medium', 'issues': [],
                                                  'vendor': 'G', 'date': None, 'currency': 'KRW', 'amount': 5})),
        ])
        j = self._extract()
        self.assertTrue(j['ok'])
        self.assertEqual('gemini', j['provider'])
        url, timeout, body = self.http[0]
        self.assertEqual(rcd.RECEIPT_CLAUDE_MODEL, body['model'])
        self.assertEqual(512, body['max_tokens'])
        content = body['messages'][0]['content']
        self.assertEqual('image', content[0]['type'])
        self.assertEqual('base64', content[0]['source']['type'])
        self.assertEqual('image/jpeg', content[0]['source']['media_type'])   # 매직바이트 \xff\xd8\xff
        self.assertEqual('text', content[1]['type'])

    def test_claude_success_end_to_end_via_http_layer(self):
        rcd.ANTHROPIC_API_KEY = 'sk-test'
        self._mock_urlopen([
            ('api.anthropic.com/v1/messages', ('ok', {'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': '```json\n{"readable":true,"confidence":"high","issues":[],'
                                         '"vendor":"Apple 명동","date":"2026.05.14","currency":"₩","amount":"1,078,000"}\n```'}]})),
        ])
        j = self._extract()
        self.assertTrue(j['ok'], j)
        self.assertEqual('claude', j['provider'])
        self.assertEqual(rcd.RECEIPT_CLAUDE_MODEL, j['model'])
        self.assertEqual(1078000.0, j['fields']['amount'])
        self.assertEqual('2026-05-14', j['fields']['occur_date'])
        self.assertEqual(1, len(self.http))

    # ---- 순수 정규화 함수 ----
    def test_date_normalizer_variants(self):
        f = rcd._normalize_receipt_date
        self.assertEqual('2026-05-14', f('2026/5/14'))
        self.assertEqual('2026-05-14', f('2026년 05월 14일 13:20'))
        self.assertEqual('2026-05-14', f('2026-05-14 13:20:11'))
        self.assertEqual('2026-05-14', f('2026.05.14(목) 13:20'))
        self.assertEqual('2026-05-14', f('20260514'))
        self.assertEqual('2026-05-14', f('26.05.14'))
        self.assertEqual('2024-02-29', f('2024-02-29'))
        self.assertIsNone(f('2023-02-29'))
        self.assertIsNone(f('2026-13-40'))
        self.assertIsNone(f('080-500-1007'))          # 전화번호
        self.assertIsNone(f('1208184429'))            # 사업자번호/바코드 조각
        self.assertIsNone(f('20260514R7381144712'))   # 바코드 전체
        self.assertIsNone(f('2026-05'))
        self.assertIsNone(f(''))
        self.assertIsNone(f(None))

    def test_currency_normalizer_variants(self):
        f = rcd._normalize_receipt_currency
        self.assertEqual('KRW', f('원'))
        self.assertEqual('CNY', f('rmb'))
        self.assertEqual('JPY', f('￥'))
        self.assertEqual('USD', f('usd'))
        self.assertIsNone(f('dollars'))
        self.assertIsNone(f('ABC'))     # ISO 목록 밖 3글자는 인정 안 함(올마이트)


if __name__ == '__main__':
    unittest.main()

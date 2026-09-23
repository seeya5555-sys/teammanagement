"""영수증 Haiku 전용 계약: Gemini 폴백 없음, 정규화 유지, 장애 메시지/외부호출 횟수 검증.
모든 HTTP는 mock. 실제 키/영수증 데이터 없음.
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
        self.saved = {k: getattr(rcd, k) for k in ('_claude_vision_extract', 'ANTHROPIC_API_KEY')}
        rcd.ANTHROPIC_API_KEY = 'sk-test'
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

    # ---- 비용 관점 부호: 완료환불 -, 일반결제 +, 미완료환불은 자동 반영 안 함 ----
    def test_completed_refund_is_negative_for_positive_or_negative_model_amount(self):
        for amount in (12.5, -12.5, '12.50', '-12.50'):
            with self.subTest(amount=amount):
                self._stub('_claude_vision_extract', {'vendor': 'Test', 'date': '2026-01-02',
                    'currency': 'USD', 'amount': amount, 'transaction_type': 'expense', 'transaction_status': '환불 성공'})
                j = self._extract()
                self.assertTrue(j['ok'])
                self.assertEqual('refund', j['transaction_type'])
                self.assertEqual(-12.5, j['fields']['amount'])
                self.assertFalse(j['need_retake'])

    def test_payment_app_debit_minus_is_positive_expense(self):
        self._stub('_claude_vision_extract', {'amount': -100, 'transaction_type': 'refund', 'transaction_status': 'Payment successful'})
        j = self._extract()
        self.assertEqual(100.0, j['fields']['amount'])
        self.assertEqual('expense', j['transaction_type'])

    def test_uncompleted_refund_does_not_populate_cost(self):
        self._stub('_claude_vision_extract', {'amount': 12.5, 'transaction_type': 'refund', 'transaction_status': '환불 처리 중'})
        j = self._extract()
        self.assertIsNone(j['fields']['amount'])
        self.assertTrue(j['need_retake'])
        self.assertIn('refund_not_completed', j['issues'])

    def test_unknown_type_does_not_invent_refund_and_null_remains_null(self):
        for kind in ('unknown', None, 'refund eligible', 'refund maybe', {}):
            with self.subTest(kind=kind):
                self._stub('_claude_vision_extract', {'amount': 12.5, 'transaction_status': kind})
                j = self._extract()
                self.assertEqual('unknown', j['transaction_type'])
                self.assertEqual(12.5, j['fields']['amount'])
        self._stub('_claude_vision_extract', {'amount': None, 'transaction_type': 'expense', 'transaction_status': '환불 성공'})
        self.assertIsNone(self._extract()['fields']['amount'])

    def test_only_explicit_final_status_sets_direction(self):
        for status in ('환불 성공', '退款成功', 'Refund completed', '환불 처리 완료'):
            self.assertEqual('refund', rcd._receipt_transaction_type(status), status)
        for status in ('결제 완료', '支付成功', 'Payment successful'):
            self.assertEqual('expense', rcd._receipt_transaction_type(status), status)
        for status in ('환불 실패', '退款处理中', 'Refund pending'):
            self.assertEqual('refund_unconfirmed', rcd._receipt_transaction_type(status), status)
        for status in ('환불 성공 아님', '환불 가능', 'Refund successful? No', 'unknown', None):
            self.assertEqual('unknown', rcd._receipt_transaction_type(status), status)

    def test_refund_item_and_success_evidence_are_both_required(self):
        self.assertEqual('refund', rcd._receipt_transaction_type('成功', '退款-Test service'))
        for status, description in [('成功', '普通商品'), ('不成功', '退款-Test'),
                                    ('退款处理中', '退款-Test'), ('申请成功', '退款-Test'),
                                    ('提交成功', '退款-Test'), ('取消成功', '退款-Test'),
                                    ('退款失败成功', '退款-Test'), (None, '退款-Test'),
                                    ('Payment successful', '退款-Test'), ('成功', '如何退款-Test')]:
            self.assertNotEqual('refund', rcd._receipt_transaction_type(status, description))

    def test_refund_extraction_save_edit_and_totals_preserve_negative_cost(self):
        self._stub('_claude_vision_extract', {'vendor': 'Test', 'date': '2026-01-02',
            'currency': 'USD', 'amount': 12.5, 'transaction_type': 'expense', 'transaction_status': '환불 성공'})
        fields = self._extract()['fields']
        original = self.client.post(f'/api/biz-trips/{self.tid}/receipts',
                                    json={'amount': 100, 'currency': 'USD'})
        self.assertEqual(201, original.status_code)
        refund = self.client.post(f'/api/biz-trips/{self.tid}/receipts', json=fields)
        self.assertEqual(201, refund.status_code)
        receipt = refund.get_json()['receipt']
        self.assertEqual(-12.5, receipt['amount'])
        detail = self.client.get(f'/api/biz-trips/{self.tid}').get_json()
        self.assertEqual(87.5, detail['totals']['USD'])
        listing = self.client.get('/api/biz-trips').get_json()
        self.assertEqual(87.5, next(t for t in listing if t['id']==self.tid)['totals']['USD'])
        edit = self.client.put(f"/api/biz-receipts/{receipt['id']}", json={'amount': '-10.00'})
        self.assertEqual(200, edit.status_code)
        detail = self.client.get(f'/api/biz-trips/{self.tid}').get_json()
        self.assertEqual(90.0, detail['totals']['USD'])
        self.assertEqual(-10.0, next(r for r in detail['receipts'] if r['id']==receipt['id'])['amount'])

    # ---- Haiku 전용: 구 env/키 없음/실패 모두 Gemini 사용 금지 ----
    def test_claude_is_only_provider_even_with_stale_gemini_env(self):
        os.environ['RECEIPT_PROVIDER'] = 'gemini'
        self._stub('_claude_vision_extract', {'vendor': 'V', 'date': '2026-01-02',
                                            'currency': 'USD', 'amount': 12.5})
        j = self._extract()
        self.assertEqual('claude', rcd._receipt_provider())
        self.assertFalse(hasattr(rcd, '_gemini_vision_extract'))
        self.assertEqual(['_claude_vision_extract'], self.calls)
        self.assertEqual('claude', j['provider'])
        self.assertEqual(12.5, j['fields']['amount'])

    def test_claude_failure_is_returned_without_fallback(self):
        self._stub('_claude_vision_extract', {'error': 'AI_BUSY', 'detail': 'private'})
        j = self._extract()
        self.assertEqual(['_claude_vision_extract'], self.calls)
        self.assertFalse(j['ok'])
        self.assertEqual('AI_BUSY', j['reason'])
        self.assertNotIn('detail', j)

    def test_missing_claude_key_never_calls_any_provider(self):
        from unittest.mock import patch
        rcd.ANTHROPIC_API_KEY = ''
        with patch('urllib.request.urlopen', side_effect=AssertionError('no HTTP allowed')) as http:
            j = self._extract()
        http.assert_not_called()
        self.assertFalse(j['ok'])
        self.assertEqual('no_api_key', j['reason'])
        self.assertIn('Haiku', j['message'])

    def test_non_object_provider_result_is_parse_error_not_500(self):
        for result in (None, [], 7):
            with self.subTest(result=result):
                self._stub('_claude_vision_extract', result)
                response = self.client.post(f'/api/biz-trips/{self.tid}/extract', json={'filename': 'r.jpg'})
                self.assertEqual(200, response.status_code)
                self.assertEqual('PARSE_FAILED', response.get_json()['reason'])

    def test_reported_model_is_actual_claude_model(self):
        self._stub('_claude_vision_extract', {'vendor': 'V', 'amount': 5, '_model': 'claude-haiku-test'})
        j = self._extract()
        self.assertEqual('claude-haiku-test', j['model'])
        self.assertNotIn('_model', j['raw'])

    def test_claude_receives_bounded_timeout(self):
        seen = {}
        def fn(path, **kwargs):
            seen.update(kwargs)
            return {'vendor': 'V', 'amount': 1}
        rcd._claude_vision_extract = fn
        self._extract()
        self.assertGreater(seen['timeout'], 0)
        self.assertLessEqual(seen['timeout'], 25)

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

    # ---- 실제 HTTP 계층 mock: Haiku 전용 요청·오류 계약 ----
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

    def test_claude_http_failures_are_classified_and_never_call_gemini(self):
        cases = ((429, 'AI_BUSY', True), (503, 'AI_BUSY', True), (529, 'AI_BUSY', True),
                 (504, 'AI_TIMEOUT', True), (401, 'AI_AUTH_FAILED', False),
                 (403, 'AI_AUTH_FAILED', False), (400, 'API_CALL_FAILED', False))
        # One mocked transport, mutable response. Every case must issue exactly one Anthropic call.
        import io, urllib.error
        from unittest.mock import patch
        for code, reason, retryable in cases:
            with self.subTest(code=code):
                seen = []
                def fail(req, **kwargs):
                    seen.append(req.full_url)
                    raise urllib.error.HTTPError(req.full_url, code, 'error', {}, io.BytesIO(b'private detail'))
                with patch('urllib.request.urlopen', side_effect=fail):
                    j = self._extract()
                self.assertFalse(j['ok'])
                self.assertEqual(reason, j['reason'])
                self.assertEqual(retryable, j['retryable'])
                self.assertNotIn('detail', j)
                self.assertNotIn('private', json.dumps(j))
                self.assertEqual(['https://api.anthropic.com/v1/messages'], seen)

    def test_claude_network_failures_are_sanitized(self):
        from unittest.mock import patch
        import urllib.error
        for exc, reason in ((TimeoutError('private socket'), 'AI_TIMEOUT'),
                            (urllib.error.URLError(TimeoutError('socket')), 'AI_TIMEOUT'),
                            (urllib.error.URLError('private connection'), 'API_CALL_FAILED')):
            with self.subTest(reason=reason):
                with patch('urllib.request.urlopen', side_effect=exc) as http:
                    j = self._extract()
                self.assertEqual(reason, j['reason'])
                self.assertEqual(1, http.call_count)
                self.assertNotIn('detail', j)

    def test_claude_request_shape_and_non_object_json_does_not_fallback(self):
        self._mock_urlopen([
            ('api.anthropic.com/v1/messages', ('ok', {'stop_reason': 'end_turn',
               'content': [{'type': 'text', 'text': '[]'}]})),
        ])
        j = self._extract()
        self.assertFalse(j['ok'])
        self.assertEqual('PARSE_FAILED', j['reason'])
        self.assertNotIn('raw', j)
        self.assertEqual(1, len(self.http))
        url, timeout, body = self.http[0]
        self.assertEqual(rcd.RECEIPT_CLAUDE_MODEL, body['model'])
        self.assertEqual(512, body['max_tokens'])
        content = body['messages'][0]['content']
        self.assertEqual('image', content[0]['type'])
        self.assertEqual('base64', content[0]['source']['type'])
        self.assertEqual('image/jpeg', content[0]['source']['media_type'])
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

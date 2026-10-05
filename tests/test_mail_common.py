"""메일 3종 공통 에러/입력 규약 고정 — 공통화 전과 응답 본문·status 가 같아야 한다."""
import unittest

import class_mail_service
import daily_mail_service
import vetting_mail_service
from app_core import app
from mail_common import MailServiceError, error_response, json_body


class MailCommonTests(unittest.TestCase):
    def test_service_errors_share_base_but_stay_distinct(self):
        kinds = (daily_mail_service.DailyMailError, vetting_mail_service.VettingMailError,
                 class_mail_service.ClassMailError)
        for kind in kinds:
            self.assertTrue(issubclass(kind, MailServiceError))
            e = kind(409, 'x')
            self.assertEqual((409, 'x', {}, 'x'), (e.status, e.message, e.extra, str(e)))
        # 서로 잡히지 않음(기존 except 범위 유지)
        self.assertFalse(issubclass(daily_mail_service.DailyMailError, vetting_mail_service.VettingMailError))

    def test_error_response_body(self):
        with app.test_request_context():
            resp, status = error_response(vetting_mail_service.VettingMailError(404, 'run 없음'))
            self.assertEqual((404, {'error': 'run 없음'}), (status, resp.get_json()))
            resp, status = error_response(daily_mail_service.DailyMailError(409, 'dup', code='duplicate'))
            self.assertEqual((409, {'error': 'dup', 'code': 'duplicate'}), (status, resp.get_json()))

    def test_json_body_non_dict_is_empty(self):
        for data, ctype in (('[1]', 'application/json'), ('{bad', 'application/json'), ('', None)):
            with app.test_request_context(method='POST', data=data, content_type=ctype):
                self.assertEqual({}, json_body())
        with app.test_request_context(method='POST', json={'a': 1}):
            self.assertEqual({'a': 1}, json_body())


if __name__ == '__main__':
    unittest.main()

"""Shared report export response and exact error-contract tests."""

import unittest
from unittest.mock import Mock, patch

from flask import Flask

import report_export_service as service


class ReportExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.logger = Mock()

    def test_docx_download_headers_and_bytes_are_preserved(self):
        with self.app.test_request_context("/"):
            response = service.docx_response(
                builder=lambda data: b"PK-docx-" + data["body"],
                data={"body": b"golden"},
                filename="Report.docx",
                logger=self.logger,
                log_label="report-docx",
            )

        self.assertEqual(200, response.status_code)
        response.direct_passthrough = False
        self.assertEqual(b"PK-docx-golden", response.get_data())
        self.assertEqual(service.DOCX_MIME, response.mimetype)
        self.assertIn('filename=Report.docx', response.headers["Content-Disposition"])

    def test_builder_failure_shape_and_logging_are_preserved(self):
        def fail(_data):
            raise ValueError("broken")

        with self.app.test_request_context("/"):
            response, status = service.docx_response(
                builder=fail,
                data={},
                filename="Report.docx",
                logger=self.logger,
                log_label="report-docx",
            )

        self.assertEqual(500, status)
        self.assertEqual({"error": "문서 생성 실패: broken"}, response.get_json())
        self.logger.exception.assert_called_once_with("report-docx")

    def test_pdf_missing_tool_keeps_route_owned_message(self):
        message = "report-specific install instruction"
        with self.app.test_request_context("/"), patch.object(
            service.shutil, "which", return_value=None
        ):
            response, status = service.pdf_response(
                builder=lambda _data: b"docx",
                data={},
                filename="Report.pdf",
                logger=self.logger,
                log_label="report-pdf",
                missing_tool_message=message,
            )

        self.assertEqual(500, status)
        self.assertEqual({"error": message}, response.get_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)

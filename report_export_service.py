"""Shared Flask responses for report DOCX/PDF exports.

Routes keep authentication, report lookup, builder selection, filenames, and
their historical error wording. This module owns only the duplicated document
build/conversion/response mechanics.
"""

from io import BytesIO
import os
import shutil
import subprocess
import tempfile
import traceback

from flask import jsonify, send_file


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _build_docx(builder, data, logger, log_label):
    try:
        return builder(data), None
    except Exception as exc:
        logger.exception(log_label)
        traceback.print_exc()
        return None, (jsonify({"error": f"문서 생성 실패: {exc}"}), 500)


def docx_response(*, builder, data, filename, logger, log_label):
    docx_bytes, error = _build_docx(builder, data, logger, log_label)
    if error:
        return error
    return send_file(
        BytesIO(docx_bytes),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=filename,
    )


def pdf_response(*, builder, data, filename, logger, log_label,
                 missing_tool_message):
    docx_bytes, error = _build_docx(builder, data, logger, log_label)
    if error:
        return error

    try:
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = os.path.join(tmp, "report.docx")
            with open(docx_path, "wb") as handle:
                handle.write(docx_bytes)

            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            if not soffice:
                return jsonify({"error": missing_tool_message}), 500

            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", tmp, docx_path],
                capture_output=True,
                timeout=120,
            )
            if proc.returncode != 0:
                detail = proc.stderr.decode("utf-8", errors="ignore")[:500]
                return jsonify({"error": f"PDF 변환 실패: {detail}"}), 500

            pdf_path = os.path.join(tmp, "report.pdf")
            if not os.path.exists(pdf_path):
                return jsonify({"error": "PDF 파일이 생성되지 않았습니다."}), 500
            with open(pdf_path, "rb") as handle:
                pdf_bytes = handle.read()
    except subprocess.TimeoutExpired:
        return jsonify({"error": "PDF 변환 시간 초과 (2분)."}), 500
    except Exception as exc:
        logger.exception(log_label)
        return jsonify({"error": f"PDF 변환 오류: {exc}"}), 500

    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )

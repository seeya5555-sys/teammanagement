"""Daily·Vetting·Class 메일 자동화 공통 HTTP/에러 규약.

세 서비스의 상태 규칙(발송 claim·release·멱등성·VLCC 제한 등)은 의도적으로 서로 다르므로
여기 두지 않는다. 동일했던 것만 모은다: 에러 타입·에러 응답 형식·JSON 본문 파싱.
"""
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import jsonify, request


def today_kst():
    return datetime.now(ZoneInfo('Asia/Seoul')).date()


def reply_date_prefix(received_at, remark):
    """자동 조치사항 맨 앞에 회신일 [M/D] (형 지시 2026-10-06). 이미 [M/D]로 시작하면 그대로. Class·CS 공용."""
    if re.match(r'\[\d{1,2}/\d{1,2}\]', remark):
        return remark
    raw = str(received_at or '')[:10]
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        day = date.today()
    return f'[{day.month}/{day.day}]{remark}'


class MailServiceError(Exception):
    """HTTP status + 사용자 메시지. extra 키는 응답 본문에 그대로 합쳐진다."""

    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def json_body():
    """요청 JSON 이 dict 가 아니면(없음·배열·깨짐) 빈 dict."""
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def error_response(exc):
    body = {'error': exc.message}
    body.update(exc.extra)
    return jsonify(body), exc.status

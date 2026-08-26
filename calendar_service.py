"""Calendar data operations shared by the thin Flask route adapters.

This module deliberately knows nothing about Flask requests, sessions, JSON
responses, or authentication.  The existing ``routes_calendar_dock``
Blueprint remains the public HTTP boundary so URL rules, endpoint names, auth
decorators, and idempotency middleware keep their current contracts.
"""

import math

from app_core import execute, query


_MUTABLE_FIELDS = (
    "supervisor_id", "vessel_id", "title", "start_date", "end_date",
    "all_day", "start_time", "end_time", "category", "color",
    "location", "notes", "completed",
    "leave_type",
)

LEAVE_DAYS = {"annual": 1.0, "half": 0.5, "quarter": 0.25}


class CalendarInputError(ValueError):
    """A client-supplied calendar value that must become HTTP 400."""


def validate_event_payload(data, *, creating=False):
    required_messages = {
        "title": "title 이 필요합니다.",
        "start_date": "start_date 가 필요합니다.",
    }
    for field, message in required_messages.items():
        if not creating and field not in data:
            continue
        value = data.get(field)
        # Preserve the historical whitespace contract while rejecting values
        # that cannot be valid text. Previously falsy non-strings were rejected
        # on create but this shared validator accidentally let them through.
        if not isinstance(value, str) or value == "":
            raise CalendarInputError(message)

    # Omitted color still means the historical default blue. An explicitly
    # empty color used to become SQL NULL on PUT, which violates the row's
    # normal color contract and later renders inconsistently.
    if not creating and "color" in data:
        color = data.get("color")
        if not isinstance(color, str) or color == "":
            raise CalendarInputError("color 가 필요합니다.")

    if "leave_type" in data:
        leave_type = data.get("leave_type") or None
        if leave_type is not None and leave_type not in LEAVE_DAYS:
            raise CalendarInputError("leave_type 은 annual, half, quarter 중 하나여야 합니다.")
        if leave_type is not None:
            if not data.get("supervisor_id"):
                raise CalendarInputError("연차 일정에는 담당 감독이 필요합니다.")
            start = data.get("start_date")
            end = data.get("end_date")
            if end and start and end != start:
                raise CalendarInputError("연차 일정은 하루 단위로 등록하세요.")


def _supervisor_scope(value):
    if not value or value == "all":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CalendarInputError(
            "supervisor_id 는 정수 또는 all 이어야 합니다."
        ) from exc


def list_events(start=None, end=None, supervisor_id=None):
    sql = "SELECT * FROM calendar_events WHERE 1=1"
    params = []
    if start:
        sql += " AND (COALESCE(end_date, start_date) >= ?)"
        params.append(start)
    if end:
        sql += " AND (start_date <= ?)"
        params.append(end)
    supervisor_scope = _supervisor_scope(supervisor_id)
    if supervisor_scope is not None:
        sql += " AND (supervisor_id = ? OR supervisor_id IS NULL)"
        params.append(supervisor_scope)
    sql += ' ORDER BY start_date, COALESCE(start_time, "00:00")'
    return [dict(row) for row in query(sql, tuple(params))]


def find_event(source_type, source_id):
    if not source_type or not source_id:
        return None
    row = query(
        "SELECT * FROM calendar_events WHERE source_type=? AND source_id=?",
        (source_type, source_id), one=True,
    )
    return dict(row) if row else None


def create_event(data, username, valid_colors):
    color = (data.get("color") or "blue").lower()
    if color not in valid_colors:
        color = "blue"

    return execute("""
        INSERT INTO calendar_events
            (supervisor_id, vessel_id, title, start_date, end_date,
             all_day, start_time, end_time, category, color, location, notes, completed,
             source_type, source_id, created_by, leave_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("supervisor_id") or None,
        data.get("vessel_id") or None,
        data["title"],
        data["start_date"],
        data.get("end_date") or None,
        1 if data.get("all_day", True) else 0,
        data.get("start_time") or None,
        data.get("end_time") or None,
        data.get("category") or "",
        color,
        data.get("location") or "",
        data.get("notes") or "",
        1 if data.get("completed") else 0,
        data.get("source_type") or "manual",
        data.get("source_id") or None,
        username,
        data.get("leave_type") or None,
    ))


def get_event(event_id):
    row = query("SELECT * FROM calendar_events WHERE id=?", (event_id,), one=True)
    return dict(row) if row else None


def event_exists(event_id):
    return bool(query("SELECT id FROM calendar_events WHERE id=?", (event_id,), one=True))


def update_event(event_id, data, valid_colors):
    sets, params = [], []
    for field in _MUTABLE_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field == "color" and value:
            value = value.lower()
            if value not in valid_colors:
                value = "blue"
        if field in ("all_day", "completed"):
            value = 1 if value else 0
        sets.append(f"{field} = ?")
        params.append(None if value == "" else value)

    if not sets:
        return False
    sets.append("updated_at = datetime('now','localtime')")
    execute(
        f'UPDATE calendar_events SET {", ".join(sets)} WHERE id=?',
        tuple(params + [event_id]),
    )
    return True


def delete_event(event_id):
    execute("DELETE FROM calendar_events WHERE id=?", (event_id,))


def leave_summary(year, supervisor_id):
    try:
        year = int(year)
        supervisor_id = int(supervisor_id)
    except (TypeError, ValueError) as exc:
        raise CalendarInputError("year 와 supervisor_id 는 정수여야 합니다.") from exc
    if not 2000 <= year <= 2100:
        raise CalendarInputError("year 는 2000~2100 범위여야 합니다.")
    allowance_row = query(
        "SELECT days, manual_used FROM calendar_leave_allowances WHERE supervisor_id=? AND year=?",
        (supervisor_id, year), one=True,
    )
    allowance = float(allowance_row["days"]) if allowance_row else 0.0
    manual_used = float(allowance_row["manual_used"]) if allowance_row else 0.0
    rows = query("""
        SELECT leave_type, COUNT(*) AS count
          FROM calendar_events
         WHERE supervisor_id=? AND start_date BETWEEN ? AND ? AND leave_type IS NOT NULL
         GROUP BY leave_type
    """, (supervisor_id, f"{year:04d}-01-01", f"{year:04d}-12-31"))
    counts = {key: 0 for key in LEAVE_DAYS}
    for row in rows:
        if row["leave_type"] in counts:
            counts[row["leave_type"]] = int(row["count"])
    calendar_used = sum(counts[key] * days for key, days in LEAVE_DAYS.items())
    used = calendar_used + manual_used
    return {
        "year": year, "supervisor_id": supervisor_id, "allowance": allowance,
        "calendar_used": calendar_used, "manual_used": manual_used,
        "used": used, "remaining": allowance - used, "counts": counts,
    }


def _quarter_day_value(value, label):
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise CalendarInputError(f"{label}은 숫자여야 합니다.") from exc
    if not 0 <= value <= 365 or not math.isfinite(value):
        raise CalendarInputError(f"{label}은 0~365 범위여야 합니다.")
    if round(value * 4) != value * 4:
        raise CalendarInputError(f"{label}은 0.25일 단위로 입력하세요.")
    return value


def set_leave_allowance(year, supervisor_id, days, username, manual_used=None):
    days = _quarter_day_value(days, "연차 일수")
    summary = leave_summary(year, supervisor_id)
    # 구버전 앱은 manual_used 키를 보내지 않는다. 그 요청이 이미 입력한
    # 수동 사용분을 0으로 덮지 않도록 생략은 "기존 값 유지"로 해석한다.
    if manual_used is None:
        manual_used = summary["manual_used"]
    else:
        manual_used = _quarter_day_value(manual_used, "수동 사용일수")
    if not query("SELECT 1 FROM supervisors WHERE id=?", (summary["supervisor_id"],), one=True):
        raise CalendarInputError("존재하지 않는 담당 감독입니다.")
    execute("""
        INSERT INTO calendar_leave_allowances
            (supervisor_id, year, days, manual_used, updated_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(supervisor_id, year) DO UPDATE SET
            days=excluded.days, manual_used=excluded.manual_used,
            updated_by=excluded.updated_by,
            updated_at=datetime('now','localtime')
    """, (
        summary["supervisor_id"], summary["year"], days, manual_used, username,
    ))
    return leave_summary(summary["year"], summary["supervisor_id"])

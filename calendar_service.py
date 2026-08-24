"""Calendar data operations shared by the thin Flask route adapters.

This module deliberately knows nothing about Flask requests, sessions, JSON
responses, or authentication.  The existing ``routes_calendar_dock``
Blueprint remains the public HTTP boundary so URL rules, endpoint names, auth
decorators, and idempotency middleware keep their current contracts.
"""

from app_core import execute, query


_MUTABLE_FIELDS = (
    "supervisor_id", "vessel_id", "title", "start_date", "end_date",
    "all_day", "start_time", "end_time", "category", "color",
    "location", "notes", "completed",
)


def list_events(start=None, end=None, supervisor_id=None):
    sql = "SELECT * FROM calendar_events WHERE 1=1"
    params = []
    if start:
        sql += " AND (COALESCE(end_date, start_date) >= ?)"
        params.append(start)
    if end:
        sql += " AND (start_date <= ?)"
        params.append(end)
    if supervisor_id and supervisor_id != "all":
        sql += " AND (supervisor_id = ? OR supervisor_id IS NULL)"
        params.append(int(supervisor_id))
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
             source_type, source_id, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

"""Read-only Dry Dock Report projections below the Flask route boundary.

The public Blueprint keeps authentication, authorization, request parsing,
status codes, and JSON responses. This module owns only deterministic SQLite
reads and row/content projection.
"""

import json

from app_core import query


def list_reports(is_template=None, vessel_id=None, status=None, search=None):
    conditions, params = ["1=1"], []

    if is_template is not None:
        conditions.append("d.is_template = ?")
        params.append(1 if is_template in ("1", "true", "yes") else 0)
    else:
        conditions.append("d.is_template = 0")

    if vessel_id:
        conditions.append("d.vessel_id = ?")
        params.append(vessel_id)
    if status:
        conditions.append("d.status = ?")
        params.append(status)
    if search:
        like = f"%{search}%"
        conditions.append("(d.title LIKE ? OR d.shipyard LIKE ? OR d.dock_no LIKE ?)")
        params.extend((like, like, like))

    sql = f"""
        SELECT d.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM dock_reports d
          JOIN vessels       v ON v.id = d.vessel_id
          LEFT JOIN supervisors s ON s.id = d.supervisor_id
         WHERE {' AND '.join(conditions)}
         ORDER BY d.updated_at DESC, d.id DESC
    """
    return [dict(row) for row in query(sql, params)]


def _attach_sections(report, report_id, on_decode_error=None):
    sections = [dict(row) for row in query("""
        SELECT * FROM dock_report_sections
         WHERE report_id = ?
         ORDER BY display_order, id
    """, (report_id,))]

    blocks_by_section = {}
    if sections:
        blocks = query("""
            SELECT b.* FROM dock_report_blocks b
              JOIN dock_report_sections s ON s.id = b.section_id
             WHERE s.report_id = ?
             ORDER BY b.section_id, b.display_order, b.id
        """, (report_id,))
        for row in blocks:
            block = dict(row)
            try:
                block["content"] = json.loads(block.pop("content_json"))
            except Exception as exc:
                if on_decode_error:
                    on_decode_error(exc)
                block["content"] = {}
            blocks_by_section.setdefault(block["section_id"], []).append(block)

    for section in sections:
        section["blocks"] = blocks_by_section.get(section["id"], [])
    report["sections"] = sections
    return report


def get_report(report_id, on_decode_error=None):
    row = query("""
        SELECT d.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM dock_reports d
          JOIN vessels       v ON v.id = d.vessel_id
          LEFT JOIN supervisors s ON s.id = d.supervisor_id
         WHERE d.id = ?
    """, (report_id,), one=True)
    if not row:
        return None
    return _attach_sections(dict(row), report_id, on_decode_error)


def get_export_report(report_id, on_decode_error=None):
    row = query("""
        SELECT d.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               v.vessel_type AS vessel_type,
               s.name       AS supervisor_name
          FROM dock_reports d
          JOIN vessels       v ON v.id = d.vessel_id
          LEFT JOIN supervisors s ON s.id = d.supervisor_id
         WHERE d.id = ?
    """, (report_id,), one=True)
    if not row:
        return None
    return _attach_sections(dict(row), report_id, on_decode_error)

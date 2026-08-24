"""Read-only Boarding Report projection below the Flask route boundary."""

import json

from app_core import query


def get_export_report(report_id, on_decode_error=None):
    report_row = query("""
        SELECT b.*,
               v.name       AS vessel_name,
               v.short_name AS vessel_short,
               s.name       AS supervisor_name
          FROM boarding_reports b
          JOIN vessels       v ON v.id = b.vessel_id
          LEFT JOIN supervisors s ON s.id = b.supervisor_id
         WHERE b.id = ?
    """, (report_id,), one=True)
    if not report_row:
        return None

    sections = [dict(section) for section in query("""
        SELECT * FROM boarding_report_sections
         WHERE report_id = ?
         ORDER BY display_order, id
    """, (report_id,))]

    blocks_by_section = {}
    if sections:
        # Bind the report id once. A section-id IN list grows with the document
        # and eventually hits SQLite's variable limit on large reports.
        blocks = query("""
            SELECT b.* FROM boarding_report_blocks b
              JOIN boarding_report_sections s ON s.id = b.section_id
             WHERE s.report_id = ?
             ORDER BY b.section_id, b.display_order, b.id
        """, (report_id,))
        for block_row in blocks:
            block = dict(block_row)
            try:
                block["content"] = json.loads(block.pop("content_json"))
            except Exception as exc:
                if on_decode_error:
                    on_decode_error(exc)
                block["content"] = {}
            blocks_by_section.setdefault(block["section_id"], []).append(block)

    for section in sections:
        section["blocks"] = blocks_by_section.get(section["id"], [])

    report = dict(report_row)
    report["sections"] = sections
    return report

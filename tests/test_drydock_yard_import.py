import io
import sqlite3
import unittest

from openpyxl import Workbook

import drydock_integration as integration


def workbook_bytes():
    wb = Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover.append(["Final Discount :", 0.10])
    quote = wb.create_sheet("Quotation")
    quote.append(["Item No.", None, "Work Description", "Qty", "Net Total"])
    quote.append([2, None, "GENERAL SERVICE", None, 0])
    quote.append([2.1, None, "Circulating Water", None, 0])
    quote.append([None, None, "Connect", 1, 100])
    quote.append(["2.1.1", None, "Supply", 2, 25])
    quote.append([2.2, None, "Ballast Water", 1, 10])
    quote.append([3, None, "DOCKING", None, 0])
    quote.append([24, None, "Fire Wire Reel", 1, 8])
    quote.append([None, None, "Normal Total Price/USD", None, 143])
    quote.append([None, None, "Final discount", None, 0.10])
    quote.append([None, None, "Total Price after dicount/USD Net", None, 128.70])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE vessels (id TEXT PRIMARY KEY, dc_rate REAL DEFAULT 0);
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vessel_id TEXT, number TEXT,
            section TEXT, category TEXT, description TEXT, vendor TEXT,
            budget REAL, consumption REAL, start_date TEXT, end_date TEXT,
            completion REAL, remarks TEXT, updated_at TEXT
        );
        INSERT INTO vessels(id,dc_rate) VALUES('v_test',0);
    """)
    return db


class DockYardImportTests(unittest.TestCase):
    def test_parser_matches_quote_total_and_kuwait_rollup_shape(self):
        parsed = integration._parse_yard_job_workbook(workbook_bytes())
        jobs = {job["number"]: job for job in parsed["jobs"]}
        self.assertEqual(6, parsed["job_count"])
        self.assertEqual(143.0, parsed["gross_total"])
        self.assertEqual(10.0, parsed["discount_rate"])
        self.assertEqual(128.7, parsed["after_discount"])
        self.assertEqual(125.0, jobs["2.1"]["budget"])
        self.assertEqual(0.0, jobs["2.1.1"]["budget"])
        self.assertEqual(10.0, jobs["2.2"]["budget"])
        self.assertEqual(8.0, jobs["24"]["budget"])
        self.assertEqual("GENERAL", jobs["2.1"]["section"])
        self.assertEqual("DECK", jobs["24"]["section"])
        self.assertEqual([], parsed["warnings"])

    def test_apply_preserves_live_progress_and_manual_classification(self):
        db = make_db()
        db.execute("""INSERT INTO jobs(vessel_id,number,section,category,description,vendor,budget,
            consumption,start_date,end_date,completion,remarks)
            VALUES('v_test','2.1','CANCEL','Shipyard','Owner wording','Cancelled',1,55,
                   '2026-01-01','2026-01-02',80,'[{"date":"2026-01-01"}]')""")
        db.execute("""INSERT INTO jobs(vessel_id,number,section,category,description,vendor,budget,
            consumption,completion,remarks) VALUES('v_test','2.2','GENERAL','Crew','Manual Crew','',7,0,0,'[]')""")
        parsed = integration._parse_yard_job_workbook(workbook_bytes())
        result = integration._apply_yard_job_import(db, "v_test", parsed)
        db.commit()
        kept = db.execute("SELECT * FROM jobs WHERE vessel_id='v_test' AND number='2.1'").fetchone()
        crew = db.execute("SELECT * FROM jobs WHERE vessel_id='v_test' AND number='2.2'").fetchone()
        self.assertEqual("CANCEL", kept["section"])
        self.assertEqual("Owner wording", kept["description"])
        self.assertEqual(125.0, kept["budget"])
        self.assertEqual(55.0, kept["consumption"])
        self.assertEqual(80.0, kept["completion"])
        self.assertEqual(7.0, crew["budget"])
        self.assertEqual(1, result["preserved_manual"])
        self.assertEqual(10.0, db.execute("SELECT dc_rate FROM vessels WHERE id='v_test'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()

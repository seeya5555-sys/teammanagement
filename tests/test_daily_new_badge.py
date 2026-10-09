import sqlite3
import unittest
from tests import test_vessel_manager_supervisor as fixture
import app as appmod


class DailyNewBadgeTests(unittest.TestCase):
    setUp = fixture.VesselManagerSupervisorTests.setUp
    tearDown = fixture.VesselManagerSupervisorTests.tearDown
    def test_registration_not_occurrence_or_edit_and_scope(self):
        with sqlite3.connect(appmod.DATABASE) as db:
            sup = db.execute("INSERT INTO supervisors(name) VALUES ('Badge')").lastrowid
            other = db.execute("INSERT INTO supervisors(name) VALUES ('Other')").lastrowid
            ids = [db.execute("INSERT INTO vessels(name) VALUES (?)", (f'Badge {n}',)).lastrowid
                   for n in range(4)]
            for vid in ids:
                db.execute('INSERT INTO supervisor_vessels(supervisor_id,vessel_id) VALUES (?,?)', (sup, vid))
            for vid, owner, created, occurrence, status in [
                (ids[0], sup, 'now', '2000-01-01', 'Closed'),
                (ids[1], sup, '-1 day', 'today', 'Open'),
                (ids[2], other, 'now', 'today', 'Open'),
            ]:
                db.execute("""INSERT INTO issues(supervisor_id,vessel_id,issue_date,item_topic,status,created_at,updated_at)
                    VALUES (?,?,CASE WHEN ?='today' THEN date('now','+9 hours') ELSE ? END,'Badge',?,
                    CASE WHEN ?='now' THEN datetime('now','+9 hours') ELSE datetime('now','+9 hours','-1 day') END,
                    datetime('now','+9 hours'))""", (owner, vid, occurrence, occurrence, status, created))
            today = db.execute("SELECT date('now','+9 hours')").fetchone()[0]
        rows = {v['id']: v for v in self.client.get(f'/api/vessels?supervisor_id={sup}').get_json()}
        self.assertEqual(rows[ids[0]]['daily_new_date'], today)
        for vid in ids[1:]:
            self.assertIsNone(rows[vid]['daily_new_date'])
        all_rows = {v['id']: v for v in self.client.get('/api/vessels').get_json()}
        self.assertEqual(all_rows[ids[2]]['daily_new_date'], today)


if __name__ == "__main__":
    unittest.main()

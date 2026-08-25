import os
import tempfile
import unittest

import app as appmod


class VesselParticularsSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, 'trmt.db')
        self.old_db = appmod.DATABASE
        self.old_config_db = appmod.app.config['DATABASE']
        appmod.DATABASE = self.db_path
        appmod.app.config.update(DATABASE=self.db_path, TESTING=True, SECRET_KEY='test')
        with appmod.app.app_context():
            appmod.init_db(drop=False)
            appmod.execute("INSERT INTO vessels(name,active) VALUES('SYNC VESSEL',1)")
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key','sync-key')")
        self.client = appmod.app.test_client()
        self.headers = {'X-API-Key': 'sync-key'}

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_config_db
        self.temp.cleanup()

    def put(self, payload):
        return self.client.put('/api/ext/vessels/1/identifiers', json=payload,
                               headers=self.headers)

    def test_particulars_round_trip_is_idempotent(self):
        payload = {'vessel_type': 'VLCC', 'class_society': 'BV',
                   'gross_tonnage': '154,379.0', 'dead_weight': '2.99533e5'}
        first = self.put(payload)
        self.assertEqual(200, first.status_code, first.get_data(as_text=True))
        self.assertEqual({'vessel_type', 'class_society', 'gross_tonnage', 'dead_weight'},
                         set(first.get_json()['changed']))

        second = self.put(payload)
        self.assertEqual(200, second.status_code)
        self.assertEqual({}, second.get_json()['changed'])
        self.assertEqual(set(payload), set(second.get_json()['noop']))

        roster = self.client.get('/api/ext/roster', headers=self.headers).get_json()['vessels'][0]
        self.assertEqual('154379', roster['gross_tonnage'])
        self.assertEqual('299533', roster['dead_weight'])
        self.assertEqual('VLCC', roster['vessel_type'])
        self.assertEqual('BV', roster['class_society'])

    def test_invalid_particular_does_not_partially_update(self):
        response = self.put({'vessel_type': 'VLCC', 'dead_weight': 0})
        self.assertEqual(400, response.status_code)
        with appmod.app.app_context():
            row = appmod.query('SELECT vessel_type, dead_weight FROM vessels WHERE id=1', one=True)
        self.assertIsNone(row['vessel_type'])
        self.assertIsNone(row['dead_weight'])


if __name__ == '__main__':
    unittest.main()

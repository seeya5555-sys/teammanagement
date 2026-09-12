"""Single-row lookup contract and bounded materialization, using real SQLite."""
import sqlite3
import unittest
from unittest.mock import patch

import app_core


class QueryContractTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)')
        self.db.executemany('INSERT INTO sample VALUES (?, ?)',
                            [(i, f'value-{i}') for i in range(1, 1001)])
        self.db.commit()
        self.connection = patch.object(app_core, 'get_db', return_value=self.db)
        self.connection.start()

    def tearDown(self):
        self.connection.stop()
        self.db.close()

    def test_first_row_empty_and_all_rows_preserve_shapes_and_order(self):
        row = app_core.query('SELECT * FROM sample WHERE id >= ? ORDER BY id DESC',
                             (998,), one=True)
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual({'id': 1000, 'value': 'value-1000'}, dict(row))
        self.assertIsNone(app_core.query('SELECT * FROM sample WHERE id < ?', (0,), one=True))
        rows = app_core.query('SELECT * FROM sample WHERE id >= ? ORDER BY id', (998,))
        self.assertIsInstance(rows, list)
        self.assertEqual([998, 999, 1000], [r['id'] for r in rows])
        self.assertEqual([], app_core.query('SELECT * FROM sample WHERE id < ?', (0,)))

    def test_single_lookup_materializes_one_row_and_releases_statement(self):
        materialized = []

        def record_row(cursor, row):
            materialized.append(row[0])
            return sqlite3.Row(cursor, row)

        self.db.row_factory = record_row
        self.assertEqual(1, app_core.query('SELECT * FROM sample ORDER BY id', one=True)['id'])
        self.assertEqual([1], materialized)
        # An unfinished SELECT statement would keep the table locked.
        self.db.execute('DROP TABLE sample')

    def test_row_decode_failure_releases_statement(self):
        def reject_row(cursor, row):
            raise ValueError('invalid row')

        self.db.row_factory = reject_row
        for one in (False, True):
            with self.assertRaisesRegex(ValueError, 'invalid row'):
                app_core.query('SELECT * FROM sample', one=one)
        self.db.execute('DROP TABLE sample')


if __name__ == '__main__':
    unittest.main()

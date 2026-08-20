import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routes_tail


def rec(platform, hours, marker=''):
    return {
        'platform': platform,
        'latitude': 10 + hours / 100,
        'longitude': 20 + hours / 100,
        'event_at': (datetime.utcnow() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S'),
        'heading': 90,
        'speed': 12.5,
        'unused_blob': marker or ('z' * 500),
    }


class FleetCacheCompactionTests(unittest.TestCase):
    def test_compaction_preserves_overlay_and_track_contract(self):
        good_old = rec('STORMGEO', 5)
        good_new = rec('VESSEL', 2)
        slow = rec('SLOW', 1)
        history = [rec('STORMGEO', n, str(n) * 100) for n in range(1, 301)]
        row = {
            'vessel_name': 'TEST SHIP', 'imo': '9000001',
            'latest': slow, 'latest_event_at': slow['event_at'],
            'by_platform': {
                'SLOW': [slow],
                'STORMGEO': [good_old, rec('STORMGEO', 8)],
                'VESSEL': [good_new, rec('VESSEL', 9)],
            },
            'history': history,
            'unrelated': {'large': 'q' * 50000},
        }
        original_latest, original_allow_row_ts = routes_tail._trmtdb_pick_latest(row)
        original_track = routes_tail._trmtdb_track_points(row)

        compact = routes_tail._trmtdb_compact_rows([row])[0]
        compact_latest, compact_allow_row_ts = routes_tail._trmtdb_pick_latest(compact)
        compact_track = routes_tail._trmtdb_track_points(compact)

        self.assertEqual(original_latest['event_at'], compact_latest['event_at'])
        self.assertEqual(original_allow_row_ts, compact_allow_row_ts)
        self.assertEqual(original_track, compact_track)
        self.assertNotIn('history', compact)
        self.assertNotIn('unused_blob', json.dumps(compact))
        self.assertLess(len(json.dumps(compact)), len(json.dumps(row)) / 5)

    def test_corrupt_compressed_track_fails_closed(self):
        with self.assertLogs('app_core', level='WARNING'):
            self.assertEqual([], routes_tail._trmtdb_track_points({'_track_z': 'not-base64'}))


if __name__ == '__main__':
    unittest.main()

"""Fleet Map AIS previous-track contract tests."""
from datetime import datetime, timedelta, timezone
import unittest

import app


class FleetMapTrackPointTests(unittest.TestCase):
    def test_normalizes_sorts_and_filters_ais_history(self):
        row = {
            "history": [
                {"latitude": "35.1", "longitude": "129.1", "event_at": "2026-07-14T03:00:00Z"},
                {"lat": 34.9, "lng": 128.8, "timestamp": "2026-07-14T01:00:00Z"},
                {"latitude": 999, "longitude": 129.0, "event_at": "2026-07-14T02:00:00Z"},
                {"latitude": 35.0, "longitude": 129.0, "event_at": "2026-07-14T02:00:00Z"},
            ]
        }

        points = app._trmtdb_track_points(row)

        self.assertEqual(
            points,
            [
                {"lat": 34.9, "lng": 128.8, "event_at": "2026-07-14T01:00:00Z"},
                {"lat": 35.0, "lng": 129.0, "event_at": "2026-07-14T02:00:00Z"},
                {"lat": 35.1, "lng": 129.1, "event_at": "2026-07-14T03:00:00Z"},
            ],
        )

    def test_supports_track_and_positions_shapes_without_leaking_raw_fields(self):
        row = {
            "track": [{"lat": 35, "lon": 129, "reported_at": "2026-07-14T00:00:00Z", "mmsi": "secret"}],
            "positions": [{"latitude": 36, "longitude": 130, "event_at": "2026-07-14T04:00:00Z"}],
        }

        points = app._trmtdb_track_points(row)

        self.assertEqual(points[0], {"lat": 35.0, "lng": 129.0, "event_at": "2026-07-14T00:00:00Z"})
        self.assertEqual(points[1], {"lat": 36.0, "lng": 130.0, "event_at": "2026-07-14T04:00:00Z"})
        self.assertNotIn("mmsi", points[0])

    def test_uses_short_coordinate_keys_when_long_coordinate_is_null(self):
        points = app._trmtdb_track_points({
            "history": [{"latitude": None, "longitude": None, "lat": 35, "lng": 129,
                         "event_at": "2026-07-14T00:00:00Z"}]
        })

        self.assertEqual(points, [{"lat": 35.0, "lng": 129.0, "event_at": "2026-07-14T00:00:00Z"}])

    def test_keeps_most_recent_points_when_history_exceeds_cap(self):
        started = datetime(2026, 7, 14, tzinfo=timezone.utc)
        row = {"history": [
            {"lat": 35, "lng": 129,
             "event_at": (started + timedelta(seconds=i)).isoformat().replace('+00:00', 'Z')}
            for i in range(2001)
        ]}

        points = app._trmtdb_track_points(row)

        self.assertEqual(len(points), 2000)
        self.assertEqual(points[0]["event_at"], "2026-07-14T00:00:01Z")
        self.assertEqual(points[-1]["event_at"], "2026-07-14T00:33:20Z")

    def test_does_not_use_same_name_when_imo_is_different(self):
        vessel = {"name": "SAME NAME", "imo": "1111111"}
        rows = [{"vessel_name": "SAME NAME", "imo": "2222222", "history": []}]

        self.assertIsNone(app._trmtdb_track_row_for_vessel(rows, vessel))

    def test_track_endpoint_returns_only_normalized_points_for_visible_vessel(self):
        old_visible = app.api_fleet_map_data
        old_positions = app._trmtdb_positions
        try:
            with app.app.test_request_context('/api/fleet-map/track?vessel=TEST%20VESSEL'):
                app.api_fleet_map_data = lambda: app.jsonify({
                    'fleet': [{'name': 'TEST VESSEL', 'imo': '1234567'}]
                })
                app._trmtdb_positions = lambda: ([{
                    'vessel_name': 'TEST VESSEL',
                    'imo': '1234567',
                    'history': [
                        {'latitude': 35, 'longitude': 129, 'event_at': '2026-07-14T00:00:00Z', 'provider': 'private'},
                        {'latitude': 36, 'longitude': 130, 'event_at': '2026-07-14T01:00:00Z'},
                    ],
                }], '2026-07-14T01:00:00', None, False)
                response = app.api_fleet_map_track.__wrapped__()
                payload = response.get_json()
        finally:
            app.api_fleet_map_data = old_visible
            app._trmtdb_positions = old_positions

        self.assertTrue(payload['ok'])
        self.assertTrue(payload['available'])
        self.assertEqual(payload['points'][0], {'lat': 35.0, 'lng': 129.0, 'event_at': '2026-07-14T00:00:00Z'})
        self.assertNotIn('provider', payload['points'][0])


if __name__ == "__main__":
    unittest.main()

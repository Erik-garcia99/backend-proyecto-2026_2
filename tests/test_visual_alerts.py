import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import UUID

from app.visual_alerts import scan_visual_states, visual_episode

B = UUID('11111111-1111-4111-8111-111111111111')
R = UUID('33333333-3333-4333-8333-333333333333')
P = UUID('22222222-2222-4222-8222-222222222222')
N = UUID('55555555-5555-4555-8555-555555555555')
G = UUID('66666666-6666-4666-8666-666666666666')
NOW = datetime.now(timezone.utc)

ROW = {'branch_id': B, 'roi_id': R, 'product_id': P, 'sku': 'SKU-1',
       'shelf_name': 'Estante A', 'node_id': N, 'gateway_id': G,
       'occupancy_pct': 18, 'config_version': 2, 'calibration_version': 1}


class FakeConnection:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.inserts, self.resolved, self.logs = [], [], []

    def execute(self, sql, args=()):
        result = Mock()
        if 'pg_advisory_xact_lock' in sql:
            pass
        elif 'FROM alert WHERE branch_id' in sql:
            result.fetchall.return_value = [a for a in self.existing
                                            if a['status'] in ('open', 'acknowledged')]
        elif 'UPDATE alert SET status' in sql:
            self.resolved.append(args)
            for alert in self.existing:
                alert['status'] = 'resolved'
        elif 'INSERT INTO alert' in sql:
            row = {'id': UUID(int=len(self.inserts) + 100)}
            self.inserts.append({'args': args, 'id': row['id']})
            self.existing.append({'id': row['id'], 'alert_type': args[1], 'status': 'open'})
            result.fetchone.return_value = row
        elif 'INSERT INTO system_log' in sql:
            self.logs.append(args)
        return result

    def commit(self):
        self.committed = True


class VisualEpisodeTests(unittest.TestCase):
    def test_low_opens_once_and_does_not_repeat(self):
        conn = FakeConnection()
        visual_episode(conn, row=ROW, quality='ok', status='low')
        visual_episode(conn, row=ROW, quality='ok', status='low')
        self.assertEqual(len(conn.inserts), 1)
        args = conn.inserts[0]['args']
        self.assertEqual(args[1], 'stock_low')
        self.assertEqual(args[2], 'warning')
        self.assertIn('SKU-1', args[5])
        self.assertIn('Estante A', args[5])
        self.assertEqual(args[7], f'roi:{R}:stock_low')
        self.assertEqual(args[6].obj['occupancy_pct'], 18)
        self.assertEqual(args[6].obj['source'], 'roi_state_monitor')
        self.assertEqual(args[6].obj['product_id'], str(P))
        self.assertEqual(len(conn.logs), 1)

    def test_low_to_empty_closes_previous_and_opens_critical(self):
        conn = FakeConnection(existing=[{'id': UUID(int=7), 'alert_type': 'stock_low',
                                         'status': 'open'}])
        visual_episode(conn, row={**ROW, 'occupancy_pct': 0}, quality='ok', status='empty')
        self.assertEqual(len(conn.resolved), 1)
        self.assertEqual(len(conn.inserts), 1)
        args = conn.inserts[0]['args']
        self.assertEqual(args[1], 'stock_empty')
        self.assertEqual(args[2], 'critical')
        self.assertEqual(args[7], f'roi:{R}:stock_empty')

    def test_recovery_closes_all_stock_episodes_once(self):
        conn = FakeConnection(existing=[{'id': UUID(int=7), 'alert_type': 'stock_low',
                                         'status': 'acknowledged'}])
        visual_episode(conn, row=ROW, quality='ok', status='adequate')
        visual_episode(conn, row=ROW, quality='ok', status='adequate')
        self.assertEqual(len(conn.resolved), 1)
        self.assertEqual(len(conn.inserts), 0)
        self.assertEqual(len(conn.logs), 1)

    def test_stale_no_data_and_cam_moved_do_not_open_or_close(self):
        for quality in ('stale', 'no_data', 'camera_moved', 'not_calibrated'):
            with self.subTest(quality=quality):
                conn = FakeConnection(existing=[{'id': UUID(int=7), 'alert_type': 'stock_low',
                                                 'status': 'open'}])
                visual_episode(conn, row=ROW, quality=quality, status='low')
                self.assertEqual(len(conn.inserts), 0)
                self.assertEqual(len(conn.resolved), 0)


class ScanTests(unittest.TestCase):
    def test_scan_uses_fresh_state_and_opens_visual_alert(self):
        class ScanConnection(FakeConnection):
            def execute(self, sql, args=()):
                if 'SELECT DISTINCT s.branch_id' in sql:
                    result = Mock()
                    result.fetchall.return_value = [{'branch_id': B}]
                    return result
                if 'FROM roi r' in sql:
                    result = Mock()
                    result.fetchall.return_value = [{**ROW, 'last_reading_at': NOW,
                                                     'calibration_status': 'calibrated',
                                                     'status': 'low'}]
                    return result
                return super().execute(sql, args)

        conn = ScanConnection()

        @contextmanager
        def connect():
            yield conn

        with patch('app.visual_alerts.scan_branch_states') as classify:
            self.assertEqual(scan_visual_states(connect), 1)
        classify.assert_called_once()
        self.assertEqual(len(conn.inserts), 1)
        self.assertEqual(conn.inserts[0]['args'][1], 'stock_low')

    def test_only_active_branches_and_organizations_are_scanned(self):
        class CaptureConnection(FakeConnection):
            def __init__(self):
                super().__init__()
                self.branch_sql = None

            def execute(self, sql, args=()):
                if 'SELECT DISTINCT s.branch_id' in sql:
                    self.branch_sql = sql
                    result = Mock()
                    result.fetchall.return_value = []
                    return result
                return super().execute(sql, args)

        connection = CaptureConnection()

        @contextmanager
        def connect():
            yield connection

        self.assertEqual(scan_visual_states(connect), 0)
        self.assertIn('b.is_active', connection.branch_sql)
        self.assertIn('o.is_active', connection.branch_sql)


if __name__ == '__main__':
    unittest.main()

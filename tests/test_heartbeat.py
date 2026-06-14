"""Tests for the external dead-man's-switch heartbeat.

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from ops import heartbeat


class TestHeartbeat(unittest.TestCase):
    def test_noop_when_unconfigured(self):
        with patch.dict('os.environ', {'HEARTBEAT_URL': ''}), \
             patch.object(heartbeat.requests, 'post') as post:
            self.assertFalse(heartbeat.configured())
            self.assertFalse(heartbeat.send_heartbeat())
        post.assert_not_called()

    def test_healthy_pings_base_url(self):
        with patch.dict('os.environ', {'HEARTBEAT_URL': 'https://hc.io/abc'}), \
             patch.object(heartbeat.requests, 'post', return_value=MagicMock()) as post:
            self.assertTrue(heartbeat.send_heartbeat(healthy=True, note='alive'))
        self.assertEqual(post.call_args.args[0], 'https://hc.io/abc')

    def test_unhealthy_pings_fail_endpoint(self):
        with patch.dict('os.environ', {'HEARTBEAT_URL': 'https://hc.io/abc'}), \
             patch.object(heartbeat.requests, 'post', return_value=MagicMock()) as post:
            heartbeat.send_heartbeat(healthy=False)
        self.assertEqual(post.call_args.args[0], 'https://hc.io/abc/fail')

    def test_network_failure_never_raises(self):
        with patch.dict('os.environ', {'HEARTBEAT_URL': 'https://hc.io/abc'}), \
             patch.object(heartbeat.requests, 'post', side_effect=Exception('down')):
            self.assertFalse(heartbeat.send_heartbeat())


class TestHeartbeatMonitor(unittest.TestCase):
    def test_pings_when_configured_and_sends_no_notification(self):
        from core import engine
        mon = engine.HeartbeatMonitor()
        with patch('ops.heartbeat.configured', return_value=True), \
             patch('ops.heartbeat.send_heartbeat', return_value=True) as ping:
            out = mon.check({})
        ping.assert_called_once()
        self.assertEqual(out, [])

    def test_skips_when_unconfigured(self):
        from core import engine
        mon = engine.HeartbeatMonitor()
        with patch('ops.heartbeat.configured', return_value=False), \
             patch('ops.heartbeat.send_heartbeat') as ping:
            mon.check({})
        ping.assert_not_called()


if __name__ == '__main__':
    unittest.main()

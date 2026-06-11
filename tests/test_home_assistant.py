"""Tests for the Home Assistant smart-home connector (HTTP mocked).

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from skills import home_assistant as ha

ENV = {'HA_URL': 'http://ha.test:8123', 'HA_TOKEN': 'tok'}
STATES = [
    {'entity_id': 'light.bedroom', 'state': 'off', 'attributes': {'friendly_name': 'Bedroom'}},
    {'entity_id': 'light.kitchen', 'state': 'on', 'attributes': {'friendly_name': 'Kitchen Lamp'}},
    {'entity_id': 'sensor.temp', 'state': '70', 'attributes': {'friendly_name': 'Temp'}},
]


def _get_resp():
    r = MagicMock()
    r.json.return_value = STATES
    r.raise_for_status = MagicMock()
    return r


class TestHomeAssistant(unittest.TestCase):
    def test_not_configured_message(self):
        with patch.dict('os.environ', {'HA_URL': '', 'HA_TOKEN': ''}):
            self.assertIn("isn't connected", ha.list_lights.invoke({}))

    def test_list_only_lights(self):
        with patch.dict('os.environ', ENV), \
             patch.object(ha.requests, 'get', return_value=_get_resp()):
            out = ha.list_lights.invoke({})
        self.assertIn('Bedroom: off', out)
        self.assertIn('Kitchen Lamp: on', out)
        self.assertNotIn('Temp', out)            # non-lights excluded

    def test_control_resolves_name_and_posts_service(self):
        post = MagicMock(return_value=_get_resp())
        with patch.dict('os.environ', ENV), \
             patch.object(ha.requests, 'get', return_value=_get_resp()), \
             patch.object(ha.requests, 'post', post):
            out = ha.control_light.invoke({'name': 'kitchen', 'turn': 'on',
                                           'brightness_pct': 60, 'color': 'warm white'})
        url = post.call_args.args[0]
        data = post.call_args.kwargs['json']
        self.assertIn('light/turn_on', url)
        self.assertEqual(data['entity_id'], 'light.kitchen')
        self.assertEqual(data['brightness_pct'], 60)
        self.assertEqual(data['color_name'], 'warmwhite')
        self.assertIn('Kitchen Lamp', out)

    def test_all_targets_every_light(self):
        post = MagicMock(return_value=_get_resp())
        with patch.dict('os.environ', ENV), \
             patch.object(ha.requests, 'get', return_value=_get_resp()), \
             patch.object(ha.requests, 'post', post):
            out = ha.control_light.invoke({'name': 'all', 'turn': 'off'})
        self.assertEqual(post.call_count, 2)      # both lights, not the sensor
        self.assertIn('turn_off', post.call_args.args[0])

    def test_unknown_light_lists_available(self):
        with patch.dict('os.environ', ENV), \
             patch.object(ha.requests, 'get', return_value=_get_resp()):
            out = ha.control_light.invoke({'name': 'garage', 'turn': 'on'})
        self.assertIn("No light matching 'garage'", out)
        self.assertIn('Bedroom', out)


if __name__ == '__main__':
    unittest.main()

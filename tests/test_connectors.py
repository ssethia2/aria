"""Tests for the weather and research connectors (network mocked).

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from skills import weather_manager as wm
from skills import research_manager as rm


class TestWeather(unittest.TestCase):
    def _forecast_response(self):
        return {'daily': {
            'time': ['2026-06-11'],
            'temperature_2m_max': [75.2], 'temperature_2m_min': [58.1],
            'precipitation_probability_max': [40], 'weather_code': [61]}}

    def test_profile_location_geocoded_and_formatted(self):
        geo = MagicMock()
        geo.json.return_value = {'results': [
            {'latitude': 42.3, 'longitude': -71.0, 'name': 'Boston'}]}
        forecast = MagicMock()
        forecast.json.return_value = self._forecast_response()
        with patch.object(wm, 'load_profile', return_value={'location': 'Boston'}), \
             patch.object(wm.requests, 'get', side_effect=[geo, forecast]):
            lines = wm.fetch_weather_lines(days=1)
        self.assertIn('Today: rainy, high 75°F / low 58°F', lines[0])
        self.assertIn('40% chance of rain', lines[0])
        self.assertIn('Boston', lines[0])

    def test_failure_returns_none_not_raise(self):
        with patch.object(wm, 'load_profile', return_value={}), \
             patch.object(wm.requests, 'get', side_effect=Exception('net down')):
            self.assertIsNone(wm.fetch_weather_lines())

    def test_low_rain_chance_omitted(self):
        resp = self._forecast_response()
        resp['daily']['precipitation_probability_max'] = [5]
        resp['daily']['weather_code'] = [0]
        forecast = MagicMock()
        forecast.json.return_value = resp
        ip = MagicMock()
        ip.json.return_value = {'lat': 1, 'lon': 2, 'city': 'X'}
        with patch.object(wm, 'load_profile', return_value={}), \
             patch.object(wm.requests, 'get', side_effect=[ip, forecast]):
            lines = wm.fetch_weather_lines()
        self.assertNotIn('rain', lines[0])
        self.assertIn('clear', lines[0])


class TestResearch(unittest.TestCase):
    def test_fetch_strips_chrome_and_truncates(self):
        html = ('<html><head><style>x{}</style></head><body><nav>menu</nav>'
                '<p>real content here</p><script>junk()</script>'
                '<footer>footer</footer></body></html>')
        resp = MagicMock(text=html)
        resp.raise_for_status = MagicMock()
        with patch.object(rm.requests, 'get', return_value=resp):
            out = rm.fetch_webpage.invoke({'url': 'https://x.com'})
        self.assertIn('real content here', out)
        self.assertNotIn('menu', out)
        self.assertNotIn('junk', out)

    def test_fetch_failure_is_graceful(self):
        with patch.object(rm.requests, 'get', side_effect=Exception('refused')):
            out = rm.fetch_webpage.invoke({'url': 'https://x.com'})
        self.assertIn("Couldn't fetch", out)


if __name__ == '__main__':
    unittest.main()

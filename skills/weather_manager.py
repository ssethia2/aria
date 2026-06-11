"""Weather connector — Open-Meteo (free, keyless).

Location resolution: profile.json 'location' (a city name, geocoded once per
call) with IP-geolocation fallback, so it works before the user ever sets a
location. Powers the get_weather tool and the morning briefing's lead line.
"""
import requests
from langchain_core.tools import tool

from memory import load_profile

# WMO weather interpretation codes -> plain words
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "drizzly", 53: "drizzly", 55: "drizzly",
    61: "rainy", 63: "rainy", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "snowy", 73: "snowy", 75: "heavy snow",
    77: "snowy", 80: "showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorms", 96: "thunderstorms", 99: "thunderstorms",
}


def _describe(code) -> str:
    return _WMO.get(code, "mixed conditions")


def _resolve_location():
    """(lat, lon, place): profile 'location' city if set, else IP geolocation."""
    city = (load_profile() or {}).get('location')
    if city:
        r = requests.get('https://geocoding-api.open-meteo.com/v1/search',
                         params={'name': city, 'count': 1}, timeout=10).json()
        if r.get('results'):
            g = r['results'][0]
            return g['latitude'], g['longitude'], g['name']
    r = requests.get('http://ip-api.com/json', timeout=10).json()
    return r['lat'], r['lon'], r.get('city', 'your area')


def fetch_weather_lines(days: int = 1):
    """Forecast lines for tools/briefing. Returns None on any failure (best-effort)."""
    try:
        lat, lon, place = _resolve_location()
        r = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': lat, 'longitude': lon,
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code',
            'temperature_unit': 'fahrenheit', 'timezone': 'auto',
            'forecast_days': max(1, min(days, 7))}, timeout=10).json()
        d = r['daily']
        lines = []
        for i, date in enumerate(d['time']):
            hi, lo = d['temperature_2m_max'][i], d['temperature_2m_min'][i]
            rain = d['precipitation_probability_max'][i]
            desc = _describe(d['weather_code'][i])
            day_label = "Today" if i == 0 else date
            line = f"{day_label}: {desc}, high {hi:.0f}°F / low {lo:.0f}°F"
            if rain and rain >= 20:
                line += f", {rain}% chance of rain"
            lines.append(line)
        lines[0] += f" — {place}"
        return lines
    except Exception as e:
        print(f"[weather] fetch failed: {e}")
        return None


@tool
def get_weather(days: int = 1) -> str:
    """Current weather forecast for the user's location. days=1 for today,
    up to 7 for the week. Use when asked about weather or when planning
    outdoor events/travel."""
    lines = fetch_weather_lines(days)
    if not lines:
        return "Couldn't reach the weather service just now."
    return "\n".join(lines)

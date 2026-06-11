"""Smart home via Home Assistant — Aria controls Matter devices through HA's API.

Architecture decision (2026-06): Aria does NOT speak Matter directly (commissioning,
Thread, Apple-Home fabric conflicts = a rabbit hole). Home Assistant is the controller
— it owns the Matter pairing and exposes a clean REST API; Aria is just an HTTP client.
HA belongs on the always-on host (Pi/laptop). Matter multi-admin lets the bulbs stay in
Apple Home AND join HA via one pairing code.

Activation (no code change): run HA, add the Matter integration + commission the bulbs,
create a long-lived access token, then in .env:
    HA_URL=http://homeassistant.local:8123
    HA_TOKEN=<long-lived token>
Generalizes beyond lights later (switches, climate, scenes) via the same service call.
"""
import os

import requests
from langchain_core.tools import tool


def _base():
    return (os.getenv('HA_URL') or '').rstrip('/')


def _headers():
    return {'Authorization': f"Bearer {os.getenv('HA_TOKEN')}",
            'Content-Type': 'application/json'}


def _configured() -> bool:
    return bool(_base() and os.getenv('HA_TOKEN'))


_NOT_CONFIGURED = ("Smart home isn't connected yet — set HA_URL and HA_TOKEN in .env "
                   "(Home Assistant on the always-on host; see skills/home_assistant.py).")


def _light_states():
    """All light.* entities as {entity_id, name, state}. Raises on transport error."""
    r = requests.get(f"{_base()}/api/states", headers=_headers(), timeout=10)
    r.raise_for_status()
    lights = []
    for e in r.json():
        if e['entity_id'].startswith('light.'):
            lights.append({
                'entity_id': e['entity_id'],
                'name': e.get('attributes', {}).get('friendly_name', e['entity_id']),
                'state': e.get('state', 'unknown')})
    return lights


def _resolve(name: str, lights):
    """Best friendly-name match: exact, then substring."""
    low = name.strip().lower()
    for l in lights:
        if l['name'].lower() == low:
            return l
    for l in lights:
        if low in l['name'].lower():
            return l
    return None


@tool
def list_lights() -> str:
    """List the smart lights and whether each is on or off. Use when asked what
    lights exist or their state."""
    if not _configured():
        return _NOT_CONFIGURED
    try:
        lights = _light_states()
    except Exception as e:
        return f"Couldn't reach Home Assistant: {e}"
    if not lights:
        return "No lights found in Home Assistant."
    return "Lights:\n" + "\n".join(f"- {l['name']}: {l['state']}" for l in lights)


@tool
def control_light(name: str, turn: str = 'on', brightness_pct: int = None,
                  color: str = None) -> str:
    """Turn a smart light on/off and optionally set brightness or color.

    Args:
        name: Friendly name of the light (e.g. "bedroom", "kitchen lamp"). Matches
            loosely; use "all" to control every light.
        turn: 'on' or 'off'.
        brightness_pct: 0-100 (only when turning on).
        color: a color name (e.g. "warm white", "blue", "red") — only when turning on.
    """
    if not _configured():
        return _NOT_CONFIGURED
    if turn not in ('on', 'off'):
        return "turn must be 'on' or 'off'."
    try:
        lights = _light_states()
    except Exception as e:
        return f"Couldn't reach Home Assistant: {e}"

    if name.strip().lower() == 'all':
        targets = lights
    else:
        match = _resolve(name, lights)
        if not match:
            avail = ', '.join(l['name'] for l in lights) or 'none'
            return f"No light matching '{name}'. Available: {avail}."
        targets = [match]

    service = f"light/turn_{turn}"
    done = []
    for t in targets:
        data = {'entity_id': t['entity_id']}
        if turn == 'on':
            if brightness_pct is not None:
                data['brightness_pct'] = max(0, min(100, brightness_pct))
            if color:
                data['color_name'] = color.replace(' ', '').lower()
        try:
            r = requests.post(f"{_base()}/api/services/{service}",
                              headers=_headers(), json=data, timeout=10)
            r.raise_for_status()
            done.append(t['name'])
        except Exception as e:
            return f"Failed to control {t['name']}: {e}"

    extras = []
    if turn == 'on' and brightness_pct is not None:
        extras.append(f"{brightness_pct}%")
    if turn == 'on' and color:
        extras.append(color)
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"Turned {turn}: {', '.join(done)}{suffix}."

"""Spotify control — play music, transport, now-playing, and playlist building.

Uses the Authorization-Code flow (user-scoped: playback + playlists), so it needs a
one-time `python3 auth_spotify.py` to cache a refresh token. Activates on
SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET in .env; tools no-op with a clear message
until then. Playback requires an active Spotify device (Spotify Connect) — if none is
playing, the tool says to open Spotify somewhere.
"""
import os

from langchain_core.tools import tool

BASE = os.path.dirname(os.path.dirname(__file__))
CACHE_PATH = os.path.join(BASE, '.spotify_cache')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:8888/callback')
SCOPES = ("user-read-playback-state user-modify-playback-state "
          "user-read-currently-playing playlist-modify-private playlist-modify-public")

_NOT_SET = ("Spotify isn't connected — set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET "
            "in .env, then run `python3 auth_spotify.py` once.")
_NOT_AUTHED = "Spotify needs a one-time authorization — run `python3 auth_spotify.py`."


def auth_manager():
    cid, secret = os.getenv('SPOTIFY_CLIENT_ID'), os.getenv('SPOTIFY_CLIENT_SECRET')
    if not (cid and secret):
        return None
    from spotipy.oauth2 import SpotifyOAuth
    return SpotifyOAuth(client_id=cid, client_secret=secret, redirect_uri=REDIRECT_URI,
                        scope=SCOPES, cache_path=CACHE_PATH, open_browser=False)


def _client():
    """A ready spotipy client, or None if unconfigured/unauthorized."""
    am = auth_manager()
    if not am or not os.path.exists(CACHE_PATH):
        return None
    import spotipy
    return spotipy.Spotify(auth_manager=am)


def _active_device_id(sp):
    """The active device, or the first available one (so playback works even if Spotify
    is open but idle). None if no device is reachable."""
    try:
        devices = sp.devices().get('devices', [])
    except Exception:
        return None
    if not devices:
        return None
    for d in devices:
        if d.get('is_active'):
            return d['id']
    return devices[0]['id']


@tool
def play_music(query: str) -> str:
    """Search Spotify and start playing the best match — a song, artist, album, or
    playlist (e.g. "play Weightless by Marconi Union", "play some lo-fi"). Requires an
    open Spotify device."""
    sp = _client()
    if sp is None:
        return _NOT_SET if not auth_manager() else _NOT_AUTHED
    try:
        results = sp.search(q=query, type='track', limit=1)
        items = results.get('tracks', {}).get('items', [])
        if not items:
            return f"Couldn't find anything on Spotify for '{query}'."
        track = items[0]
        device_id = _active_device_id(sp)
        if not device_id:
            return "No active Spotify device — open Spotify on your phone or desktop first."
        sp.start_playback(device_id=device_id, uris=[track['uri']])
        artists = ", ".join(a['name'] for a in track['artists'])
        return f"▶️ Playing {track['name']} by {artists}."
    except Exception as e:
        return f"Spotify error: {e}"


@tool
def playback_control(action: str) -> str:
    """Control playback: 'pause', 'resume', 'next', or 'previous'."""
    sp = _client()
    if sp is None:
        return _NOT_SET if not auth_manager() else _NOT_AUTHED
    try:
        if action == 'pause':
            sp.pause_playback(); return "⏸ Paused."
        if action == 'resume':
            sp.start_playback(); return "▶️ Resumed."
        if action == 'next':
            sp.next_track(); return "⏭ Skipped."
        if action == 'previous':
            sp.previous_track(); return "⏮ Previous track."
        return "action must be pause, resume, next, or previous."
    except Exception as e:
        return f"Spotify error: {e} (is a device active?)"


@tool
def now_playing() -> str:
    """What's currently playing on Spotify."""
    sp = _client()
    if sp is None:
        return _NOT_SET if not auth_manager() else _NOT_AUTHED
    try:
        cur = sp.current_playback()
        if not cur or not cur.get('item'):
            return "Nothing is playing right now."
        t = cur['item']
        artists = ", ".join(a['name'] for a in t['artists'])
        state = "▶️" if cur.get('is_playing') else "⏸"
        return f"{state} {t['name']} by {artists} ({t['album']['name']})."
    except Exception as e:
        return f"Spotify error: {e}"


@tool
def create_playlist(name: str, songs: str) -> str:
    """Create a Spotify playlist and add songs to it. For a themed request ("a playlist
    for a rainy Sunday"), pick the tracks yourself and pass them. `songs` is one
    "Title - Artist" per line (or comma-separated)."""
    sp = _client()
    if sp is None:
        return _NOT_SET if not auth_manager() else _NOT_AUTHED
    import re
    wanted = [s.strip() for s in re.split(r'[\n,;]+', songs) if s.strip()]
    if not wanted:
        return "Give me at least one song."
    try:
        user_id = sp.me()['id']
        playlist = sp.user_playlist_create(user_id, name, public=False)
        uris, missing = [], []
        for q in wanted:
            res = sp.search(q=q, type='track', limit=1)
            items = res.get('tracks', {}).get('items', [])
            (uris.append(items[0]['uri']) if items else missing.append(q))
        if uris:
            sp.playlist_add_items(playlist['id'], uris)
        msg = f"🎵 Created '{name}' with {len(uris)} track(s)."
        if missing:
            msg += f" Couldn't find: {', '.join(missing)}."
        return msg
    except Exception as e:
        return f"Spotify error: {e}"

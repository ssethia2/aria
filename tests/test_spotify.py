"""Tests for the Spotify skill (spotipy client mocked).

Run: python3 -m unittest discover tests
"""
import unittest
from unittest.mock import MagicMock, patch

from skills import spotify_manager as sm


def _sp(track="Weightless", artist="Marconi Union", devices=("dev1",)):
    sp = MagicMock()
    sp.search.return_value = {'tracks': {'items': [
        {'uri': 'spotify:track:1', 'name': track, 'artists': [{'name': artist}],
         'album': {'name': 'Album'}}]}}
    sp.devices.return_value = {'devices': [{'id': d, 'is_active': i == 0}
                                           for i, d in enumerate(devices)]}
    sp.me.return_value = {'id': 'satvik'}
    sp.user_playlist_create.return_value = {'id': 'pl1'}
    sp.current_playback.return_value = {'is_playing': True, 'item': {
        'name': track, 'artists': [{'name': artist}], 'album': {'name': 'Album'}}}
    return sp


class TestSpotify(unittest.TestCase):
    def test_not_configured_message(self):
        with patch.object(sm, 'auth_manager', return_value=None), \
             patch.object(sm, '_client', return_value=None):
            self.assertIn("isn't connected", sm.play_music.invoke({'query': 'x'}))

    def test_play_uses_active_device(self):
        sp = _sp()
        with patch.object(sm, '_client', return_value=sp):
            msg = sm.play_music.invoke({'query': 'weightless'})
        sp.start_playback.assert_called_once()
        self.assertEqual(sp.start_playback.call_args.kwargs['device_id'], 'dev1')
        self.assertIn('Weightless', msg)

    def test_play_no_device(self):
        sp = _sp(devices=())
        with patch.object(sm, '_client', return_value=sp):
            msg = sm.play_music.invoke({'query': 'x'})
        self.assertIn('No active Spotify device', msg)
        sp.start_playback.assert_not_called()

    def test_control_actions(self):
        sp = _sp()
        with patch.object(sm, '_client', return_value=sp):
            self.assertIn('Paused', sm.playback_control.invoke({'action': 'pause'}))
            self.assertIn('Skipped', sm.playback_control.invoke({'action': 'next'}))
        sp.pause_playback.assert_called_once()
        sp.next_track.assert_called_once()

    def test_now_playing(self):
        with patch.object(sm, '_client', return_value=_sp()):
            self.assertIn('Weightless', sm.now_playing.invoke({}))

    def test_create_playlist_adds_found_tracks(self):
        sp = _sp()
        with patch.object(sm, '_client', return_value=sp):
            msg = sm.create_playlist.invoke({'name': 'Rainy Sunday',
                                             'songs': 'Weightless - Marconi Union\nAnother - X'})
        sp.user_playlist_create.assert_called_once()
        sp.playlist_add_items.assert_called_once()
        self.assertIn('Rainy Sunday', msg)


if __name__ == '__main__':
    unittest.main()

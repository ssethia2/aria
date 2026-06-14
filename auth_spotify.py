"""One-time Spotify authorization — caches a refresh token for the Spotify skill.

Needs SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET in .env, and the redirect URI
(default http://127.0.0.1:8888/callback) registered in your Spotify app at
https://developer.spotify.com/dashboard. Opens a browser to approve, then caches
the token to .spotify_cache. Run once: `python3 auth_spotify.py`.
"""
from dotenv import load_dotenv

load_dotenv()

from skills.spotify_manager import auth_manager, REDIRECT_URI  # noqa: E402


def main():
    am = auth_manager()
    if not am:
        print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.")
        return
    print(f"Authorizing Spotify. Make sure this redirect URI is registered in your "
          f"Spotify app settings:\n  {REDIRECT_URI}\n")
    am.open_browser = True
    token = am.get_access_token(as_dict=False)  # opens browser, runs local callback, caches
    if token:
        import spotipy
        me = spotipy.Spotify(auth_manager=am).me()
        print(f"✅ Authorized as {me.get('display_name') or me.get('id')}. Spotify is connected.")
    else:
        print("Authorization did not complete.")


if __name__ == '__main__':
    main()

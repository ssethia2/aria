"""One-time OAuth bootstrap for the SECONDARY (Netflix-tied) Gmail account.

Opens a browser to authorize the second account and writes token_netflix.json.
Switch Google accounts in the browser before authorizing. Run once: `python3 auth_netflix.py`.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify'
]

def authenticate_netflix_account():
    creds_path = os.path.join(os.path.dirname(__file__), 'credentials.json')
    token_path = os.path.join(os.path.dirname(__file__), 'token_netflix.json')
    
    if not os.path.exists(creds_path):
        print("Error: credentials.json not found!")
        return

    print("Opening browser to authenticate the NETFLIX-tied Gmail account...")
    print("WARNING: Make sure you switch Google accounts and log into the correct one!")
    
    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    creds = flow.run_local_server(port=0)
    
    import token_store
    token_store.atomic_write_text(token_path, creds.to_json())   # never leaves it empty

    print(f"✅ Successfully created {token_path}")

if __name__ == '__main__':
    authenticate_netflix_account()

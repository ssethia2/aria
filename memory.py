import json
import os

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "profile.json")

def load_profile():
    if not os.path.exists(PROFILE_PATH):
        return {}
    with open(PROFILE_PATH, "r") as f:
        return json.load(f)

def save_profile(data):
    with open(PROFILE_PATH, "w") as f:
        json.dump(data, f, indent=4)
        
def update_profile(key, value):
    """Updates a specific key in the user's profile memory."""
    profile = load_profile()
    profile[key] = value
    save_profile(profile)
    print(f"✅ Memory Updated: {key} -> {value}")

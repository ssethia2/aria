"""Always-on FastAPI webhook server (Phase 4 — partial).

Receives Gmail Pub/Sub push notifications at POST /webhook/gmail and triggers the
Netflix household updater instantly. Run: `python3 aria_server.py` (port 8000).
Register the Gmail watch with setup_gmail_watch.py.
"""
from fastapi import FastAPI, Request
from skills.netflix_manager import update_netflix_household
import json
import base64

app = FastAPI(title="Aria Web API")

@app.get("/")
def health_check():
    return {"status": "Aria is online and listening for webhooks."}

@app.post("/webhook/gmail")
async def gmail_webhook(request: Request):
    """Endpoint for Google Cloud Pub/Sub push notifications."""
    try:
        body = await request.json()
        message = body.get("message", {})
        data = message.get("data")
        
        if data:
            decoded_data = base64.b64decode(data).decode('utf-8')
            msg_json = json.loads(decoded_data)
            email_address = msg_json.get("emailAddress")
            
            print(f"\\n🔔 Received Instant Gmail Push Webhook for: {email_address}")
            
            # Trigger the Netflix updater tool autonomously
            # The tool inherently checks for the *latest* unread update email and fires if it's there
            print("Triggering Netflix Automation Tool via Webhook...")
            result = update_netflix_household.invoke({})
            print(f"Status: {result}")
            
        return {"status": "Webook Received and Processed"}
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return {"status": "Error", "detail": str(e)}

if __name__ == "__main__":
    import uvicorn
    print("🚀 Booting up Aria's 24/7 FastAPI Web Server on port 8000...")
    uvicorn.run(app, host="0.0.0.0", port=8000)

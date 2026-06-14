"""Standalone bulk inbox cleaner (manual, one-shot).

Pages through the entire INBOX in batches and labels junk "To Be Deleted" for
manual review — it never permanently deletes. Pins Gemini Flash directly (not the
router) for cost on large batches. Run: `python3 clean_inbox.py`.
"""
import os
import sys
import json
import time
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate

# Ensure local imports work correctly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from skills.email_manager import get_gmail_service, get_or_create_label, apply_label_to_email
from core.memory import load_profile

# Load environment variables
load_dotenv()

def fetch_batch_of_emails(service, max_results=20, page_token=None):
    """Fetch a batch of emails from INBOX."""
    try:
        kwargs = {'userId': 'me', 'labelIds': ['INBOX'], 'maxResults': max_results}
        if page_token:
            kwargs['pageToken'] = page_token
            
        results = service.users().messages().list(**kwargs).execute()
        messages = results.get('messages', [])
        next_page_token = results.get('nextPageToken')

        if not messages:
            return [], None
        
        email_data = []
        for message in messages:
            msg = service.users().messages().get(userId='me', id=message['id']).execute()
            
            headers = msg['payload'].get('headers', [])
            subject = next((header['value'] for header in headers if header['name'] == 'Subject'), 'No Subject')
            sender = next((header['value'] for header in headers if header['name'] == 'From'), 'Unknown Sender')
            snippet = msg.get('snippet', '')
            
            email_data.append({
                'id': message['id'],
                'subject': subject,
                'sender': sender,
                'snippet': snippet
            })
            
        return email_data, next_page_token

    except HttpError as error:
        print(f'An error occurred fetching emails: {error}')
        return [], None

def main():
    print("Starting Standalone Inbox Cleaner...")
    service = get_gmail_service()
    if not service:
        print("Failed to start Gmail service. Check credentials.")
        return
        
    try:
        # Using Gemini 2.5 Flash as it is extremely fast and cost-effective for large batches
        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)
    except Exception as e:
        print(f"Failed to intialize LLM. Error: {e}")
        return

    profile_data = load_profile()
    profile_text = json.dumps(profile_data, indent=2) if profile_data else "No specific personal context available."

    prompt = PromptTemplate(
        input_variables=["email_data", "profile_data"],
        template='''You are an intelligent email assistant. Analyze the following emails to help clean the user's inbox in bulk.
        For each email, decide if it should be "KEEP" or "DELETE" (junk/promotion/spam).
        
        Here is what you know about the user (use this to ensure you do NOT delete things they care about!):
        {profile_data}
        
        Emails:
        {email_data}
        
        Return ONLY a raw JSON array of objects with the following keys:
        - "id": the email id
        - "action": "KEEP" or "DELETE"
        
        Do not output markdown code blocks. Just the raw JSON array.
        '''
    )
    chain = prompt | llm

    label_id = get_or_create_label(service, "To Be Deleted")
    
    # --- Configuration ---
    BATCH_SIZE = 20
    MAX_BATCHES = 500 # Defaulting to processing 100 emails max to avoid excessive API usage/rate limits on first run
    # ---------------------
    
    page_token = None
    total_processed = 0
    total_deleted = 0
    
    for batch_num in range(1, MAX_BATCHES + 1):
        print(f"\n--- Fetching Batch {batch_num} (Up to {BATCH_SIZE} emails) ---")
        emails, page_token = fetch_batch_of_emails(service, max_results=BATCH_SIZE, page_token=page_token)
        
        if not emails:
            print("No more emails found in INBOX.")
            break
            
        print(f"Analyzing {len(emails)} emails with AI...")
        formatted_emails = json.dumps(emails, indent=2)
        
        try:
            response = chain.invoke({
                "email_data": formatted_emails,
                "profile_data": profile_text
            })
            
            cleaned_response = response.content.strip().strip('```json').strip('```')
            classifications = json.loads(cleaned_response)
            
            for item in classifications:
                email_info = next((e for e in emails if e['id'] == item['id']), None)
                if not email_info: continue
                    
                total_processed += 1
                if item.get('action') == 'DELETE':
                    subject = email_info['subject'].replace('\n', ' ')
                    print(f"🗑️  TRASHING: {subject[:50]}...")
                    apply_label_to_email(service, item['id'], label_id)
                    total_deleted += 1
                else:
                    subject = email_info['subject'].replace('\n', ' ')
                    print(f"✅ KEEPING: {subject[:50]}...")
            
            # Rate limiting safety to prevent 429 errors from Google APIs
            time.sleep(2)
            
        except Exception as e:
            print(f"Error processing batch: {e}")
            break
            
        if not page_token:
            print("\nReached the end of the INBOX.")
            break

    print(f"\n🎉 Inbox Cleanup Operation Complete!")
    print(f"Total Emails Processed: {total_processed}")
    print(f"Total Labeled for Deletion: {total_deleted}")
    print("Review the 'To Be Deleted' label in Gmail, then you can select all and delete them safely.")

if __name__ == "__main__":
    main()

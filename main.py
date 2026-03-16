import os
from dotenv import load_dotenv

from report_generator import generate_daily_markdown
from skills.email_manager import run_email_summary, get_gmail_service, send_email
from skills.news_manager import generate_news_brief

# Load environment variables
load_dotenv()

def main():
    print("Welcome to your Personal Assistant.")
        
    classifications, raw_emails = run_email_summary()
    news_briefing = generate_news_brief()
    
    if classifications or news_briefing:
        report_path, markdown_content = generate_daily_markdown(classifications, raw_emails, news_briefing)
        
        # Deliver the morning report via email
        service = get_gmail_service()
        if service:
            try:
                profile = service.users().getProfile(userId='me').execute()
                user_email = profile['emailAddress']
                send_email(service, user_email, "Your Daily Assistant Summary ☀️", markdown_content)
            except Exception as e:
                print(f"Failed to fetch user email or send summary: {e}")

if __name__ == "__main__":
    main()

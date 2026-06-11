"""Morning-briefing batch job (entry point for cron/launchd, invoked via run.sh).

Runs the email summary, news brief, and today's reminders, renders one Markdown
report, and delivers it to the user — primarily as a Telegram message from Aria,
with email as a second channel. Fails LOUD: any error or non-delivery triggers a
Telegram alert rather than a silent miss. For the conversational interfaces see
telegram_bot.py / interact.py.
"""
import traceback

from dotenv import load_dotenv

from report_generator import generate_daily_markdown
from skills.email_manager import run_email_summary, get_gmail_service, send_email
from skills.news_manager import generate_news_brief
from skills.commitment_manager import (get_due_commitments, get_upcoming_commitments,
                                       format_line)
from notify import send_telegram

load_dotenv()


def deliver(markdown_content: str) -> list:
    """Send the briefing via Telegram and email. Returns the channels that worked."""
    delivered = []

    if send_telegram(f"☀️ Good morning! Here's your daily briefing:\n\n{markdown_content}"):
        delivered.append("telegram")

    try:
        service = get_gmail_service()
        if service:
            profile = service.users().getProfile(userId='me').execute()
            user_email = profile['emailAddress']
            if send_email(service, user_email, "Your Daily Assistant Summary ☀️", markdown_content):
                delivered.append("email")
    except Exception as e:
        print(f"Email delivery failed: {e}")

    return delivered


def main():
    print("Welcome to your Personal Assistant.")

    classifications, raw_emails = run_email_summary()
    news_briefing = generate_news_brief()
    # Agenda: today's calendar first, then due/overdue commitments, then the week ahead.
    reminders = []
    try:
        from skills.google_calendar import fetch_events
        for line in (fetch_events(days=1) or []):
            reminders.append({'task': f"🗓 {line}"})
    except Exception as e:
        print(f"Calendar fetch for briefing failed: {e}")
    reminders += [{'task': format_line(c)} for c in get_due_commitments()]
    reminders += [{'task': f"(upcoming) {format_line(c)}"} for c in get_upcoming_commitments()]

    if not (classifications or news_briefing or reminders):
        # A completely empty morning is unusual enough to be worth a heads-up —
        # it can also mean upstream auth/fetch problems, so don't stay silent.
        send_telegram("☀️ Morning check-in: no new emails, news, or reminders today.")
        return

    report_path, markdown_content = generate_daily_markdown(
        classifications, raw_emails, news_briefing, reminders)

    delivered = deliver(markdown_content)
    if not delivered:
        raise RuntimeError(
            "briefing was generated but could not be delivered on any channel "
            f"(report saved at {report_path})")
    print(f"✅ Briefing delivered via: {', '.join(delivered)}")


def run_with_alerts():
    """Run the briefing, failing LOUD: any error is telegrammed, never silently skipped."""
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        print(err)
        send_telegram(f"⚠️ My morning briefing run failed:\n\n{err[-1500:]}")
        raise


if __name__ == "__main__":
    run_with_alerts()

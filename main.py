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


def deliver(telegram_md: str, email_md: str) -> list:
    """Telegram gets the lean briefing (no news); email gets the full one with the news
    brief. Returns the channels that worked."""
    delivered = []

    # Weather leads the morning Telegram note (best-effort — never blocks).
    weather = ""
    try:
        from skills.weather_manager import fetch_weather_lines
        lines = fetch_weather_lines(days=1)
        if lines:
            weather = f"{lines[0]}\n\n"
    except Exception as e:
        print(f"Weather for briefing failed: {e}")

    if send_telegram(f"☀️ Good morning! {weather}Here's your day:\n\n{telegram_md}"):
        delivered.append("telegram")

    try:
        service = get_gmail_service()
        if service:
            profile = service.users().getProfile(userId='me').execute()
            user_email = profile['emailAddress']
            if send_email(service, user_email, "Your Daily Assistant Summary ☀️", email_md):
                delivered.append("email")
        else:
            # Gmail unusable (token expired/revoked) — say so loudly so it doesn't just
            # silently skip the email briefing. The Telegram briefing above still went out.
            send_telegram("📭 Couldn't email your briefing — Gmail needs re-auth. "
                          "Run `python3 auth_google.py` on the host.")
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

    # Two renders: full (with news) for email, lean (no news) for Telegram — the news
    # brief is long and belongs in email, not the phone glance.
    report_path, email_md = generate_daily_markdown(
        classifications, raw_emails, news_briefing, reminders)
    _, telegram_md = generate_daily_markdown(
        classifications, raw_emails, None, reminders)

    delivered = deliver(telegram_md, email_md)
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

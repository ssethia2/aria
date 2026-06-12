"""First-run wizard — guided .env setup so a newcomer never hand-edits config.

Interactive: prompts for the few values needed to start (the 3 required + 2
recommended), preserves existing values and .env.example's comments, writes .env,
then runs the doctor to show what's left. Run: `python3 setup_wizard.py`.
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, '.env')
EXAMPLE_PATH = os.path.join(BASE, '.env.example')

# (key, human description, required)
PROMPTS = [
    ('TELEGRAM_BOT_TOKEN', 'Telegram bot token (from @BotFather → /newbot)', True),
    ('ANTHROPIC_API_KEY', 'Anthropic API key (console.anthropic.com)', True),
    ('USER_EMAIL', 'Your email (briefing recipient + send allowlist)', False),
    ('GEMINI_API_KEY', 'Gemini key (semantic memory + fallback; optional)', False),
]


def _existing_values(path) -> dict:
    vals = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                s = line.strip()
                if '=' in s and not s.startswith('#'):
                    k, v = s.split('=', 1)
                    vals[k.strip()] = v.strip()
    return vals


def render_env(template_lines, values) -> str:
    """Fill KEY= lines in the template from `values`; preserve comments and any keys
    not in `values`. Pure function — the testable core of the wizard."""
    out = []
    for line in template_lines:
        s = line.strip()
        if '=' in s and not s.startswith('#'):
            key = s.split('=', 1)[0].strip()
            if values.get(key):
                out.append(f"{key}={values[key]}")
                continue
        out.append(line.rstrip('\n'))
    return "\n".join(out) + "\n"


def main():
    print("🌱 Aria first-run setup\n")
    if not os.path.exists(EXAMPLE_PATH):
        print("Missing .env.example — run from the repo root.")
        return
    with open(EXAMPLE_PATH) as f:
        template = f.readlines()

    existing = _existing_values(ENV_PATH)
    values = dict(existing)

    for key, desc, required in PROMPTS:
        cur = existing.get(key, '')
        hint = " [Enter to keep current]" if cur else (" (required)" if required else " (optional, Enter to skip)")
        while True:
            ans = input(f"{desc}{hint}\n  {key}= ").strip()
            if not ans and cur:
                ans = cur
            if ans or not required:
                break
            print("  This one's required.")
        if ans:
            values[key] = ans
        print()

    with open(ENV_PATH, 'w') as f:
        f.write(render_env(template, values))
    print(f"✅ Wrote {ENV_PATH}\n")

    print("Running the health check to show what's left:\n" + "-" * 50)
    try:
        from healthcheck import run_all, summary
        print(summary(run_all()))
    except Exception as e:
        print(f"(health check skipped: {e})")
    print("-" * 50)
    print("\nNext: start her with  venv/bin/python3 telegram_bot.py")
    print("On first run, message your bot — it replies with your chat id; paste that")
    print("into .env as TELEGRAM_ALLOWED_CHAT_ID and restart. (Gmail/Calendar: add")
    print("credentials.json and run auth_google.py.)")


if __name__ == '__main__':
    main()

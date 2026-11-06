"""Keep secrets out of the logs.

The Telegram bot token is part of every Bot API URL, and requests puts the URL in its
exceptions, so ordinary network blips wrote the token to logs/telegram_bot.log ~200,000
times. install() wraps stdout and stderr so anything printed (tracebacks included) has
known secrets replaced. Call it after load_dotenv().
"""
import os
import sys

SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN")


class _Redacting:
    def __init__(self, stream, secrets):
        self._stream, self._secrets = stream, secrets

    def write(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return self._stream.write(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _secrets(names):
    return [v for v in (os.getenv(n) for n in names) if v and len(v) >= 12]


def install(names=SECRET_NAMES):
    secrets = _secrets(names)
    if not secrets or isinstance(sys.stdout, _Redacting):
        return
    sys.stdout = _Redacting(sys.stdout, secrets)
    sys.stderr = _Redacting(sys.stderr, secrets)

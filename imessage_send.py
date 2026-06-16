"""Outbound iMessage — send a message to a handle on macOS.

The iMessage analog of notify's Telegram send: drives Messages.app via AppleScript
(`osascript`) over the signed-in iMessage account. Text is passed as an argv argument
(not interpolated into the script source) so quotes, newlines, and emoji can't break or
inject into the AppleScript. Best-effort: never raises, returns True on success.

Requirements (one-time, see docs/imessage.md):
  - Messages.app signed into the iMessage account Aria speaks from (ideally a dedicated
    Apple ID, so messages from your personal number arrive as inbound).
  - Terminal/your Python host granted Automation control of Messages
    (System Settings → Privacy & Security → Automation) and Full Disk Access (for the
    inbound reader).
"""
import subprocess

_SEND_SCRIPT = r'''
on run argv
    set targetHandle to item 1 of argv
    set targetMessage to item 2 of argv
    tell application "Messages"
        set iMessageService to 1st account whose service type = iMessage
        try
            set targetBuddy to participant targetHandle of iMessageService
            send targetMessage to targetBuddy
        on error
            send targetMessage to participant targetHandle of iMessageService
        end try
    end tell
end run
'''

IMESSAGE_CHUNK = 8000  # chunk very long payloads, mirroring Telegram's split


def send_imessage(handle: str, text: str) -> bool:
    """Send `text` to `handle` (phone like +15551234567 or an email). Returns True if every
    chunk was delivered to osascript without error. Never raises."""
    if not handle:
        print("[imessage] no handle to send to")
        return False
    text = text or "(no response)"
    ok = True
    for i in range(0, len(text), IMESSAGE_CHUNK):
        chunk = text[i:i + IMESSAGE_CHUNK]
        try:
            result = subprocess.run(
                ["osascript", "-", handle, chunk],
                input=_SEND_SCRIPT, text=True, capture_output=True, timeout=30)
            if result.returncode != 0:
                print(f"[imessage] send to {handle} failed: {result.stderr.strip()}")
                ok = False
        except Exception as e:
            print(f"[imessage] send to {handle} errored: {e}")
            ok = False
    return ok


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("usage: python3 imessage_send.py <handle> <message>")
        raise SystemExit(2)
    print("sent" if send_imessage(sys.argv[1], sys.argv[2]) else "failed")

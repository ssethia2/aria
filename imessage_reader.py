"""Inbound iMessage — read new messages from the macOS Messages database.

Zero infra, the iMessage analog of Telegram long-polling: this polls
~/Library/Messages/chat.db (the SQLite store Messages.app writes) read-only, tracking the
highest message ROWID seen so each message is handled exactly once. No webhook, no port,
no third-party server.

Two wrinkles this handles:
  - On modern macOS `message.text` is often NULL and the real text lives in
    `attributedBody`, a serialized NSAttributedString. `decode_attributed_body` extracts
    the plain string from that typedstream blob.
  - The DB is live (WAL) and may be briefly locked by Messages; reads open read-only with
    a busy timeout and a poll that hits a lock just retries next cycle — ROWID tracking
    means nothing is dropped, only delayed.

Requires Full Disk Access for the process running this (System Settings → Privacy &
Security → Full Disk Access). Without it the open fails.
"""
import os
import sqlite3

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")


def _connect():
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA query_only = ON")
    return conn


def decode_attributed_body(blob) -> str:
    """Best-effort plain-text extraction from a Messages `attributedBody` typedstream. The
    text sits just after the 'NSString' class marker, length-prefixed: a single length byte
    for short strings, or 0x81 then a uint16 little-endian length for longer ones. Returns
    '' if the shape isn't recognized."""
    if not blob:
        return ""
    try:
        if isinstance(blob, str):
            blob = blob.encode("utf-8", errors="replace")
        marker = blob.split(b"NSString", 1)
        if len(marker) < 2:
            return ""
        payload = marker[1][5:]  # skip the \x01\x94\x84\x01+ class/version preamble
        if not payload:
            return ""
        if payload[0] == 0x81:
            length = int.from_bytes(payload[1:3], "little")
            raw = payload[3:3 + length]
        else:
            length = payload[0]
            raw = payload[1:1 + length]
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def latest_rowid() -> int:
    """Highest message ROWID currently in the DB — the starting point so a freshly launched
    bot doesn't replay history. Returns 0 on any failure."""
    try:
        conn = _connect()
        try:
            row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
            return row[0] or 0
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        print(f"[imessage] can't open chat.db (Full Disk Access?): {e}")
        return 0


def fetch_new(since_rowid: int):
    """Return inbound (is_from_me = 0) messages with ROWID > since_rowid, oldest first. Each
    item: {'rowid', 'handle', 'text', 'date'}; 'handle' is the sender's phone/email. Messages
    whose text can't be recovered are skipped. On a transient DB lock returns []."""
    try:
        conn = _connect()
    except sqlite3.OperationalError as e:
        print(f"[imessage] open failed: {e}")
        return []
    try:
        rows = conn.execute(
            """SELECT m.ROWID, m.text, m.attributedBody, h.id, m.date
                 FROM message m
                 LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.ROWID > ? AND m.is_from_me = 0
                ORDER BY m.ROWID ASC""",
            (since_rowid,)).fetchall()
    except sqlite3.OperationalError as e:
        print(f"[imessage] read deferred (locked): {e}")
        return []
    finally:
        conn.close()

    out = []
    for rowid, text, attributed, handle, date in rows:
        body = (text or "").strip() or decode_attributed_body(attributed)
        if not body:
            continue
        out.append({"rowid": rowid, "handle": handle or "", "text": body, "date": date})
    return out


if __name__ == "__main__":
    import time
    print(f"Watching {CHAT_DB} (Ctrl+C to stop)…")
    seen = latest_rowid()
    print(f"starting at ROWID {seen}")
    try:
        while True:
            for msg in fetch_new(seen):
                seen = msg["rowid"]
                print(f"[{msg['handle']}] {msg['text']!r}")
            time.sleep(2)
    except KeyboardInterrupt:
        pass

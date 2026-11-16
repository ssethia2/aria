"""What each background judge was shown, what it said, and what was done about it.

Aria's background side is a handful of single questions put to a model: is this email
worth flagging, is this commitment already handled, is a nudge worth sending. When one
got it wrong, the only trace was the message he received; what the judge had been shown
was gone, so every fix began by guessing. This keeps each question and answer for
KEEP_DAYS in `decisions.jsonl` (local, gitignored), so that:

  * "why did you do that?" is answered from the record (`explain`, her `explain_decision`
    tool), not from a guess;
  * a prompt or model change can be replayed against real past decisions before it ships
    (`python3 -m evals --replay <judge>`).

    python3 decisions.py                      # the latest decisions, one line each
    python3 decisions.py reconcile --last 5   # one judge
    python3 decisions.py --about acme --full  # everything shown and said about a subject

Each line is one of:
  {"at", "judge", "fields", "reply"}   a question (the prompt's fields) and the answer
  {"at", "judge", "note"}              what the code then did (closed #12, rejected a close)
"""
import json
import os
import sys
from datetime import datetime, timedelta

LOG_PATH = os.path.join(os.path.dirname(__file__), "decisions.jsonl")
KEEP_DAYS = 14
FIELD_LIMIT = 40_000        # characters kept per field; a runaway input can't bloat the log

_pruned_on = None


def _append(entry: dict):
    line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode()
    # One O_APPEND write per line: the engine thread and the briefing job both write here.
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def record(judge: str, fields: dict, reply: str, now: datetime = None):
    """Keep one judgment. Never raises: a full disk must not stop a judge from judging."""
    try:
        now = now or datetime.now()
        _prune(now)
        _append({"at": now.isoformat(timespec="seconds"), "judge": judge,
                 "fields": {k: (v if isinstance(v, str) else json.dumps(v, default=str))
                            [:FIELD_LIMIT] for k, v in fields.items()},
                 "reply": reply})
    except Exception as e:
        print(f"[decisions] couldn't record {judge}: {e}")


def note(judge: str, text: str, now: datetime = None):
    """What the code did with a verdict: the close it made, the one it refused."""
    print(f"[engine] {judge}: {text}")
    try:
        _append({"at": (now or datetime.now()).isoformat(timespec="seconds"),
                 "judge": judge, "note": text})
    except Exception as e:
        print(f"[decisions] couldn't note {judge}: {e}")


def _prune(now: datetime):
    """Once a day, drop what is older than KEEP_DAYS."""
    global _pruned_on
    if _pruned_on == now.date() or not os.path.exists(LOG_PATH):
        _pruned_on = now.date()
        return
    _pruned_on = now.date()
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    with open(LOG_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    kept = [ln for ln in lines if _at(ln) >= cutoff]
    if len(kept) < len(lines):
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, LOG_PATH)


def _at(line: str) -> str:
    try:
        return json.loads(line).get("at", "")
    except ValueError:
        return ""       # a torn line sorts oldest and is pruned


def read(judge: str = None, about: str = None, limit: int = None, questions_only=False) -> list:
    """Entries oldest first, optionally one judge's and/or those mentioning `about`."""
    if not os.path.exists(LOG_PATH):
        return []
    about = (about or "").strip().lower()
    out = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            if about and about not in line.lower():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if judge and entry.get("judge") != judge:
                continue
            if questions_only and "fields" not in entry:
                continue
            out.append(entry)
    return out[-limit:] if limit else out


def _when(entry: dict) -> str:
    try:
        return datetime.fromisoformat(entry["at"]).strftime('%a %b %-d, %-I:%M %p')
    except (KeyError, ValueError):
        return "?"


def _excerpt(fields: dict, about: str, width: int = 240, most: int = 6) -> list:
    """The lines of what a judge was shown that mention the subject."""
    hits = []
    for name, value in fields.items():
        for line in str(value).splitlines():
            if about in line.lower():
                hits.append(f"{name}: {' '.join(line.split())[:width]}")
                if len(hits) >= most:
                    return hits
    return hits


def explain(about: str, limit: int = 6) -> str:
    """What the background judges were shown and decided about a subject, newest first.
    Written for the chat agent: enough to say truthfully why something happened."""
    about = (about or "").strip().lower()
    if not about:
        return "Say what it's about (a name, a company, a subject)."
    entries = read(about=about)
    if not entries:
        return (f"Nothing in the last {KEEP_DAYS} days of background decisions mentions "
                f"'{about}'. Either no judge saw it, or it happened in chat (not recorded "
                f"here). Say you can't see why rather than guessing.")
    blocks = []
    for e in reversed(entries[-limit:]):
        head = f"{_when(e)} · {e.get('judge', '?')}"
        if "note" in e:
            blocks.append(f"{head}\n  did: {e['note']}")
            continue
        shown = _excerpt(e.get("fields", {}), about)
        reply = " ".join(str(e.get("reply", "")).split())
        if about in reply.lower() or len(reply) <= 700:
            said = reply[:700]
        else:
            said = "(its answer doesn't mention this, so it chose not to act on it)"
        blocks.append(head + "".join(f"\n  saw {s}" for s in shown) + f"\n  said: {said}")
    more = len(entries) - limit
    return ("Background decisions mentioning this, newest first:\n\n" + "\n\n".join(blocks)
            + (f"\n\n({more} older not shown.)" if more > 0 else ""))


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Read Aria's background decision record.")
    p.add_argument("judge", nargs="?", help="one judge (email-screen, reconcile, chase, ...)")
    p.add_argument("--about", help="only entries mentioning this")
    p.add_argument("--last", type=int, default=20)
    p.add_argument("--full", action="store_true", help="print the fields and reply in full")
    a = p.parse_args(argv)
    entries = read(judge=a.judge, about=a.about, limit=a.last)
    if not entries:
        print("(nothing recorded)")
        return 0
    for e in entries:
        if "note" in e:
            print(f"{_when(e)}  {e['judge']:<18} did: {e['note']}")
        elif a.full:
            print(f"\n=== {_when(e)}  {e['judge']} ===")
            for name, value in e.get("fields", {}).items():
                print(f"--- {name}\n{value}")
            print(f"--- reply\n{e.get('reply', '')}")
        else:
            print(f"{_when(e)}  {e['judge']:<18} said: {' '.join(str(e.get('reply', '')).split())[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

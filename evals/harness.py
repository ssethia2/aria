"""Aria evals — a behavioral regression suite for the REAL agent.

Unit tests (tests/) prove the code does what it says, with the LLM mocked out. Evals answer
the other question: does Aria actually BEHAVE right? Pick the right tool, get the date
right, ask instead of guessing, own a mistake without being chipper. Each scenario is a
short conversation run through the production agent (subscription or API brain, the same
quick-path-then-agent route Telegram takes) inside a Sandbox (evals/sandbox.py), then
checked with deterministic assertions and, where judgment is the point, an LLM judge.

    python3 -m evals                    # everything, brain from .env, 1 trial
    python3 -m evals --only calendar    # id/area substring filter
    python3 -m evals --trials 3         # flakiness: pass rate per scenario
    python3 -m evals --brain api        # exercise the API brain (bills the API)
    python3 -m evals --list

Scenarios: evals/scenarios/*.yaml (format in evals/README.md). Results: evals/results/
(gitignored) — every run is diffed against the previous one to flag regressions.
"""
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENARIO_DIR = os.path.join(HERE, "scenarios")
RESULTS_DIR = os.path.join(HERE, "results")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---- date templates ------------------------------------------------------------------
# Scenarios are written relative to the day they run, so they never rot:
#   {{today+3}} -> 2026-10-18     {{weekday:today+3}} -> Sunday   {{wd:today+1}} -> Fri
#   {{longdate:today+7}} -> Thursday, October 22   {{monthday:today+2}} -> October 17
#   {{annual:03-14}} -> the NEXT March 14 (this year's, or next year's if it has passed)

_TPL = re.compile(r"\{\{\s*(?:(?P<fmt>iso|weekday|wd|longdate|monthday):)?"
                  r"(?:today(?P<off>[+-]\d+)?|annual:(?P<md>\d{2}-\d{2}))\s*\}\}")


def _annual(md: str, today: date) -> date:
    m, d = (int(x) for x in md.split("-"))
    this_year = date(today.year, m, d)
    return this_year if this_year >= today else date(today.year + 1, m, d)


def _fmt(d: date, fmt: str) -> str:
    if fmt == "weekday":
        return d.strftime("%A")
    if fmt == "wd":
        return d.strftime("%a")
    if fmt == "longdate":
        return f"{d.strftime('%A, %B')} {d.day}"
    if fmt == "monthday":
        return f"{d.strftime('%B')} {d.day}"
    return d.isoformat()


def resolve(obj, today: date):
    """Expand date templates in every string of a (nested) scenario structure."""
    if isinstance(obj, str):
        def sub(m):
            d = _annual(m.group("md"), today) if m.group("md") \
                else today + timedelta(days=int(m.group("off") or 0))
            return _fmt(d, m.group("fmt") or "iso")
        return _TPL.sub(sub, obj)
    if isinstance(obj, list):
        return [resolve(x, today) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve(v, today) for k, v in obj.items()}
    return obj


def date_mentions(iso: str, today: date) -> list:
    """Regexes for the ways a reply can legitimately name a date."""
    d = date.fromisoformat(iso)
    pats = [re.escape(iso),
            rf"\b{d.strftime('%B')}\s+{d.day}(st|nd|rd|th)?\b",
            rf"\b{d.strftime('%b')}\.?\s+{d.day}(st|nd|rd|th)?\b",
            rf"\b{d.day}(st|nd|rd|th)?\s+(of\s+)?{d.strftime('%B')}\b",
            rf"\b{d.month}/{d.day}\b"]
    delta = (d - today).days
    if 0 <= delta <= 6:
        pats.append(rf"\b{d.strftime('%A')}\b")
        pats.append(rf"\b{d.strftime('%a')}\b")
    if delta == 0:
        pats.append(r"\btoday\b")
    if delta == 1:
        pats.append(r"\btomorrow\b")
    if delta > 1:
        pats.append(rf"\bin {delta} days\b")
    return pats


# ---- assertions ----------------------------------------------------------------------

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap])?\.?\s*m?\.?\s*$", re.I)


def norm_time(v):
    """'7:30pm' / '19:30' / '19:30:00' / '5 PM' -> 'HH:MM'; anything else unchanged."""
    if v in (None, ""):
        return None
    m = _TIME.match(str(v))
    if not m:
        return str(v).strip().lower()
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}"


def match_value(actual, expected) -> bool:
    """One expected value against one actual value. Matchers:
    scalar (case-insensitive equality) | null (unset) | {contains: x} | {one_of: [...]}
    | {time: 'HH:MM'} (time-normalized)."""
    if isinstance(expected, dict):
        if "contains" in expected:
            return str(expected["contains"]).lower() in str(actual or "").lower()
        if "one_of" in expected:
            return any(match_value(actual, e) for e in expected["one_of"])
        if "time" in expected:
            return norm_time(actual) == norm_time(expected["time"])
        raise ValueError(f"unknown matcher {expected}")
    if expected is None:
        return actual in (None, "")
    return str(actual).strip().lower() == str(expected).strip().lower()


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if v not in (None, ""))


def check_turn(expect: dict, reply: str, path: str, calls: list, db: list, rules: list,
               today: date) -> list:
    """Deterministic checks for one turn. Returns failure messages (empty = pass)."""
    fails = []
    names = [c["tool"] for c in calls]

    for t in expect.get("called", []) or []:
        if t not in names:
            fails.append(f"expected {t} to be called (called: {', '.join(names) or 'nothing'})")
    for t in expect.get("not_called", []) or []:
        if t in names:
            args = [_fmt_args(c["args"]) for c in calls if c["tool"] == t]
            fails.append(f"{t} was called but must not be ({'; '.join(args)})")
    any_of = expect.get("called_any") or []
    if any_of and not any(t in names for t in any_of):
        fails.append(f"expected one of {any_of} to be called "
                     f"(called: {', '.join(names) or 'nothing'})")
    for t, want in (expect.get("called_with") or {}).items():
        mine = [c["args"] for c in calls if c["tool"] == t]
        if not mine:
            fails.append(f"expected {t}({_fmt_args(want)}) but {t} wasn't called")
        elif not any(all(match_value(a.get(k), v) for k, v in want.items()) for a in mine):
            fails.append(f"{t} called with ({'; '.join(_fmt_args(a) for a in mine)}) — "
                         f"wanted {_fmt_args(want)}")

    for pat in expect.get("reply_matches", []) or []:
        if not re.search(pat, reply or "", re.I):
            fails.append(f"reply doesn't match /{pat}/")
    for pat in expect.get("reply_not_matches", []) or []:
        m = re.search(pat, reply or "", re.I)
        if m:
            fails.append(f"reply contains forbidden /{pat}/ ('{m.group(0)}')")
    if expect.get("mentions_date"):
        iso = expect["mentions_date"]
        if not any(re.search(p, reply or "", re.I) for p in date_mentions(iso, today)):
            fails.append(f"reply doesn't mention {iso}")
    if expect.get("path") and expect["path"] != path:
        fails.append(f"answered by the {path} path, expected {expect['path']}")

    dbx = expect.get("db") or {}
    by_status = lambda s: [r for r in db if r["status"] == s]  # noqa: E731
    for key, status in (("open_count", "open"), ("done_count", "done"),
                        ("dropped_count", "dropped")):
        if key in dbx and len(by_status(status)) != dbx[key]:
            fails.append(f"{len(by_status(status))} {status} commitments, expected {dbx[key]}")
    for item in dbx.get("items", []) or []:
        rows = [r for r in db if item["match"].lower() in (r["description"] or "").lower()]
        if item.get("absent"):
            if rows:
                fails.append(f"commitment matching '{item['match']}' exists but shouldn't")
            continue
        if not rows:
            if not item.get("optional"):
                fails.append(f"no commitment matching '{item['match']}'")
            continue
        fields = {k: v for k, v in item.items() if k not in ("match", "optional", "absent")}

        def row_ok(r):
            for k, v in fields.items():
                actual = r.get(k)
                if k == "due_time":
                    if norm_time(actual) != norm_time(v):
                        return False
                elif k == "who" and v is not None:
                    if str(v).lower() not in str(actual or "").lower():
                        return False
                elif not match_value(actual, v):
                    return False
            return True

        if not any(row_ok(r) for r in rows):
            got = "; ".join(", ".join(f"{k}={r.get(k)}" for k in fields) for r in rows)
            fails.append(f"'{item['match']}' is [{got}], expected "
                         f"[{', '.join(f'{k}={v}' for k, v in fields.items())}]")
    if "rules_count" in expect and len(rules) != expect["rules_count"]:
        fails.append(f"{len(rules)} standing rules, expected {expect['rules_count']}")
    return fails


# ---- LLM judge -----------------------------------------------------------------------

JUDGE_PROMPT = """You are grading ONE reply from Aria, a personal AI assistant, in a regression test.
Judge ONLY against the rubric. Be strict about what the rubric says and fair about wording: a
reply that meets the rubric's intent passes even if you'd have phrased it differently.

Why this scenario exists: {why}

Conversation before this turn:
{history}

The user's message this turn:
{user}

Tools Aria called this turn:
{calls}

Commitment store after the turn:
{db}

Aria's reply:
\"\"\"{reply}\"\"\"

Rubric. PASS only if ALL of it holds:
{rubric}

Respond with ONLY a JSON object: {{"pass": true or false, "reason": "<one short sentence>"}}"""


def run_judge(rubric, why, history, user, calls, db, reply, tier="standard"):
    from llm_router import get_llm
    from agent_core import extract_text
    prompt = JUDGE_PROMPT.format(
        why=why or "(none)",
        history="\n".join(f"User: {u}\nAria: {a}" for u, a in history) or "(none)",
        user=user,
        calls="\n".join(f"- {c['tool']}({_fmt_args(c['args'])})" for c in calls) or "(none)",
        db="\n".join(f"- #{r['id']} {r['description']} [{r['status']}] due={r['due_date']} "
                     f"{r['due_time'] or ''}".rstrip() for r in db) or "(empty)",
        reply=reply, rubric=rubric)
    try:
        out = extract_text(get_llm(temperature=0, tier=tier).invoke(prompt).content)
        m = re.search(r"\{.*\}", out, re.S)
        verdict = json.loads(m.group(0)) if m else {}
        return bool(verdict.get("pass")), str(verdict.get("reason") or out[:200])
    except Exception as e:
        return False, f"judge unavailable ({type(e).__name__}: {str(e)[:120]})"


# ---- running scenarios ---------------------------------------------------------------

def load_scenarios(directory=SCENARIO_DIR) -> list:
    out = []
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        area = os.path.splitext(fn)[0]
        with open(os.path.join(directory, fn)) as f:
            for sc in yaml.safe_load(f) or []:
                sc.setdefault("area", area)
                out.append(sc)
    return out


def _seed(sb, setup: dict):
    from skills import commitment_manager as cm
    for c in setup.get("commitments", []) or []:
        cid = cm.add(c["description"], kind=c.get("kind", "promise"), who=c.get("who"),
                     due_date=c.get("due_date"), due_time=c.get("due_time"),
                     recurring=c.get("recurrence"), source="eval")
        status = c.get("status", "open")
        if status == "done":
            cm.complete(cid)
        elif status == "dropped":
            cm.drop(cid)
        if c.get("snoozed_until"):
            cm.snooze(cid, c["snoozed_until"])
    if setup.get("rules"):
        import instructions
        items = instructions._load()
        for text in setup["rules"]:
            items.append({"id": max([i["id"] for i in items] or [0]) + 1,
                          "instruction": text, "updated": date.today().isoformat()})
        instructions._save(items)


def _compose(turn: dict) -> str:
    if "photo" in turn:
        from telegram_bot import compose_photo_message
        return compose_photo_message(turn["photo"], turn.get("user", ""))
    return turn["user"]


def _respond(agent, thread_id: str, text: str, use_quick: bool):
    """One turn, routed exactly like the Telegram bot: cheap fast path first, then the
    full agent. Returns (reply, path)."""
    from langchain_core.messages import HumanMessage
    from agent_core import quick_answer, thread_config, extract_text
    if use_quick:
        ans = quick_answer(agent, thread_id, text)
        if ans is not None:
            return ans, "quick"
    out = agent.invoke({"messages": [HumanMessage(content=text)]},
                       config=thread_config(thread_id))
    return extract_text(out["messages"][-1].content), "agent"


def run_scenario(sc: dict, brain: str, trial: int, today: date, use_quick=True,
                 judge_tier="standard", live_context=False) -> dict:
    from evals.sandbox import Sandbox
    sc = resolve(sc, today)
    thread_id = f"eval-{sc['id']}-t{trial}"
    turns_out, history, log = [], [], io.StringIO()
    actual_brain = "?"
    with Sandbox(overrides=sc.get("tools"), live_context=live_context) as sb:
        _seed(sb, sc.get("setup") or {})
        with contextlib.redirect_stdout(log):
            agent = sb.build_agent(brain)
        actual_brain = "subscription" if type(agent).__name__ == "SubscriptionBrain" else "api"
        for i, turn in enumerate(sc["turns"]):
            sb.turn = i
            n_fallbacks = len(sb.fallbacks)
            text = _compose(turn)
            shown = f"[photo] {turn.get('user', '')}".strip() if "photo" in turn else text
            quick = use_quick and turn.get("quick", True) and "photo" not in turn
            t0, err = time.time(), None
            try:
                with contextlib.redirect_stdout(log):
                    reply, path = _respond(agent, thread_id, text, quick)
            except Exception as e:
                reply, path, err = "", "error", f"{type(e).__name__}: {str(e)[:300]}"
            seconds = round(time.time() - t0, 1)
            calls = [c for c in sb.calls if c["turn"] == i]
            db = sb.commitments()
            served = "quick" if path == "quick" else (
                "api-fallback" if len(sb.fallbacks) > n_fallbacks else actual_brain)
            expect = turn.get("expect") or {}
            fails = ([f"agent raised {err}"] if err else
                     check_turn(expect, reply, path, calls, db, sb.standing_rules(), today))
            judged = None
            if expect.get("judge") and not err:
                ok, reason = run_judge(expect["judge"], sc.get("why"), history, shown,
                                       calls, db, reply, tier=judge_tier)
                judged = {"pass": ok, "reason": reason}
                if not ok:
                    fails.append(f"judge: {reason}")
            turns_out.append({"user": shown, "reply": reply, "path": path, "served_by": served,
                              "calls": calls, "failures": fails, "judge": judged,
                              "seconds": seconds,
                              "fallback": sb.fallbacks[n_fallbacks:] or None})
            history.append((shown, reply))
            if err:
                break
    return {"passed": all(not t["failures"] for t in turns_out), "turns": turns_out,
            "brain": actual_brain, "log": log.getvalue()[-4000:]}


# ---- reporting -----------------------------------------------------------------------

def _total_spend() -> float:
    try:
        import cost_tracker
        return sum(m.get("usd", 0) for day in cost_tracker._load().values()
                   for m in day.values())
    except Exception:
        return 0.0


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "?"


def _previous_run(brain: str):
    if not os.path.isdir(RESULTS_DIR):
        return None
    for fn in sorted(os.listdir(RESULTS_DIR), reverse=True):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(RESULTS_DIR, fn)) as f:
                    prev = json.load(f)
                if prev.get("brain") == brain:
                    return prev
            except Exception:
                continue
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m evals", description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="substring filter on scenario id or area")
    ap.add_argument("--brain", choices=["env", "subscription", "api"], default="env",
                    help="which brain to test (default: whatever ARIA_BRAIN in .env says)")
    ap.add_argument("--trials", type=int, default=1, help="runs per scenario (flakiness)")
    ap.add_argument("--judge-tier", default="standard", choices=["light", "standard", "heavy"])
    ap.add_argument("--no-quick", action="store_true", help="skip the fast path")
    ap.add_argument("--live-context", action="store_true",
                    help="use copies of YOUR real profile/rules/people instead of fixtures")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)   # live progress even when piped
    except Exception:
        pass

    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))

    scenarios = load_scenarios()
    if args.only:
        q = args.only.lower()
        scenarios = [s for s in scenarios if q in s["id"].lower() or q in s["area"].lower()]
    if args.list:
        for s in scenarios:
            print(f"{s['area']}/{s['id']}: {s.get('why', '')}")
        return 0
    if not scenarios:
        print("No scenarios matched.")
        return 1

    today = date.today()
    wanted = args.brain if args.brain != "env" else (
        "subscription" if os.getenv("ARIA_BRAIN", "").lower() == "subscription" else "api")
    print(f"Aria evals: {len(scenarios)} scenario(s) × {args.trials} trial(s), "
          f"brain={wanted}, {today.isoformat()}\n")

    spend0, t_start = _total_spend(), time.time()
    results, brains_seen, fallback_turns = {}, set(), 0
    for sc in scenarios:
        runs = []
        for trial in range(args.trials):
            r = run_scenario(sc, args.brain, trial, today, use_quick=not args.no_quick,
                             judge_tier=args.judge_tier, live_context=args.live_context)
            runs.append(r)
            brains_seen.add(r["brain"])
            fallback_turns += sum(1 for t in r["turns"] if t["served_by"] == "api-fallback")
        passed = sum(r["passed"] for r in runs)
        secs = sum(t["seconds"] for r in runs for t in r["turns"])
        mark = "✅" if passed == len(runs) else ("⚠️ " if passed else "❌")
        print(f"{mark} {sc['area']}/{sc['id']}  {passed}/{len(runs)}  ({secs:.0f}s)")
        for r in runs:
            if r["passed"]:
                continue
            for i, t in enumerate(r["turns"]):
                for f in t["failures"]:
                    print(f"     turn {i + 1}: {f}")
            last = r["turns"][-1] if r["turns"] else None
            if last:
                print(f"     reply: {' '.join((last['reply'] or '').split())[:220]}")
            break  # one failing trial's detail is enough on the console
        results[sc["id"]] = {"area": sc["area"], "why": sc.get("why"), "passed": passed,
                             "trials": len(runs), "runs": runs}

    total = len(results)
    full = sum(1 for r in results.values() if r["passed"] == r["trials"])
    spend = _total_spend() - spend0
    elapsed = time.time() - t_start
    brain = "/".join(sorted(brains_seen))

    prev = _previous_run(brain)
    regressions, fixes = [], []
    if prev:
        for sid, r in results.items():
            p = prev.get("scenarios", {}).get(sid)
            if not p:
                continue
            was, now = p["passed"] == p["trials"], r["passed"] == r["trials"]
            if was and not now:
                regressions.append(sid)
            if now and not was:
                fixes.append(sid)

    print(f"\n{full}/{total} scenarios passed ({100 * full // max(total, 1)}%) · "
          f"brain={brain} · API spend ≈ ${spend:.2f} · {elapsed / 60:.1f} min")
    if wanted == "subscription" and "api" in brains_seen:
        print("⚠️  Asked for the subscription brain but got the API brain — is Claude Code "
              "logged in? (run /login)")
    if fallback_turns:
        print(f"⚠️  {fallback_turns} turn(s) fell back to the API brain mid-run "
              "(subscription limit or login) — those results describe the API brain.")
    if prev:
        print(f"vs last run ({prev.get('started', '?')[:16]}, {prev.get('git', '?')}): "
              f"regressions: {', '.join(regressions) or 'none'} · "
              f"fixed: {', '.join(fixes) or 'none'}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{stamp}.json")
    with open(path, "w") as f:
        json.dump({"started": datetime.fromtimestamp(t_start).isoformat(), "git": _git_sha(),
                   "brain": brain, "trials": args.trials, "today": today.isoformat(),
                   "api_spend_usd": round(spend, 4), "seconds": round(elapsed),
                   "passed": full, "total": total, "regressions": regressions,
                   "fixes": fixes, "scenarios": results}, f, indent=2, default=str)
    print(f"Results: {os.path.relpath(path, ROOT)}")
    return 0 if full == total else 1

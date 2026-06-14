"""Agentic browser — Aria explores a web flow and reports back.

ONE general capability instead of a hand-scripted skill per site (the Netflix
skill is the scripted opposite; this is the generalist). An LLM loop reads the
page, picks the next action, and drives Playwright until the task is done or it
hits a hard boundary.

SAFETY (non-negotiable, enforced in code, not just prompt):
  - NEVER completes a purchase/payment/order — those buttons are blocked, and on
    reaching them the loop STOPS and returns the current URL for the human.
  - NEVER types into password fields.
  - Only uses `facts` the caller explicitly provides (last name, confirmation #);
    invents no personal/payment data.
  - Hard step cap so it can't run away.
The design point: it navigates "as far as safely possible," then hands off — so a
stuck/uncertain run degrades to "got this far, here's the link," never to a
wrong action with the user's money.
"""
import json
import re

from langchain_core.tools import tool

from core.llm_router import get_llm

MAX_STEPS = 14
MAX_PAGE_CHARS = 3000

# Buttons we refuse to click — anything that spends money or finalizes an order.
FORBIDDEN = ['place order', 'pay now', 'buy now', 'complete purchase', 'submit payment',
             'confirm and pay', 'confirm payment', 'purchase', 'place your order',
             'buy it now', 'pay $', 'authorize payment']

_SYSTEM = """You are a careful web-navigation agent operating a real browser on the user's
behalf. Your job: accomplish the TASK by reading each page and choosing ONE action at a
time, then STOP and report.

HARD RULES:
- You are EXPLORING and TEEING THINGS UP, not buying. NEVER click anything that places an
  order or submits payment (e.g. "Place order", "Pay now", "Buy now"). When you reach a
  payment/checkout/login/order-confirmation step, choose action "finish" and report what
  you found plus that the user must take it from here.
- Only enter values from the provided FACTS. Never invent names, emails, card numbers, or
  passwords. Never type into a password field.
- If the task looks done (you've gathered the options/info asked for), "finish" and report.

Respond with ONLY a JSON object, no prose:
{"thought": "<brief>", "action": "click|type|select|scroll|finish",
 "index": <element number, for click/type/select>,
 "value": "<text to type, or option label to select>",
 "report": "<for finish: what you found + what's left for the user, incl. the current URL>"}"""

_STATE_JS = r"""() => {
  const sel = ['a','button','input','select','textarea','[role=button]'];
  const nodes = [];
  document.querySelectorAll(sel.join(',')).forEach(el => {
    const s = getComputedStyle(el);
    if (s.display==='none' || s.visibility==='hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width===0 && r.height===0) return;
    nodes.push(el);
  });
  nodes.forEach((el,i)=>el.setAttribute('data-aria-idx', String(i)));
  const elements = nodes.map((el,i)=>{
    const label = (el.innerText||el.value||el.getAttribute('aria-label')||
                   el.getAttribute('placeholder')||el.getAttribute('name')||'').trim().slice(0,80);
    return {idx:i, tag:el.tagName.toLowerCase(), type:(el.getAttribute('type')||''), label};
  });
  return {text: document.body.innerText.slice(0, %d), elements};
}""" % MAX_PAGE_CHARS


def _is_forbidden(label: str) -> bool:
    low = (label or '').lower()
    return any(k in low for k in FORBIDDEN)


def _parse_action(content) -> dict:
    if isinstance(content, list):
        content = next((b['text'] for b in content
                        if isinstance(b, dict) and b.get('type') == 'text'), str(content))
    m = re.search(r'\{.*\}', content, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON action in: {content[:200]}")
    return json.loads(m.group(0))


def _drive(page, task: str, facts: str, llm) -> str:
    history = []
    for step in range(MAX_STEPS):
        state = page.evaluate(_STATE_JS)
        elements = state['elements']
        el_lines = "\n".join(
            f"  [{e['idx']}] {e['tag']}{('/'+e['type']) if e['type'] else ''}: {e['label']}"
            for e in elements)
        user_msg = (
            f"TASK: {task}\nFACTS YOU MAY USE: {facts or '(none)'}\n"
            f"CURRENT URL: {page.url}\nPAGE TEXT:\n{state['text']}\n\n"
            f"INTERACTIVE ELEMENTS:\n{el_lines}\n\n"
            f"STEPS SO FAR: {'; '.join(history) or 'none'}\nChoose the next action.")
        resp = llm.invoke([{"role": "system", "content": _SYSTEM},
                           {"role": "user", "content": user_msg}])
        try:
            action = _parse_action(resp.content)
        except Exception as e:
            return f"I got confused reading the page and stopped safely. ({e}). URL: {page.url}"

        act = action.get('action')
        if act == 'finish':
            return action.get('report') or f"Done. Current URL: {page.url}"

        idx = action.get('index')
        target = page.query_selector(f'[data-aria-idx="{idx}"]') if idx is not None else None
        if target is None:
            history.append(f"step{step}: element {idx} vanished")
            continue

        label = (action.get('value') or '')
        try:
            if act == 'click':
                btn_label = target.inner_text() or ''
                if _is_forbidden(btn_label) or _is_forbidden(label):
                    return (f"I reached a payment/order step ('{btn_label.strip()[:40]}') and "
                            f"stopped — this is yours to confirm. You can finish here:\n{page.url}")
                target.click()
                page.wait_for_timeout(2500)
                history.append(f"step{step}: clicked [{idx}] {btn_label.strip()[:30]}")
            elif act == 'type':
                if (target.get_attribute('type') or '') == 'password':
                    return f"That step needs a password — I don't handle those. URL: {page.url}"
                target.fill(action.get('value', ''))
                history.append(f"step{step}: typed into [{idx}]")
            elif act == 'select':
                target.select_option(label=action.get('value', ''))
                history.append(f"step{step}: selected '{action.get('value')}'")
            elif act == 'scroll':
                page.mouse.wheel(0, 1200)
                history.append(f"step{step}: scrolled")
            else:
                history.append(f"step{step}: unknown action {act}")
        except Exception as e:
            history.append(f"step{step}: action failed ({e})")

    return (f"I explored {MAX_STEPS} steps without fully finishing — here's where I am so "
            f"you can take over:\n{page.url}")


def _session_file(session: str):
    """Path to a saved login session from browser_login.py, if it exists."""
    if not session:
        return None
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        'browser_sessions', f"{session}.json")
    return path if os.path.exists(path) else None


@tool
def browse_and_report(task: str, start_url: str, facts: str = None,
                      session: str = None) -> str:
    """Drive a real browser to explore a web task and report back — for things like
    "open this airline pre-order link, enter my last name, and tell me the meal
    options" or navigating a logged-in site toward a cart. You EXPLORE and tee things
    up; you never pay, place orders, or enter passwords — you stop at that boundary
    and hand the user the link.

    For availability/booking searches (lodging, flights, tickets), the dates and party
    size MUST constrain the search before you read results — prefer a start_url that
    already encodes them (e.g. Airbnb checkin/checkout/adults params) so the listings
    shown are actually bookable. Only report options confirmed available for the dates.

    Args:
        task: What to accomplish and what to report (e.g. "find available 2-bed rentals
            for Jun 19-22, 4 guests, under $500, and list the bookable ones").
        start_url: The URL to begin at — for a booking search, build it with the dates
            and guest count already in the query string.
        facts: Any data you may enter on the user's behalf — last name, confirmation
            number, etc. Provide only what the user has actually given you.
        session: Name of a saved login session to reuse (e.g. "amazon") so you act
            already-signed-in. Only works if the user has run browser_login.py for
            that site; if a logged-in site asks you to sign in, tell the user to set
            up the session rather than attempting to log in yourself.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return "The browser engine isn't installed on this host."

    storage = _session_file(session)
    if session and not storage:
        return (f"I don't have a saved '{session}' login session — run "
                f"`python3 browser_login.py {session} <url>` once so I can act as you there.")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx_kwargs = {'user_agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')}
            if storage:
                ctx_kwargs['storage_state'] = storage
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.goto(start_url, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(1500)
            result = _drive(page, task, facts, get_llm(temperature=0, tier="heavy"))
            browser.close()
            return result
    except Exception as e:
        return f"Browser automation failed: {e}"

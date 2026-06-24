"""LLM cost tracking — estimate what Aria spends, by day and model.

Every model from llm_router.get_llm() carries a CostTrackingHandler, so each LLM call's
token usage is captured at one seam and recorded to cost_log.json (daily → model). Costs
are ESTIMATES from a static price table (a "what am I spending" gauge, not an invoice) —
the free Gemini tier records $0, so you can see the engine's move off paid models pay off.

View it:  python3 cost_tracker.py [days]   or the owner-only get_costs agent tool.
"""
import os
import json
import threading
from datetime import date, timedelta

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import tool

LOG_PATH = os.path.join(os.path.dirname(__file__), "cost_log.json")
_lock = threading.Lock()
KEEP_DAYS = 120          # prune older buckets so the file can't grow unbounded

# $ per 1M tokens (input, output). Gemini runs on the free tier → $0 (the whole point of
# routing the engine there). Tune via env if your prices change. See the claude-api skill.
PRICES = {
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
    "gemini-2.5-flash":  (0.0, 0.0),
}
# Anthropic cache economics relative to the input rate: reads ~0.1x; writes 2x at the 1h TTL
# Aria uses (ARIA_CACHE_TTL=1h). Used only when usage carries cache token details.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 2.0


def _rates(model: str):
    """(input, output) $/token for a model id, matched by prefix; (0,0) if unknown."""
    for key, (pin, pout) in PRICES.items():
        if model and (model.startswith(key) or key in model):
            return pin / 1_000_000, pout / 1_000_000
    return 0.0, 0.0


def estimate_cost(model, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    """Estimated USD for one call. LangChain's usage_metadata.input_tokens is the TOTAL prompt
    (cached + uncached), with cache_read/cache_creation reported separately in
    input_token_details — so subtract them to get the full-price uncached portion, then bill
    cache reads at 0.1x and writes at the 1h-TTL 2x. (Charging input_tokens in full AND the
    cache tokens again over-counts cached turns ~8x.)"""
    pin, pout = _rates(model)
    uncached = max(input_tokens - cache_read - cache_creation, 0)
    return round(uncached * pin
                 + cache_read * pin * CACHE_READ_FACTOR
                 + cache_creation * pin * CACHE_WRITE_FACTOR
                 + output_tokens * pout, 6)


def _load() -> dict:
    try:
        return json.loads(open(LOG_PATH).read())
    except Exception:
        return {}


def record(model, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    """Add one call's usage + estimated cost to today's bucket for `model`. Never raises —
    cost tracking must not be able to break a real LLM call."""
    try:
        usd = estimate_cost(model, input_tokens, output_tokens, cache_read, cache_creation)
        today = date.today().isoformat()
        with _lock:
            data = _load()
            # prune anything older than KEEP_DAYS
            cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
            data = {d: v for d, v in data.items() if d >= cutoff}
            day = data.setdefault(today, {})
            m = day.setdefault(model or "unknown",
                               {"calls": 0, "input": 0, "output": 0,
                                "cache_read": 0, "cache_creation": 0, "usd": 0.0})
            m["calls"] += 1
            m["input"] += input_tokens
            m["output"] += output_tokens
            m["cache_read"] += cache_read
            m["cache_creation"] += cache_creation
            m["usd"] = round(m["usd"] + usd, 6)
            with open(LOG_PATH, "w") as f:
                json.dump(data, f)
    except Exception as e:
        print(f"[cost] record failed (non-fatal): {e}")


class CostTrackingHandler(BaseCallbackHandler):
    """Attached to every get_llm() chain; on each completed LLM call it pulls token usage
    off the response and records the estimated cost. Defensive throughout — a usage-shape
    change degrades to a skipped record, never an exception in the request path."""

    def on_llm_end(self, response, **kwargs):
        try:
            gen = response.generations[0][0]
            msg = getattr(gen, "message", None)
            usage = getattr(msg, "usage_metadata", None) or {}
            if not usage:
                return
            model = (getattr(msg, "response_metadata", {}) or {}).get("model") \
                or (getattr(msg, "response_metadata", {}) or {}).get("model_name") \
                or (response.llm_output or {}).get("model_name", "unknown")
            details = usage.get("input_token_details") or {}
            record(model,
                   input_tokens=usage.get("input_tokens", 0),
                   output_tokens=usage.get("output_tokens", 0),
                   cache_read=details.get("cache_read", 0),
                   cache_creation=details.get("cache_creation", 0))
        except Exception as e:
            print(f"[cost] usage capture skipped: {e}")


def summary(days: int = 7) -> dict:
    """Aggregate the last `days` days: per-model totals + grand total USD."""
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    data = _load()
    per_model, total = {}, 0.0
    for d, models in data.items():
        if d < cutoff:
            continue
        for model, m in models.items():
            agg = per_model.setdefault(model, {"calls": 0, "input": 0, "output": 0,
                                               "cache_read": 0, "cache_creation": 0, "usd": 0.0})
            agg["calls"] += m.get("calls", 0)
            agg["input"] += m.get("input", 0)
            agg["output"] += m.get("output", 0)
            agg["cache_read"] += m.get("cache_read", 0)
            agg["cache_creation"] += m.get("cache_creation", 0)
            agg["usd"] = round(agg["usd"] + m.get("usd", 0.0), 6)
            total = round(total + m.get("usd", 0.0), 6)
    return {"days": days, "total_usd": total, "by_model": per_model}


def format_summary(days: int = 7) -> str:
    s = summary(days)
    if not s["by_model"]:
        return f"No LLM usage recorded in the last {days} day(s)."
    lines = [f"💸 Estimated LLM spend — last {s['days']} day(s): ${s['total_usd']:.2f}"]
    for model, m in sorted(s["by_model"].items(), key=lambda kv: -kv[1]["usd"]):
        lines.append(f"  • {model}: ${m['usd']:.2f}  "
                     f"({m['calls']} calls, {m['input']:,} in / {m['output']:,} out)")
    return "\n".join(lines)


@tool
def get_costs(days: int = 7) -> str:
    """Show estimated LLM API spend (what Aria is costing) over the last N days, broken down
    by model. Use when the user asks about cost, spend, usage, or their bill. days=7 default;
    30 for a monthly view. Estimates from list prices — the free Gemini tier shows as $0."""
    return format_summary(days)


if __name__ == "__main__":
    import sys
    print(format_summary(int(sys.argv[1]) if len(sys.argv) > 1 else 30))

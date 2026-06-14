# Contributing to Aria

Contributions are welcome. This guide covers the conventions that keep Aria modular. For architecture and rationale see [`context/implementation_plan.md`](context/implementation_plan.md) and [`docs/adr/`](docs/adr/); for setup see the [README](README.md).

## Licensing & the CLA (read first)

Aria is **dual-licensed**: open source under the [GNU AGPL-3.0](LICENSE) for the community, and available under separate commercial terms for users for whom the AGPL is unsuitable. To keep that dual-licensing path clean, **every contributor must agree to the [Contributor License Agreement (`CLA.md`)](CLA.md)** before their contribution can be merged.

The CLA does two things: you keep ownership of your contribution (you grant a non-exclusive license, you don't assign copyright), and you grant the maintainer the right to offer your contribution under **both** the AGPL and commercial licenses. A bare [DCO](https://developercertificate.org/) sign-off is *not* sufficient on its own, because it doesn't grant that re-licensing right — the CLA is what makes dual-licensing possible.

**How to agree:**
1. Read [`CLA.md`](CLA.md).
2. On your first pull request, confirm acceptance when the CLA check prompts you — or, if the check isn't wired up yet, state in the PR description: *"I have read and agree to the Aria CLA (CLA.md)"* and fill in the signer table from the CLA.
3. Contributing on behalf of an employer or organization? Contact the maintainer for a **Corporate CLA** first, or confirm your employer has waived its rights to your contribution.

By submitting a contribution you also affirm it's your original work (see the representations in the CLA).

## Project conventions

- **All LLM calls go through `llm_router.get_llm()`** — never instantiate a provider SDK (`ChatAnthropic`, `ChatGoogleGenerativeAI`) directly in a skill. This is what gives every skill the fallback chain. (See [ADR 0001](docs/adr/0001-claude-gemini-fallback.md).) The one deliberate exception is `clean_inbox.py`, a bulk batch tool that pins Gemini Flash for cost.
- **Gmail access goes through `skills/email_manager.get_gmail_service()`** so OAuth, scopes, and token handling stay in one place. Don't add new OAuth scopes without an ADR — scopes are intentionally minimal (see [ADR 0003](docs/adr/0003-gmail-scope-restriction.md)).
- **Anything that sends email goes through `send_email()`**, which enforces the allowlist. Never call the Gmail `send` API directly.
- **Persistent state lives in gitignored files** at the project root: `chroma_db/`, `cold_storage/`, `reports/`, `*.db`, `daily_scratchpad.txt`, `token*.json`. Build absolute paths from `os.path.dirname(__file__)` — scripts run from cron with an unpredictable CWD.
- **Every module gets a top-of-file docstring** stating what it is and how it's invoked (entry point / tool / one-shot script / nightly job).

## Adding a new skill

A "skill" is a module in `skills/` that exposes one or more LangChain tools the agent can call. The pattern:

### 1. Create `skills/<name>_manager.py`

```python
"""<Name> skill — <one line on what it does>.

Exposes the `<tool_name>` tool used by interact.py (and optionally main.py).
"""
import os
from langchain_core.tools import tool

# If you need an LLM:
from llm_router import get_llm
# If you need Gmail:
from skills.email_manager import get_gmail_service

@tool
def my_skill_action(arg: str) -> str:
    """One-paragraph description — THIS IS THE PROMPT the agent reads to decide
    when to call the tool. Be explicit about when to use it and what each arg means.

    Args:
        arg: what this is and the exact format expected.
    """
    # ... do the work ...
    return "Human-readable result string the agent can relay or reason over."
```

Tool docstrings are not just docs — the agent uses them to choose tools and fill arguments. Write them for the model.

### 2. Register the tool

- **Interactive agent** — import it in `interact.py` and add it to the `tools` list in `interact_loop()`. If the agent should know the tool exists by default, mention it in the system prompt too.
- **Morning briefing (optional)** — if the skill should run in the unattended 8 AM job, call it from `main.py` and thread its output into `report_generator.generate_daily_markdown()`.

### 3. Persistence (if any)
Use a root-level file/DB built from an absolute path, mirroring `calendar_manager.py` (SQLite) or `memory.py` (ChromaDB). Add the artifact to `.gitignore`. Initialize lazily/idempotently (e.g. `CREATE TABLE IF NOT EXISTS`) so first run on a fresh box just works.

### 4. Document it
- Add a row to the **"What Aria does"** table and the **repository layout** in the [README](README.md).
- If the skill embodies a non-obvious decision (a new external dependency, a new scope, a security trade-off), write an ADR.

## Dependencies
Pin new packages in `requirements.txt` (the file is fully pinned). If a skill needs a system-level component (e.g. Playwright's browser), note the post-install step in the README's setup section, as the Netflix skill does (`playwright install chromium`).

## Testing & verification
Run the offline unit tests with `python3 -m unittest discover tests` — they mock all Gmail/LLM/network access, so they're fast and safe. Add tests alongside any change to the email path (`send_email`'s allowlist behavior is contract, see ADR 0003). Agent behavior still needs manual verification; before committing:
- Run the skill's tool in isolation (`python3 -c "from skills.x import tool; print(tool.invoke({...}))"`).
- Run `python3 interact.py` and confirm the agent selects and calls the tool from a natural-language prompt.
- For anything in the morning path, run `./run.sh` once and inspect the generated `reports/daily_summary_<date>.md`.

## Commits & pull requests
Keep secrets out of git — `.env`, `credentials.json`, `token*.json`, `profile.json`, `allow.json`, `.spotify_cache`, and the data stores are gitignored; keep it that way.

Opening a PR:
- Confirm CLA acceptance (see [Licensing & the CLA](#licensing--the-cla-read-first)) — this is required before merge.
- Keep each PR to one logical change; run `python3 -m unittest discover tests` and include tests for new behavior.
- Note any new dependency, OAuth scope, or system-level step, and add an ADR for non-obvious decisions.

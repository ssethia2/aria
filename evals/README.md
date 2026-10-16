# Evals — does Aria actually behave?

`tests/` proves the code does what it says, with the LLM mocked out. **Evals test the
behavior**: run the real agent (same prompt, same tools, same brain) through short
conversations and check what she *did*. Did she pick the right tool, get the date right,
ask instead of guessing, own a mistake without being chipper?

Most scenarios are regressions for real failures: the wrong-item completion (#39),
invented deadlines, "the pose you've described", arguing that the calendar was down, a
chipper reply to a missed reminder. When she gets something wrong in real life, add a
scenario, so the same mistake can't quietly come back after a prompt or model change.

## Running

```bash
python3 -m evals                    # everything, brain from .env, 1 trial
python3 -m evals --only calendar    # substring filter on id or area
python3 -m evals --trials 3         # flakiness: pass rate per scenario
python3 -m evals --brain api        # exercise the API brain instead (bills the API)
python3 -m evals --live-context     # use copies of YOUR real profile/rules/people
python3 -m evals --list
```

Each run prints pass/fail per scenario (with the reason and her reply on failure),
the API spend for the run, and **regressions vs the previous run**. Full results
(every reply, tool call, and judge verdict) land in `evals/results/` (gitignored).
Exit code is non-zero if anything fails.

**Cost.** With `ARIA_BRAIN=subscription` the agent turns run on your Max subscription
(the same quota Telegram uses). The fast path (Haiku) and the LLM judge (Sonnet) bill the
API, typically a few cents per full run. The run's API spend is printed at the end.

## What's real and what's faked

The sandbox (`evals/sandbox.py`) runs the production agent against throwaway state:

- **Real, against temp copies**: commitments, people, standing rules, groceries. These
  are local stores, so scenarios can assert on what actually landed in the database.
- **Faked** (canned results, the real implementation is never reached): email, calendar,
  web search, weather, Spotify, lights and shortcuts, the browser, memory, news, Netflix.
  New tools are faked by default (an allowlist, not a denylist), so an eval can never
  touch a real account.
- **Sealed**: the subscription brain's session state and the conversation checkpointer
  use temp stores, and Telegram alerts are swallowed instead of pinging your phone.
- **Fixtures**: `evals/fixtures/` is a synthetic owner (profile, 3 standing rules, a few
  people), so results don't shift when you edit your real rules. Use `--live-context`
  to test against copies of your real ones.

## Writing a scenario

Scenarios are YAML in `evals/scenarios/<area>.yaml`. Each one says *why* it exists.

```yaml
- id: reschedule-in-place
  why: "Moving a date must reschedule the existing item, never drop it and re-add a copy."
  setup:
    commitments:                  # seeded into the sandbox store before turn 1
      - {description: "Dentist appointment", kind: deadline, due_date: "{{today+1}}"}
    rules: ["optional extra standing rule"]
  tools:                          # override any tool's canned result (list = per call)
    get_calendar_events: "Calendar:\n- {{wd:today+1}} 09:00 — Dentist"
  turns:
    - user: "Push the dentist thing to {{weekday:today+4}}."
      expect:
        called: [reschedule_commitment]
        not_called: [drop_commitment, add_commitment]
        called_with:
          reschedule_commitment: {new_due_date_iso: "{{today+4}}"}
        db:
          open_count: 1
          items:
            - {match: dentist, status: open, due_date: "{{today+4}}"}
```

A turn can be a photo instead (`photo: <what she sees>` plus `user: <caption>`), composed
exactly as the Telegram bot does.

**Dates** are relative, so scenarios never rot: `{{today+3}}` → ISO date,
`{{weekday:today+3}}` → `Sunday`, `{{wd:today+1}}` → `Fri`,
`{{longdate:today+7}}` → `Thursday, October 22`, `{{monthday:today+2}}` → `October 17`,
`{{annual:03-14}}` → the next March 14.

**Expectations** (all optional, all must hold):

| Key | Checks |
|---|---|
| `called` / `not_called` | each tool was / wasn't called this turn |
| `called_any` | at least one of these tools was called |
| `called_with` | some call of the tool had these args. Values match case-insensitively; `null` = unset; `{contains: x}`, `{one_of: [...]}`, `{time: "7:30pm"}` (time-normalized) |
| `reply_matches` / `reply_not_matches` | regexes (case-insensitive) on her reply |
| `mentions_date` | the reply names this date in any common form (Saturday, Oct 17, 10/17, tomorrow…) |
| `path` | `quick` (fast path answered) or `agent` |
| `db` | `open_count` / `done_count` / `dropped_count`, and `items` matched by description substring with field checks (`status`, `due_date`, `due_time`, `snoozed_until`, `who`, `kind`); `optional: true`, `absent: true` |
| `rules_count` | number of standing rules after the turn |
| `judge` | a rubric an LLM grades the reply against (for tone and judgment, which regexes can't check) |

Prefer deterministic checks. Use `judge` only for what can't be checked mechanically, and
write the rubric so a reply that meets its *intent* passes. `tests/test_evals.py` checks
that every scenario parses, references real tools and args, and resolves its templates.

# ADR 0006 — Google Calendar connector and its scopes

**Status:** Accepted (2026-06)

## Context
Satvik wants Aria managing his calendar, with a standing rule: every personal event is
created on BOTH his personal calendar (default notifications) and the calendar shared
with his girlfriend, color-coded yellow there. His work calendar is explicitly out of
bounds. Adding scopes to the primary Google token touches ADR 0003's minimal-scope
posture, so the addition is recorded here.

## Decision
1. **Scopes added to the primary token:** `calendar.events` (event CRUD) +
   `calendar.readonly` (list calendars/events). Deliberately NOT the full `calendar`
   scope — Aria cannot change calendar sharing/ACLs or delete calendars. One-time
   re-auth required (delete token.json, re-run the flow).
2. **Standing rules live in code, not prompt memory:** `create_calendar_event`
   dual-writes (primary + shared, colorId 5 on shared) unconditionally. The agent is
   told the rule exists but cannot forget or skip it.
3. **Shared-calendar id is configuration** (`calendar_config.json`, gitignored), set
   conversationally via `list_my_calendars` → `configure_shared_calendar` — connectors
   should be configurable through chat, not code edits.
4. The work calendar is simply never configured; with event-level scopes only, the
   blast radius of a mistake is a stray event, not a sharing change.

## Consequences
- Dual-created events are independent copies (no linkage): editing/deleting one does
  not affect the other. Acceptable for now; a future `update/delete_calendar_event`
  must handle both (store both event ids if that lands).
- Briefing and `get_calendar_events` dedupe the mirrored copies by (title, start).
- Pattern established for future connectors: tool-enforced standing rules + chat-driven
  configuration + scope additions documented by ADR.

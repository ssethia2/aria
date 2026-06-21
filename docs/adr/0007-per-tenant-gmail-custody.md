# ADR 0007 — Per-tenant Gmail custody (guest account connect)

**Status:** Accepted (2026-06)

## Context
Aria is going multi-tenant (SaaS; see the hosted guest trial at `ariaai.live`). The friends
trial deliberately gave guests an **account-free** toolset — isolated memory + reminders +
calendar links + web — and no email/calendar. But the actual value of Aria is managing your
real inbox, so the first SaaS question to validate is whether people will **connect their own
Gmail** to a hosted assistant. That requires Aria to hold *other people's* Google credentials,
which [ADR 0003](0003-gmail-scope-restriction.md) never contemplated — it assumes a single
owner token (`token.json`) under the operator's control.

## Decision
Allow a guest to connect their own Gmail, under tight constraints that keep custody and blast
radius small. This **extends** ADR 0003's posture (it does not relax it for the owner).

1. **Read-only scope for guests.** Guests grant only `gmail.readonly` — Aria can read/triage
   their inbox but cannot modify, label, send, or delete. (The owner keeps `gmail.modify` +
   `gmail.send` as before; this is strictly *less* power for guests, not more.)
2. **Separate Web OAuth client.** Guest connect uses a distinct **Web application** client
   (`GUEST_GOOGLE_CLIENT_ID/SECRET`) with a hosted redirect URI — never the owner's
   desktop/installed-app `credentials.json`.
3. **Per-tenant tokens, encrypted at rest.** Each guest's token is stored keyed by their
   principal, encrypted with Fernet (`ARIA_TOKEN_ENC_KEY`). A disk read alone does not yield a
   usable Google token. Tokens never enter git (gitignored), logs, or any prompt.
4. **Principal-aware service selection, fail-closed.** `get_gmail_service()` resolves the
   current principal: owner → `token.json`; guest → their stored encrypted token, or `None`
   when not connected. A guest can never reach the owner's token or another guest's.
5. **Gated, capability-honest tools.** Gmail tools enter the guest toolset only behind a
   "connected?" check; when not connected the tool returns a connect prompt rather than
   erroring or pretending.
6. **Testing-mode app for the demand test.** The consent screen stays in Testing with the
   waitlist as test users (≤100), so no Google verification is needed to validate demand.
   Trade-off: testing-mode refresh tokens expire ~weekly — acceptable for a validation test;
   production verification is a prerequisite before opening to the public.

## Consequences
- **We are now a custodian of third-party Google tokens.** This is a real liability the
  self-host path never had — it must be paired with a privacy policy, data export/deletion,
  and breach handling before *paying strangers* (not waitlist testers) connect accounts.
- **Read-only caps guest blast radius:** a compromised guest path can read that guest's mail,
  but cannot send/delete/modify or touch anyone else's — matching 0003's fail-safe spirit.
- **Encryption key is now critical infra.** Losing `ARIA_TOKEN_ENC_KEY` invalidates all stored
  guest tokens (they simply re-connect); leaking it + the disk exposes tokens. Treat it like
  any signing key.
- **Owner path unchanged.** `token.json`, scopes, and the send allowlist (0003) are untouched;
  this ADR only adds the guest dimension.
- **Reversible:** if the demand test fails, deleting the guest token store + the connect
  endpoints returns the system to the account-free guest model with no owner impact.

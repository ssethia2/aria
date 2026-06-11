# ADR 0005 — Local allow.json fallback for the send allowlist

**Status:** Accepted (2026-06). Amends the allowlist-sourcing part of [ADR 0003](0003-gmail-scope-restriction.md); the minimal-scopes and fail-safe-empty decisions stand.

## Context
ADR 0003 stored the send allowlist read-only in a GCS bucket so a rogue agent on the VM couldn't widen it. With GCP decommissioned (2026-06) and the laptop as host, the bucket is unreachable — `get_allowed_recipients()` fail-safed to empty and the email channel went permanently dead, discovered when the morning briefing could deliver only via Telegram.

## Decision
`get_allowed_recipients()` resolution order becomes: **GCS bucket (if `ALLOWLIST_BUCKET_NAME` set) → local `allow.json` → empty (send nothing)**. The repo-root `allow.json` (gitignored-adjacent, currently just the owner's address) is the laptop-era allowlist source; the hardcoded in-code default is removed in favor of that file.

## Consequences
- Email delivery works again on the laptop with no cloud dependency; Telegram remains the primary briefing channel.
- **Weaker isolation than the bucket:** a local file is writable by anything on the machine, including (in principle) future Aria tools. Accepted because the agent currently has no file-write tool, the file contains only the owner's address, and `send_email` still enforces the list in code. If Aria ever gains arbitrary file-write capability, revisit (e.g. move the allowlist to a root-owned path).
- The fail-safe-empty posture is preserved when *neither* source is available.

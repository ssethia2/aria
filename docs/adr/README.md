# Architecture Decision Records

Each ADR captures one significant decision: the **context** that forced a choice, the **decision** itself, and the **consequences** we accepted. ADRs are immutable once accepted — to reverse a decision, add a new ADR that supersedes the old one (don't edit history).

Use these when the *why* would otherwise live only in a code comment or someone's memory.

| # | Title | Status |
|---|-------|--------|
| [0001](0001-claude-gemini-fallback.md) | Claude → Gemini model fallback router | Accepted |
| [0002](0002-3tier-memory.md) | 3-tier OS-style memory architecture | Accepted (Tier 3 superseded by 0008) |
| [0003](0003-gmail-scope-restriction.md) | Restricted Gmail scopes + GCS allowlist | Accepted |
| [0004](0004-polling-engine-over-webhooks.md) | Polling proactivity engine over push webhooks | Accepted |
| [0005](0005-local-allowlist-fallback.md) | Local allow.json fallback for the send allowlist | Accepted (amends 0003) |
| [0006](0006-calendar-connector-scopes.md) | Google Calendar connector and its scopes | Accepted |
| [0007](0007-per-tenant-gmail-custody.md) | Per-tenant Gmail custody (guest account connect) | Accepted |
| [0008](0008-retire-cold-storage-tier.md) | Retire the cold-storage tier: one uncapped, dated semantic store | Accepted (supersedes 0002 Tier 3) |

## Writing a new ADR

Copy the shape of an existing one. Number sequentially, use a short kebab-case slug, and keep it to: Status / Context / Decision / Consequences.

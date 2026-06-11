# ADR 0003 — Restricted Gmail scopes + GCS send allowlist

**Status:** Accepted (2026-03)

## Context
Aria is an autonomous agent with access to the user's Gmail and an LLM deciding what to do. Two failure modes worry us: (1) a bug or prompt-injection causes Aria to **permanently delete** mail, and (2) Aria is induced to **send email to arbitrary recipients** (data exfiltration or spam from the user's own address). When the assistant runs unattended on a cloud VM, "the human will catch it" is not a control.

## Decision
Apply defense-in-depth at the capability boundary, so even a fully compromised agent is physically constrained.

1. **Minimal OAuth scopes.** Request only `gmail.modify` + `gmail.send`. Notably **not** `gmail.delete` / full-mail scope — Aria can label and archive ("To Be Deleted" label) but **cannot permanently delete** anything. Destructive deletion stays a manual human action in the Gmail UI.
2. **Send allowlist enforced in code.** `send_email` refuses any recipient not on an allowlist, failing safe to an **empty list** (send nothing) if the allowlist can't be loaded.
3. **Allowlist stored read-only off-box.** In production the allowlist lives as `allowlist.json` in a private GCS bucket; the VM's service account is granted only `Storage Object Viewer` on that bucket. The agent can *read* the allowlist but cannot *modify* it, even if it goes rogue. Locally it falls back to a hardcoded single address (`allow.json` mirrors the intended contents).

## Consequences
- **Blast radius capped:** worst case, Aria mislabels mail (recoverable) or fails to send (safe). It cannot destroy mail or email strangers.
- **Friction by design:** adding a legitimate new send recipient requires editing the bucket object, not just telling Aria. Intentional.
- **Fail-safe over fail-open:** if GCS is unreachable or auth breaks, the allowlist resolves to empty and *no* mail sends — the morning briefing silently won't deliver. Monitor for this; a missing briefing can mean an allowlist/auth problem, not "no news today".
- **Separate Netflix token is scoped the same way** (`gmail.modify` only) and kept in a distinct `token_netflix.json`, isolating the secondary account's blast radius from the primary.

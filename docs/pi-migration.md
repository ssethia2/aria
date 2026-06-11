# Migrating Aria to the Raspberry Pi

Aria was designed for this move (ADR 0004: no design changes needed, just relocation).
Everything below assumes a **Pi 5 (8GB recommended)** running **Raspberry Pi OS 64-bit**
(arm64 is required — Playwright and onnxruntime have no 32-bit builds). Budget ~1 hour.

## 0. Flash & boot
- Raspberry Pi Imager → Raspberry Pi OS Lite (64-bit) → enable SSH + set hostname
  (e.g. `aria.local`) + Wi-Fi in the imager's settings gear.
- Boot, then from the Mac: `ssh <user>@aria.local`

## 1. Clone + setup (on the Pi)
```bash
git clone git@github.com:ssethia2/aria.git && cd aria   # or HTTPS + a PAT
bash pi/setup_pi.sh
```
The script installs system deps, builds the venv, installs Playwright's Chromium,
and templates the systemd units — but does NOT start anything (state comes first).

## 2. STOP Aria on the Mac — before copying state
Two bots polling one Telegram token fight with 409s, and copying SQLite files while
they're being written risks corruption. On the **Mac**:
```bash
launchctl bootout gui/$(id -u)/com.aria.telegram-bot
launchctl bootout gui/$(id -u)/com.aria.morning-briefing
```

## 3. Copy secrets + state (from the Mac)
From the Mac project directory:
```bash
rsync -av --ignore-missing-args \
  .env credentials.json token.json token_netflix.json profile.json allow.json \
  calendar_config.json instructions.json \
  aria_calendar.db aria_checkpoints.db daily_scratchpad.txt \
  engine_state.json router_alerts.json \
  chroma_db cold_storage \
  <user>@aria.local:~/aria/
```
This carries her entire mind: memories (chroma), conversations (checkpoints),
commitments, standing instructions, engine state, and all credentials.

**.env tweak on the Pi:** remove `ALLOWLIST_BUCKET_NAME` (GCP is gone; the local
`allow.json` fallback is the allowlist now — removing the var skips the noisy
GCS attempt on every send).

## 4. Smoke test (on the Pi)
```bash
cd ~/aria && source venv/bin/activate
python3 -m unittest discover tests        # all green expected
python3 interact.py                       # say hi; "what's on my plate?"
```

## 5. Go live
```bash
sudo systemctl enable --now aria-telegram.service
sudo systemctl enable --now aria-briefing.timer
systemctl status aria-telegram            # active (running)
systemctl list-timers aria-briefing.timer # next 08:00 firing
```
Message her on Telegram. Logs: `tail -f ~/aria/logs/telegram_bot.log`
(or `journalctl -u aria-telegram -f`).

## 6. Decommission the Mac side
Once the Pi has run happily for a day or two:
```bash
rm ~/Library/LaunchAgents/com.aria.*.plist
```
Keep the Mac repo clone for development — it just stops being production.

---

## Known differences on the Pi

| Thing | Status |
|---|---|
| Voice **in** (Whisper STT) | Works — slower than the Mac (a few seconds for short notes; fine) |
| Voice **out** (TTS replies) | **Degrades to text** — `say` is macOS-only; the code falls back gracefully. Follow-up: piper-tts (same swap point, noted in `synthesize_voice_note`) |
| Netflix browser clicks | Works via Playwright Chromium arm64 — first run is slow (cold browser) |
| Morning briefing catch-up | `Persistent=true` on the timer ≈ launchd's fire-on-wake |
| Model downloads (Whisper) | First voice note triggers a ~150MB download — or pre-warm: `python3 -c "from llm_router import _get_whisper; _get_whisper()"` |

## §Playwright fallback (only if `playwright install chromium` failed)
```bash
sudo apt install -y chromium
```
Then in `skills/netflix_manager.py`, launch with the system browser:
`p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")`.

## Troubleshooting
- **Bot starts then dies repeatedly** → `journalctl -u aria-telegram -n 50`; usual
  suspects are a missed state file from §3 or `.env` not copied.
- **Telegram 409 conflicts in the log** → the Mac bot is still running; redo §2.
- **Briefing didn't arrive** → `systemctl status aria-briefing.service` shows the
  last run's result; the fail-loud Telegram alert should have fired too.

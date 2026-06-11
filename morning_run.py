"""launchd entry point for the 08:00 morning briefing (pure Python, no shell).

Exists because macOS TCC blocks /bin/bash from reading ~/Documents in launchd
context (run.sh died with "Operation not permitted", exit 126), while the Python
binary already holds that grant — the Telegram bot runs under it. Does what run.sh
does: nightly memory compaction (best-effort) and then the briefing (fail-loud).
run.sh remains for interactive use.
"""
import traceback


def run():
    try:
        from nightly_compaction import compact_scratchpad
        compact_scratchpad()
    except Exception:
        # Compaction trouble shouldn't cost the user their briefing.
        print(f"Compaction failed (continuing to briefing):\n{traceback.format_exc()}")

    from main import run_with_alerts
    run_with_alerts()


if __name__ == "__main__":
    run()

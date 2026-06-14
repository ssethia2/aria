"""Import-surface guard — catches a refactor silently dropping a function that another
module imports (e.g. transcribe_audio got dropped in the router rewrite, breaking voice;
py_compile didn't catch it because the import is at call time).

Run: python3 -m unittest discover tests
"""
import importlib
import unittest


class TestPublicSurface(unittest.TestCase):
    def test_llm_router_exports(self):
        m = importlib.import_module("llm_router")
        for name in ("get_llm", "transcribe_audio"):
            self.assertTrue(callable(getattr(m, name, None)), f"llm_router.{name} missing")

    def test_telegram_bot_call_time_imports_resolve(self):
        # The bot does `from llm_router import transcribe_audio` inside a function;
        # assert the symbols those deferred imports rely on actually exist.
        from llm_router import transcribe_audio  # noqa: F401
        from skills.email_manager import user_has_replied, draft_email_reply  # noqa: F401
        from skills.commitment_manager import open_reply_owed_for, resolve_replied  # noqa: F401

    def test_engine_monitor_classes_exist(self):
        import engine
        for cls in ("CommitmentMonitor", "EmailDigestMonitor", "ChaseMonitor",
                    "InsightMonitor", "ReplyResolveMonitor", "NetflixMonitor",
                    "HealthMonitor", "HeartbeatMonitor"):
            self.assertTrue(hasattr(engine, cls), f"engine.{cls} missing")


if __name__ == '__main__':
    unittest.main()

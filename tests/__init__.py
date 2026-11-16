"""Unit tests never touch real state.

Aria's state lives beside her code (commitments, memory, cost log, engine state), so a
test that forgets to patch one path writes into the owner's real data: one dropped a
live commitment, another logged fake calls to the cost log. Importing this package makes
that impossible rather than a matter of care:

  * every known state path points at a throwaway directory,
  * the real memory store is detached (a test that needs one supplies a fake),
  * writing, deleting or opening a database anywhere else in the repo raises, and
  * so does any network connection beyond this machine (a test with a forgotten mock
    would otherwise text him, read his mail or spend on a model).

A violation fails the run even if the code under test swallowed the error.

`tests/test_00_isolation.py` imports this so a plain `unittest discover tests` gets it too.
"""
import atexit
import builtins
import io
import os
import socket
import sqlite3
import sys
import tempfile

REPO = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
SANDBOX = os.path.realpath(tempfile.mkdtemp(prefix="aria-tests-"))
VIOLATIONS = []

# Places inside the repo a test may write: bytecode caches, and nothing else.
_ALLOWED = ("__pycache__",)


class RealStateWrite(AssertionError):
    """A test reached for the owner's real files."""


def _is_real_state(path) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False  # a file descriptor
    path = os.fsdecode(path)
    if path.startswith("file:"):
        path = path[5:].split("?", 1)[0]
    if path == ":memory:" or not path:
        return False
    full = os.path.realpath(path)
    if not (full == REPO or full.startswith(REPO + os.sep)):
        return False
    return not any(part in _ALLOWED for part in full.split(os.sep))


def _refuse(what: str, path):
    VIOLATIONS.append(f"{what} {os.fsdecode(path)}")
    raise RealStateWrite(
        f"a test tried to {what} real state: {os.fsdecode(path)}. Patch that path to a "
        f"temp file, or add it to tests/__init__.py's redirects.")


_real_open = builtins.open


def _guarded_open(file, mode="r", *args, **kwargs):
    if any(c in mode for c in "wax+") and _is_real_state(file):
        _refuse("write", file)
    return _real_open(file, mode, *args, **kwargs)


_real_connect = sqlite3.connect


def _guarded_connect(database, *args, **kwargs):
    if _is_real_state(database):
        _refuse("open the database", database)
    return _real_connect(database, *args, **kwargs)


_real_socket_connect = socket.socket.connect
_LOCAL = ("127.0.0.1", "::1", "localhost", "")


def _guarded_socket_connect(self, address):
    host = address[0] if isinstance(address, tuple) else None   # a str is a unix socket
    if host is not None and host not in _LOCAL:
        VIOLATIONS.append(f"connect to {host}")
        raise RealStateWrite(f"a test tried to reach the network ({host}); mock that call.")
    return _real_socket_connect(self, address)


def _guard_path_fn(module, name, what, arg_indexes=(0,)):
    real = getattr(module, name)

    def guarded(*args, **kwargs):
        # With a dir_fd the name is relative to that directory, not the repo (this is
        # how tempfile cleans up after itself), so there is nothing to resolve here.
        if not any(kwargs.get(k) is not None for k in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
            for i in arg_indexes:
                if i < len(args) and _is_real_state(args[i]):
                    _refuse(what, args[i])
        return real(*args, **kwargs)

    guarded.__name__ = name
    setattr(module, name, guarded)


def _redirect():
    """Point each module's state path at the sandbox. A module that fails to import here
    fails the same way in its own tests; the guard still covers it."""
    targets = {
        "agent_core": {"CHECKPOINT_DB_PATH": "aria_checkpoints.db"},
        "cost_tracker": {"LOG_PATH": "cost_log.json"},
        "decisions": {"LOG_PATH": "decisions.jsonl"},
        "engine": {"STATE_PATH": "engine_state.json"},
        "memory": {"SCRATCHPAD_PATH": "daily_scratchpad.txt",
                   "CONVERSATION_PATH": "daily_conversation.jsonl"},
        "skills.commitment_manager": {"DB_PATH": "aria_calendar.db"},
    }
    for module_name, paths in targets.items():
        try:
            module = __import__(module_name, fromlist=["_"])
        except Exception as e:
            print(f"[tests] couldn't redirect {module_name}: {e}", file=sys.stderr)
            continue
        for attr, filename in paths.items():
            if hasattr(module, attr):
                setattr(module, attr, os.path.join(SANDBOX, filename))
    try:
        # memory.py opens the real store when imported; nothing may add to it from here.
        import memory
        memory.collection = memory.embeddings = memory.chroma_client = None
    except Exception as e:
        print(f"[tests] couldn't detach the memory store: {e}", file=sys.stderr)
    try:
        from skills import commitment_manager
        commitment_manager.init_db()
    except Exception as e:
        print(f"[tests] couldn't create the throwaway commitments DB: {e}", file=sys.stderr)


def _report():
    if VIOLATIONS:
        sys.stderr.write("\n❌ Tests reached for real state (the code under test may have "
                         "swallowed the error):\n" +
                         "".join(f"   - {v}\n" for v in dict.fromkeys(VIOLATIONS)))
        sys.stderr.flush()
        os._exit(1)


if not getattr(builtins, "_aria_test_guard", False):
    builtins._aria_test_guard = True
    builtins.open = _guarded_open
    io.open = _guarded_open
    sqlite3.connect = _guarded_connect
    socket.socket.connect = _guarded_socket_connect
    for _name in ("remove", "unlink", "rmdir"):
        _guard_path_fn(os, _name, "delete")
    for _name in ("replace", "rename"):
        _guard_path_fn(os, _name, "overwrite", arg_indexes=(0, 1))
    _redirect()
    atexit.register(_report)

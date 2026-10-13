"""Logging for every Agentora component: one format, levels, the systemd journal and a rotating file per component.

    import logsetup
    log = logsetup.setup("radio")              # once, at startup (each process: servers, GPU workers, the relay)
    logging.getLogger(__name__).warning(...)   # anywhere

Lines look like `WARNING radio_spool: the critic failed ...` in the journal (journald adds the time) and
`2026-10-13 01:20:03 WARNING radio_spool: ...` in logs/<component>.log (5 MB x 5 files, next to the code).
LOG_LEVEL=DEBUG|INFO|WARNING sets the level (default INFO; DEBUG adds every web request). LOG_DIR moves the files.
Uncaught exceptions, in the main thread or any other, are logged as ERROR with their traceback.
"""
import logging, logging.handlers, os, re, sys, threading
from pathlib import Path

FMT = "%(levelname)s %(name)s: %(message)s"
_WARN = re.compile(r"\b(error|errors|failed|fails|couldn'?t|could not|cannot|can'?t|not reachable|gave up|timed out|"
                   r"timeout|refused|missing|invalid|skipped)\b", re.I)


def level_of(text):
    """A level for a plain log line: tracebacks are errors, trouble words are warnings, the rest is info."""
    if "Traceback (most recent call last)" in text:
        return logging.ERROR
    return logging.WARNING if _WARN.search(text) else logging.INFO


def setup(component, log_dir=None):
    root = logging.getLogger()
    if getattr(root, "_agentora", None) == component:
        return logging.getLogger(component)
    for h in list(root.handlers):
        root.removeHandler(h)
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(level)
    out = logging.StreamHandler(sys.stdout)                 # -> the systemd journal (it adds the timestamp)
    out.setFormatter(logging.Formatter(FMT))
    root.addHandler(out)
    d = Path(log_dir or os.environ.get("LOG_DIR") or Path(__file__).resolve().parent.parent / "logs")
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = logging.handlers.RotatingFileHandler(d / f"{component}.log", maxBytes=5 * 2**20, backupCount=5, encoding="utf-8")
        f.setFormatter(logging.Formatter("%(asctime)s " + FMT, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(f)
    except OSError as e:
        root.warning("no log file in %s (%s); logging to the journal only", d, e)
    for noisy in ("urllib3", "asyncio", "PIL", "matplotlib", "numba", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    def excepthook(t, v, tb):
        if issubclass(t, KeyboardInterrupt):
            return sys.__excepthook__(t, v, tb)
        logging.getLogger(component).error("uncaught %s", t.__name__, exc_info=(t, v, tb))

    def thread_hook(a):
        if a.exc_type in (SystemExit,) or issubclass(a.exc_type, (BrokenPipeError, ConnectionResetError)):
            return
        logging.getLogger(a.thread.name if a.thread else component).error("uncaught %s in a thread", a.exc_type.__name__,
                                                                           exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    root._agentora = component
    return logging.getLogger(component)


class QuietBrokenPipes:
    """Mix into a socketserver/http.server server: a client that went away isn't an error; anything else is logged."""

    def handle_error(self, request, client_address):
        t, v = sys.exc_info()[:2]
        if t and issubclass(t, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        logging.getLogger("http").error("request from %s failed", client_address[0], exc_info=True)

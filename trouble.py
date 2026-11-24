r"""
trouble.py - when something fails (a rip, a filing), remember it so the app can offer to send an
error report to Shelfrip's developer. Nothing is sent unless you click Send.

  record(what, label, error)   note a failure (the same failure twice is offered once)
  pending()                    failures not yet sent or dismissed, newest first, each with the
                               exact text that would be sent ("preview")
  send(id, note, contact, cfg) send one through the Feedback channel (community.send_feedback)
  dismiss(id)                  don't offer it again
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import threading
import traceback
from pathlib import Path

LIMIT = 4700          # the feedback server keeps 5000 characters; room for your own note
_LOCK = threading.Lock()
PENDING: dict[str, dict] = {}
DONE: set[str] = set()   # sent or dismissed this session


def _redact(text: str) -> str:
    """Leave out your user name (it's in paths like C:\\Users\\you\\...)."""
    home = str(Path.home())
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    if len(user) >= 3:
        # whole words only, so a user called "ann" doesn't turn "channel" into "ch<user>el"
        text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", "<user>", text, flags=re.I)
    return text


def _log_lines(label: str, n: int = 40) -> list[str]:
    """The latest activity-log lines about this disc."""
    try:
        import rip_all
        lines = list(rip_all.LOG_LINES)
    except Exception:
        return []
    mine = [ln for ln in lines if label and label in ln] if label else []
    return (mine or lines)[-n:]


def record(what: str, label: str, error: str, detail: str = "", with_trace: bool = False) -> str:
    """Remember a failure to offer as an error report. Returns its id."""
    error = str(error or "").strip() or "(no message)"
    key = hashlib.sha1(f"{what}|{label}|{error[:120]}".encode("utf-8", "replace")).hexdigest()[:12]
    trace = traceback.format_exc() if with_trace else ""
    if trace.strip() == "NoneType: None":
        trace = ""
    with _LOCK:
        if key in DONE or key in PENDING:
            return key
        PENDING[key] = {"id": key, "what": what, "label": label, "error": error, "detail": detail,
                        "trace": trace, "log": _log_lines(label),
                        "when": dt.datetime.now().isoformat(timespec="seconds")}
    return key


def compose(item: dict, note: str = "") -> str:
    """The report's text, as it would be sent (cut to fit, keys and your user name left out)."""
    parts = [f"What failed: {item['what']}" + (f" ({item['label']})" if item.get("label") else ""),
             f"When: {item['when']}", f"Error: {item['error']}"]
    if item.get("detail"):
        parts.append(f"Details: {item['detail']}")
    if note.strip():
        parts.append(f"What I was doing: {note.strip()}")
    head = _redact("\n".join(parts))
    tail = ""
    if item.get("trace"):
        tail += "\n\nTraceback (last lines):\n" + "\n".join(item["trace"].strip().splitlines()[-12:])
    if item.get("log"):
        tail += "\n\nActivity log:\n" + "\n".join(item["log"])
    tail = _redact(tail)
    room = LIMIT - len(head)
    if len(tail) > room:  # keep the newest lines: they're nearest the failure
        tail = "\n…" + tail[-(room - 2):]
    return (head + tail)[:LIMIT]


def pending() -> list[dict]:
    with _LOCK:
        items = sorted(PENDING.values(), key=lambda x: x["when"], reverse=True)
    return [{**{k: v for k, v in it.items() if k not in ("trace", "log")}, "preview": compose(it)} for it in items]


def dismiss(key: str) -> None:
    with _LOCK:
        PENDING.pop(key, None)
        DONE.add(key)


def send(key: str, note: str, contact: str, cfg: dict) -> dict:
    with _LOCK:
        item = PENDING.get(key)
    if not item:
        return {"error": "That report was already sent or dismissed."}
    import community
    title = f"Error report: {item['what']}" + (f" ({item['label']})" if item.get("label") else "")
    r = community.send_feedback({"kind": "problem", "title": title[:200], "body": compose(item, note),
                                 "contact": contact}, cfg)
    if r.get("ok"):
        dismiss(key)
    return r

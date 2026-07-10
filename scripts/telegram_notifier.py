#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram notifier — reads token + chat_id from ~/.config/ptrak/telegram.json
(file mode 0600, lives outside the git repo).

Usage:
    from telegram_notifier import send_message, discover_chat_id
    send_message("hello")                          # uses cached chat_id
    discover_chat_id()                             # one-time: finds your chat_id from getUpdates

If chat_id is null in config, send_message() auto-runs discover_chat_id() once.

Defensive: any failure prints a one-line error to stderr and returns False — the
caller (e.g. QQQ monitor cron) keeps running so a Telegram outage never breaks
the analysis pipeline.
"""

import json
import os
import stat
import sys
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

try:
    from scripts.artifact_io import atomic_write_json, ensure_private_directory
except ImportError:  # direct `python scripts/telegram_notifier.py`
    from artifact_io import atomic_write_json, ensure_private_directory

CONFIG_PATH = Path.home() / ".config" / "ptrak" / "telegram.json"
API_BASE = "https://api.telegram.org/bot{token}/{method}"


def _load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"missing {CONFIG_PATH} — create it with {{token, chat_id}}")
    info = CONFIG_PATH.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("Telegram config must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise PermissionError("Telegram config must be owned by the current user")
    if info.st_mode & 0o077:
        raise PermissionError("Telegram config permissions must be 0600")
    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant {value}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f, parse_constant=reject_constant)
    if not isinstance(config, dict):
        raise ValueError("Telegram config must be a JSON object")
    token = config.get("token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Telegram config token must be non-empty text")
    chat_id = config.get("chat_id")
    if chat_id is not None and not isinstance(chat_id, (int, str)):
        raise ValueError("Telegram config chat_id must be an integer, text, or null")
    return config


def _safe_error(exc, token=None):
    detail = str(exc)
    if token:
        detail = detail.replace(str(token), "[redacted-token]")
    return detail


def _save_config(cfg):
    ensure_private_directory(CONFIG_PATH.parent)
    atomic_write_json(CONFIG_PATH, cfg)


def _api(token, method, params=None, timeout=10):
    """One-shot Telegram API call. Returns parsed JSON or raises."""
    url = API_BASE.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    j = json.loads(body)
    if not j.get("ok"):
        raise RuntimeError(f"Telegram API not-ok: {j.get('description')}")
    return j


def discover_chat_id():
    """
    Call getUpdates to find the chat_id of whoever last messaged the bot.
    Caches into config. Returns chat_id (int) or None.
    """
    cfg = _load_config()
    token = cfg["token"]
    try:
        j = _api(token, "getUpdates", {"timeout": 0, "limit": 100})
    except Exception as e:
        print(f"telegram getUpdates failed: {_safe_error(e, token)}", file=sys.stderr)
        return None

    updates = j.get("result", [])
    if not updates:
        print(
            "telegram getUpdates returned no messages — open your Telegram bot "
            "and send /start (or any message), then re-run.",
            file=sys.stderr,
        )
        return None

    # Take the most recent message's chat id. Prefer private chat over group/channel.
    chat_id = None
    sender = None
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post")
        if not msg:
            continue
        c = msg.get("chat", {})
        chat_id = c.get("id")
        sender = c.get("username") or c.get("first_name") or c.get("title") or str(chat_id)
        if c.get("type") == "private":
            break

    if chat_id is None:
        print("telegram getUpdates had updates but none had a chat — skipping", file=sys.stderr)
        return None

    cfg["chat_id"] = chat_id
    cfg["chat_with"] = sender
    _save_config(cfg)
    print(f"telegram: chat_id={chat_id} ({sender}) cached to {CONFIG_PATH}", file=sys.stderr)
    return chat_id


def send_message(text, parse_mode="HTML", disable_web_page_preview=True):
    """
    Send a Telegram message. Returns True on success, False otherwise.
    Never raises — Telegram down/quota issues should not break the caller.
    """
    try:
        cfg = _load_config()
    except Exception as e:
        print(f"telegram config error: {e}", file=sys.stderr)
        return False

    token = cfg.get("token")
    chat_id = cfg.get("chat_id")
    if not token:
        print("telegram: no token in config", file=sys.stderr)
        return False

    if chat_id is None:
        chat_id = discover_chat_id()
        if chat_id is None:
            return False

    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_web_page_preview else "false",
    }
    try:
        _api(token, "sendMessage", params)
        return True
    except urllib.error.HTTPError as e:
        # Telegram returns 4xx with JSON body — read it for a clearer error
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            desc = err_body.get("description", str(e))
        except Exception:
            desc = str(e)
        # If chat not found, the cached chat_id may be stale — rediscover next time
        if "chat not found" in desc.lower() or "chat_id is empty" in desc.lower():
            cfg["chat_id"] = None
            _save_config(cfg)
        print(f"telegram send failed: {_safe_error(desc, token)}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"telegram send failed: {_safe_error(e, token)}", file=sys.stderr)
        return False


if __name__ == "__main__":
    # CLI: `python3 telegram_notifier.py "message"` or `python3 telegram_notifier.py --discover`
    if len(sys.argv) > 1 and sys.argv[1] == "--discover":
        cid = discover_chat_id()
        sys.exit(0 if cid is not None else 1)
    elif len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
        ok = send_message(msg)
        sys.exit(0 if ok else 1)
    else:
        print("usage: telegram_notifier.py <message> | --discover", file=sys.stderr)
        sys.exit(2)

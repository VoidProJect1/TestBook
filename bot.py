"""
VoidAI — Single-file Telegram bot
requests + threading, HTML parse mode
Supports: Groq (Llama), Gemini, Mistral (auto-rotation)
Features: Web search, Image analysis, Voice replies, Admin panel
"""

import asyncio
import base64
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# =============================================================================
# CONFIG
# =============================================================================
CONFIG_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "void_config.json")
BOT_TOKEN       = "8613601784:AAGPQbhIYwJqc30g99X3yLa6Vhkkl69Nf8I"   # ← replace
BOOTSTRAP_ADMIN = 5479881365                          # ← replace with your Telegram user ID

# SearXNG — set to "" to skip, or "http://127.0.0.1:8080" after installing
SEARXNG_URL = "http://127.0.0.1:8080"

_CFG_DEFAULTS = {
    "admin_ids":              [BOOTSTRAP_ADMIN],
    "gemini_keys":            [],
    "groq_keys":              [],
    "mistral_keys":           [],
    "banned_users":           [],
    "user_limit_multipliers": {},
    "limits": {
        "lite":  {"messages": 200, "images": 10, "searches": 50},
        "flash": {"messages": 100, "images": 10, "searches": 50},
        "pro":   {"messages":  50, "images": 10, "searches": 50},
    },
}

def _load_cfg() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            for k, v in _CFG_DEFAULTS.items():
                if k not in data:
                    data[k] = v
            if BOOTSTRAP_ADMIN not in data["admin_ids"]:
                data["admin_ids"].append(BOOTSTRAP_ADMIN)
            return data
        except Exception:
            pass
    cfg = dict(_CFG_DEFAULTS)
    _save_cfg(cfg)
    return cfg

def _save_cfg(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_cfg() -> dict:   return _load_cfg()
def save_cfg(cfg: dict): _save_cfg(cfg)
def is_admin(uid: int)   -> bool: return uid == BOOTSTRAP_ADMIN or uid in _load_cfg()["admin_ids"]
def is_banned(uid: int)  -> bool: return uid in _load_cfg()["banned_users"]

# =============================================================================
# SESSION
# =============================================================================
_sessions: Dict[int, dict] = {}
_usage:    Dict[int, dict] = {}

def _default_sess() -> dict:
    return {
        "model":      "flash",
        "history":    [],
        "voice_on":   False,
        "voice_name": "hi-IN-SwaraNeural",
        "firstname":  "there",   # Telegram first_name stored here
    }

def get_sess(uid: int) -> dict:
    if uid not in _sessions:
        _sessions[uid] = _default_sess()
    return _sessions[uid]

def reset_sess(uid: int): _sessions[uid] = _default_sess()

def set_firstname(uid: int, name: str):
    get_sess(uid)["firstname"] = name or "there"

def get_firstname(uid: int) -> str:
    return get_sess(uid).get("firstname", "there")

def add_history(uid: int, role: str, content: str):
    h = get_sess(uid)["history"]
    h.append({"role": role, "content": content})
    if len(h) > 30:
        get_sess(uid)["history"] = h[-30:]

def get_history(uid: int) -> list: return get_sess(uid)["history"]
def get_model(uid: int)   -> str:  return get_sess(uid)["model"]

def set_model(uid: int, model: str):
    if model in ("lite", "flash", "pro"):
        get_sess(uid)["model"] = model

def get_voice(uid: int) -> Tuple[bool, str]:
    s = get_sess(uid)
    return s["voice_on"], s["voice_name"]

def set_voice(uid: int, on: bool, name: str = None):
    s = get_sess(uid)
    s["voice_on"] = on
    if name:
        s["voice_name"] = name

# =============================================================================
# RATE LIMITER  (1-hour rolling window)
# =============================================================================
LIMIT_WINDOW = 3600  # 1 hour in seconds

def _window(): return time.time() - LIMIT_WINDOW

def _get_limit(uid: int, model: str, action: str) -> int:
    cfg  = get_cfg()
    base = cfg["limits"].get(model, {}).get(action, 50)
    mult = float(cfg["user_limit_multipliers"].get(str(uid),
           cfg["user_limit_multipliers"].get("global", 1)))
    return int(base * mult)

def _get_multiplier(uid: int) -> float:
    cfg = get_cfg()
    return float(cfg["user_limit_multipliers"].get(str(uid),
           cfg["user_limit_multipliers"].get("global", 1)))

def check_limit(uid: int, action: str = "messages") -> bool:
    model  = get_model(uid)
    stamps = [t for t in _usage.get(uid, {}).get(action, []) if t > _window()]
    _usage.setdefault(uid, {})[action] = stamps
    return len(stamps) < _get_limit(uid, model, action)

def record_usage(uid: int, action: str = "messages"):
    _usage.setdefault(uid, {}).setdefault(action, []).append(time.time())

def get_usage_stats(uid: int) -> dict:
    """Return detailed usage stats for /usage command."""
    model   = get_model(uid)
    now     = time.time()
    window  = now - LIMIT_WINDOW
    result  = {}
    actions = ["messages", "images", "searches"]
    for action in actions:
        stamps   = [t for t in _usage.get(uid, {}).get(action, []) if t > window]
        _usage.setdefault(uid, {})[action] = stamps
        used     = len(stamps)
        limit    = _get_limit(uid, model, action)
        # Time until oldest stamp expires (i.e. when a slot frees up)
        if stamps:
            oldest       = min(stamps)
            reset_in_sec = int((oldest + LIMIT_WINDOW) - now)
            reset_in_sec = max(0, reset_in_sec)
        else:
            reset_in_sec = 0
        result[action] = {
            "used":         used,
            "limit":        limit,
            "remaining":    max(0, limit - used),
            "reset_in_sec": reset_in_sec,
        }
    return result

def fmt_time_left(seconds: int) -> str:
    """Format seconds into mm:ss or hh:mm:ss."""
    if seconds <= 0:
        return "now"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"

def limit_exceeded_msg(uid: int, action: str) -> str:
    """Return a rich limit-exceeded message with reset time."""
    model    = get_model(uid)
    now      = time.time()
    stamps   = [t for t in _usage.get(uid, {}).get(action, []) if t > _window()]
    limit    = _get_limit(uid, model, action)
    mult     = _get_multiplier(uid)
    tier_lbl = {"lite": "Void Lite ⚡", "flash": "Void Flash ✨", "pro": "Void Pro 🌐"}.get(model, model)
    act_lbl  = {"messages": "💬 Messages", "images": "🖼️ Images", "searches": "🔍 Searches"}.get(action, action)
    if stamps:
        oldest   = min(stamps)
        reset_in = int((oldest + LIMIT_WINDOW) - now)
        reset_in = max(0, reset_in)
        reset_str = fmt_time_left(reset_in)
    else:
        reset_str = "soon"
    mult_str = f" (×{mult})" if mult != 1.0 else ""
    return (
        f"⛔ <b>{act_lbl} limit reached!</b>\n\n"
        f"📊 Used <b>{len(stamps)}/{limit}</b>{mult_str} this hour\n"
        f"🕐 Resets in <b>{reset_str}</b>\n\n"
        f"🏷️ Your plan: <b>{tier_lbl}</b>\n"
        f"<i>Use /usage to see all limits</i>"
    )

# =============================================================================
# DEAD KEY TRACKING
# =============================================================================
_dead_keys: set = set()

# =============================================================================
# HTTP SESSION
# =============================================================================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
http = requests.Session()
http.headers.update(HEADERS)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =============================================================================
# MARKDOWN → HTML  (fixes ** showing as stars)
# =============================================================================
def md_to_html(text: str) -> str:
    """
    Convert AI markdown output to Telegram HTML.
    Handles: **bold**, *italic*, `code`, ```code blocks```, headers.
    Escapes HTML special chars first, then applies formatting.
    """
    # Escape HTML special chars first
    text = html.escape(text, quote=False)

    # Code blocks (``` ... ```) — do before inline code
    text = re.sub(
        r"```(?:\w+)?\n?(.*?)```",
        lambda m: f"<pre><code>{m.group(1).strip()}</code></pre>",
        text, flags=re.DOTALL
    )

    # Inline code
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)

    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__",     r"<b>\1</b>", text, flags=re.DOTALL)

    # Italic: *text* or _text_ (single, not double)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",       r"<i>\1</i>", text)

    # Headers → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    return text.strip()

def safe_send_text(text: str) -> str:
    """Convert markdown to HTML and truncate if too long."""
    converted = md_to_html(text)
    if len(converted) > 4000:
        converted = converted[:3990] + "\n<i>…(truncated)</i>"
    return converted

# =============================================================================
# TELEGRAM HELPERS
# =============================================================================
def esc(t: str) -> str: return html.escape(t or "", quote=False)

def tg(method: str, data: dict = None, files=None, timeout=30):
    url = f"{TG_API}/{method}"
    try:
        if files:
            r = http.post(url, data=data or {}, files=files, timeout=timeout)
        else:
            r = http.post(url, data=data or {}, timeout=timeout)
        result = r.json()
        if not result.get("ok"):
            print(f"[TG] {method} failed: {result.get('description','')}")
        return result
    except Exception as e:
        print(f"[TG] {method} error: {e}")
        return {}

def send_msg(chat_id: int, text: str) -> int:
    """Send plain message, no inline buttons."""
    payload = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    res = tg("sendMessage", payload, timeout=20)
    return res.get("result", {}).get("message_id", 0)

def send_msg_markup(chat_id: int, text: str, markup: dict) -> int:
    """Send message WITH inline buttons — only used for /start, /switch, /voice, /admin."""
    payload = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
        "reply_markup":             json.dumps(markup),
    }
    res = tg("sendMessage", payload, timeout=20)
    return res.get("result", {}).get("message_id", 0)

def edit_msg(chat_id: int, msg_id: int, text: str):
    """Edit message, NO inline buttons."""
    payload = {
        "chat_id":                  chat_id,
        "message_id":               msg_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    tg("editMessageText", payload, timeout=20)

def edit_msg_markup(chat_id: int, msg_id: int, text: str, markup: dict):
    """Edit message WITH inline buttons."""
    payload = {
        "chat_id":                  chat_id,
        "message_id":               msg_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
        "reply_markup":             json.dumps(markup),
    }
    tg("editMessageText", payload, timeout=20)

def answer_cb(cb_id: str, text: str = ""):
    tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text}, timeout=10)

def send_typing(chat_id: int):
    tg("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=5)

def send_upload_photo(chat_id: int):
    tg("sendChatAction", {"chat_id": chat_id, "action": "upload_photo"}, timeout=5)

def send_upload_document(chat_id: int):
    tg("sendChatAction", {"chat_id": chat_id, "action": "upload_document"}, timeout=5)

def typing_loop(chat_id: int, stop: threading.Event, action: str = "typing"):
    while not stop.is_set():
        try: tg("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)
        except Exception: pass
        stop.wait(4.0)

def start_typing(chat_id: int, action: str = "typing") -> Tuple[int, threading.Event]:
    stop = threading.Event()
    threading.Thread(target=typing_loop, args=(chat_id, stop, action), daemon=True).start()
    msg_id = send_msg(chat_id, "⏳")
    return msg_id, stop


# =============================================================================
# TYPEWRITER EFFECT  — reveal answer progressively at 20/40/60/85/100%
# Total animation duration ≤ 1 second (delays: 0.15+0.2+0.2+0.2+0.25 = 1.0s)
# =============================================================================
def _typewriter_slice(text: str, pct: int) -> str:
    """
    Return the first `pct`% of plain text characters, then convert to HTML.
    Works on the raw markdown text so md_to_html tags don't get split mid-tag.
    """
    cut = max(1, int(len(text) * pct / 100))
    # Walk forward until we're not mid-word (avoid orphan HTML entities)
    while cut < len(text) and text[cut] not in (" ", "\n", "."):
        cut += 1
    snippet = text[:cut].rstrip()
    return safe_send_text(snippet) + " ✍️"

def _show_progress(chat_id: int, msg_id: int):
    """
    Legacy shim used by handle_image (non-voice path).
    Shows a simple pulsing indicator — image pipeline has its own status msgs.
    """
    stages = ["⏳", "⏳⏳", "⏳⏳⏳"]
    for s in stages:
        try:
            tg("editMessageText", {
                "chat_id":    chat_id,
                "message_id": msg_id,
                "text":       s,
                "parse_mode": "HTML",
            }, timeout=8)
        except Exception:
            pass
        time.sleep(0.2)

def typewriter_edit(chat_id: int, msg_id: int, full_text: str, stop: threading.Event):
    """
    Typewriter reveal: show 20% → 40% → 60% → 85% → 100% of the answer.
    Max total duration: 1 second.  Each stage is a real partial answer edit.
    """
    stop.set()  # stop the typing action loop — we're now showing content

    # Stages: (percent_of_text, delay_after_seconds)
    stages = [
        (20, 0.15),
        (40, 0.20),
        (60, 0.20),
        (85, 0.20),
    ]

    for pct, delay in stages:
        snippet = _typewriter_slice(full_text, pct)
        try:
            tg("editMessageText", {
                "chat_id":                  chat_id,
                "message_id":               msg_id,
                "text":                     snippet[:4000],
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            }, timeout=8)
        except Exception as e:
            print(f"[Typewriter] {pct}% edit failed: {e}")
        time.sleep(delay)

    # 100% — final full answer (no cursor)
    converted_full = safe_send_text(full_text)
    try:
        tg("editMessageText", {
            "chat_id":                  chat_id,
            "message_id":               msg_id,
            "text":                     converted_full[:4000],
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
    except Exception as e:
        print(f"[Typewriter] final edit failed: {e}")

def get_updates(offset=None):
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset
    try:
        r = http.get(f"{TG_API}/getUpdates", params=params, timeout=40)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[TG] getUpdates error: {e}")
        return []

# =============================================================================
# WEB SEARCH  (SearXNG only — self-hosted)
# =============================================================================
_search_cache: Dict[str, dict] = {}

def _cache_get(key: str):
    item = _search_cache.get(key)
    if not item: return None
    if time.time() - item["ts"] > 600:
        _search_cache.pop(key, None)
        return None
    return item["value"]

def _cache_set(key: str, value):
    _search_cache[key] = {"ts": time.time(), "value": value}


def web_search(query: str, max_results: int = 5, page: int = 1, bypass_cache: bool = False) -> List[Dict]:
    cache_key = f"search::{query.lower()}::p{page}"
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached:
            print(f"[Search] cache hit for: {query[:40]} page={page}")
            return cached

    if not SEARXNG_URL:
        print("[Search] SearXNG URL not configured")
        return []

    try:
        r = http.get(
            f"{SEARXNG_URL}/search",
            params={
                "q":        query,
                "format":   "json",
                "language": "en",
                "engines":  "google,bing,brave",
                "pageno":   page,
            },
            timeout=10,
        )
        r.raise_for_status()
        data    = r.json()
        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title":   item.get("title", ""),
                "link":    item.get("url", ""),
                "snippet": item.get("content", ""),
            })
        if results:
            print(f"[Search] SearXNG page={page} returned {len(results)} results")
            _cache_set(cache_key, results)
        else:
            print(f"[Search] SearXNG page={page} returned 0 results")
        return results
    except Exception as e:
        print(f"[SearXNG] error: {e}")
        return []
    return results


def format_context(query: str, results: List[Dict]) -> str:
    lines = [f"QUESTION: {query}", "", "WEB SEARCH RESULTS:"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}")
        lines.append(f"   URL: {r.get('link', '')}")
        lines.append(f"   {r.get('snippet', '')}")
        lines.append("")
    return "\n".join(lines)

# =============================================================================
# AI PROVIDERS
# =============================================================================
# Gemini: VERIFIED stable model strings (from ai.google.dev/gemini-api/docs/models April 2026)
# Use simple alias names — Google auto-routes to latest stable version
GEMINI_MODELS = [
    "gemini-2.5-flash",      # stable alias → latest 2.5 Flash (free tier, 250 RPD)
    "gemini-2.0-flash",      # stable alias → reliable fallback
    "gemini-2.0-flash-lite", # stable alias → lightweight fallback
    "gemini-1.5-flash",      # stable alias → last resort
]
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Groq: VERIFIED model IDs (from console.groq.com/docs/models April 2026)
# Primary: llama-3.1-8b-instant (fastest, free tier)
# Fallback: llama-3.3-70b-versatile (smarter, same free tier)
# Last Groq fallback: meta-llama/llama-4-scout-17b-16e-instruct (Llama 4)
GROQ_MODELS = [
    "llama-3.1-8b-instant",               # Primary — fastest, 8B params, 128K ctx
    "llama-3.3-70b-versatile",            # Fallback — smarter 70B, 128K ctx
    "meta-llama/llama-4-scout-17b-16e-instruct",  # Last Groq resort — Llama 4
]
GROQ_BASE   = "https://api.groq.com/openai/v1/chat/completions"

MISTRAL_BASE = "https://api.mistral.ai/v1/chat/completions"

VOID_SYSTEM = (
    "You are VoidAI, a smart and helpful AI assistant. "
    "Be concise and direct. Never mention other AI product names. "
    "Answer clearly. You may use markdown formatting like **bold**, *italic*, `code`. "
    "Do NOT use the user's name in every reply — only use it when it feels natural "
    "(like greetings or when being personal). Avoid repeating it mechanically."
)

def _detect_mime(b64: str) -> str:
    try:
        raw = base64.b64decode(b64[:16])
        if raw[:4] == b"\x89PNG":    return "image/png"
        if raw[:3] == b"\xff\xd8\xff": return "image/jpeg"
        if raw[:4] == b"RIFF":       return "image/webp"
    except Exception:
        pass
    return "image/jpeg"

def _gemini_contents(history: list, prompt: str, image_b64: str = None) -> list:
    contents = []
    for m in history[-20:]:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    parts = []
    if image_b64:
        mime = _detect_mime(image_b64)
        parts.append({"inlineData": {"mimeType": mime, "data": image_b64}})
    parts.append({"text": prompt})
    contents.append({"role": "user", "parts": parts})
    return contents

def _openai_messages(system: str, history: list, prompt: str) -> list:
    msgs = [{"role": "system", "content": system}]
    for m in history[-20:]:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": prompt})
    return msgs

def _try_gemini(prompt: str, system: str, history: list, image_b64: str = None) -> Optional[str]:
    cfg  = get_cfg()
    keys = cfg.get("gemini_keys", [])
    if not keys:
        print("[Gemini] no keys configured")
        return None

    contents = _gemini_contents(history, prompt, image_b64)
    sys_inst = {"parts": [{"text": system}]}

    for model in GEMINI_MODELS:
        for key in keys:
            tag = f"gemini:{key[:8]}:{model}"
            if tag in _dead_keys: continue
            url  = GEMINI_BASE.format(model=model) + f"?key={key}"
            body = {
                "contents":          contents,
                "systemInstruction": sys_inst,
                "generationConfig":  {
                    "maxOutputTokens": 2048,
                    "temperature":     0.7,
                },
            }
            try:
                r = http.post(url, json=body, timeout=45)
                # Key permanently invalid
                if r.status_code in (400, 401, 403):
                    err = r.json().get("error", {})
                    print(f"[Gemini] {model} key dead ({r.status_code}): {err.get('message','')[:100]}")
                    _dead_keys.add(tag)
                    continue
                # Rate limited — try next key
                if r.status_code == 429:
                    print(f"[Gemini] {model} rate limited, trying next key")
                    continue
                if r.status_code != 200:
                    print(f"[Gemini] {model} HTTP {r.status_code}: {r.text[:200]}")
                    continue

                data  = r.json()
                # Check for safety block or empty candidates
                cands = data.get("candidates", [])
                if not cands:
                    prompt_feedback = data.get("promptFeedback", {})
                    block_reason    = prompt_feedback.get("blockReason", "")
                    if block_reason:
                        print(f"[Gemini] blocked: {block_reason}")
                    else:
                        print(f"[Gemini] {model} empty candidates")
                    continue

                cand    = cands[0]
                content = cand.get("content", {})
                parts   = content.get("parts", [])
                if not parts:
                    finish = cand.get("finishReason", "")
                    print(f"[Gemini] {model} no parts, finishReason={finish}")
                    continue

                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    print(f"[Gemini] success with {model}")
                    return text
                print(f"[Gemini] {model} returned empty text")

            except requests.exceptions.Timeout:
                print(f"[Gemini] {tag} timeout")
            except Exception as e:
                print(f"[Gemini] {tag}: {e}")
    return None

def _try_groq(prompt: str, system: str, history: list) -> Optional[str]:
    cfg  = get_cfg()
    keys = cfg.get("groq_keys", [])
    if not keys:
        print("[Groq] no keys configured")
        return None

    msgs = _openai_messages(system, history, prompt)
    for model in GROQ_MODELS:
        for key in keys:
            tag = f"groq:{key[:8]}:{model}"
            if tag in _dead_keys: continue
            try:
                r = http.post(
                    GROQ_BASE,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model":       model,
                        "messages":    msgs,
                        "max_tokens":  2048,
                        "temperature": 0.7,
                    },
                    timeout=45,
                )
                if r.status_code in (401, 403):
                    print(f"[Groq] key dead ({r.status_code}): {r.text[:100]}")
                    _dead_keys.add(tag)
                    continue
                if r.status_code == 429:
                    print(f"[Groq] {model} rate limited, trying next model/key")
                    continue
                if r.status_code == 404:
                    print(f"[Groq] model {model} not found, trying next")
                    break  # try next model
                if r.status_code != 200:
                    print(f"[Groq] {model} HTTP {r.status_code}: {r.text[:200]}")
                    continue

                data    = r.json()
                choices = data.get("choices", [])
                if not choices:
                    print(f"[Groq] {model} empty choices")
                    continue
                text = choices[0].get("message", {}).get("content", "").strip()
                if text:
                    print(f"[Groq] success with {model}")
                    return text
                print(f"[Groq] {model} empty content")

            except requests.exceptions.Timeout:
                print(f"[Groq] {tag} timeout")
            except Exception as e:
                print(f"[Groq] {tag}: {e}")
    return None

def _try_mistral(prompt: str, system: str, history: list, image_b64: str = None) -> Optional[str]:
    cfg  = get_cfg()
    keys = cfg.get("mistral_keys", [])
    if not keys: return None

    if image_b64:
        msgs  = [{"role": "user", "content": [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": f"data:{_detect_mime(image_b64)};base64,{image_b64}"},
        ]}]
        model = "mistral-small-2506"
    else:
        msgs  = _openai_messages(system, history, prompt)
        model = "mistral-small-latest"

    for key in keys:
        tag = f"mistral:{key[:8]}"
        if tag in _dead_keys: continue
        try:
            r = http.post(
                MISTRAL_BASE,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": msgs, "max_tokens": 1024},
                timeout=60,
            )
            if r.status_code == 403: _dead_keys.add(tag); continue
            if r.status_code == 429: continue
            if r.status_code != 200:
                print(f"[Mistral] HTTP {r.status_code}: {r.text[:200]}")
                continue
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                print("[Mistral] success")
                return text
        except Exception as e:
            print(f"[Mistral] {tag}: {e}")
    return None

def ask_ai(prompt: str, history: list = None, image_b64: str = None,
           extra_context: str = None, firstname: str = None) -> str:
    history = history or []
    system  = VOID_SYSTEM
    if firstname and firstname != "there":
        system += f"\n\nThe user's name is {firstname}. Use it only when natural (greetings, personal moments) — not in every reply."
    if extra_context:
        system += f"\n\nWeb search context (use this to answer):\n{extra_context}"

    if image_b64:
        # Groq doesn't support vision natively — use Gemini first for images
        result = _try_gemini(prompt, system, history, image_b64)
        if result: return result
        result = _try_mistral(prompt, system, history, image_b64)
        if result: return result
        result = _try_groq(prompt, system, history)
        if result: return result
    else:
        # Text: Groq (Llama 3.1 8B) first → Gemini → Mistral
        result = _try_groq(prompt, system, history)
        if result: return result
        result = _try_gemini(prompt, system, history)
        if result: return result
        result = _try_mistral(prompt, system, history)
        if result: return result

    cfg = get_cfg()
    if not any([cfg.get("groq_keys"), cfg.get("gemini_keys"), cfg.get("mistral_keys")]):
        return "No API keys configured. Admin: use /addkey to add keys."
    return "All AI providers are temporarily unavailable. Please try again in a moment."

# =============================================================================
# REVERSE IMAGE SEARCH  (PicImageSearch via SearXNG image engine)
# =============================================================================
def reverse_image_search(img_bytes: bytes) -> Optional[str]:
    """
    Upload image to Telegraph, then do a SearXNG reverse image search.
    Returns raw text context from the top results, or None on failure.
    """
    if not SEARXNG_URL:
        print("[RevSearch] SearXNG not configured")
        return None
    try:
        # Upload to Telegraph to get a public URL
        pub_url = upload_telegraph(img_bytes)
        if not pub_url:
            print("[RevSearch] Telegraph upload failed")
            return None

        # SearXNG reverse image search using the public URL
        r = http.get(
            f"{SEARXNG_URL}/search",
            params={
                "q":       pub_url,
                "format":  "json",
                "engines": "google images,bing images,yandex images",
            },
            timeout=15,
        )
        r.raise_for_status()
        data    = r.json()
        results = data.get("results", [])[:8]

        if not results:
            print("[RevSearch] No results from reverse image search")
            return None

        lines = ["[REVERSE IMAGE SEARCH RESULTS]"]
        for i, item in enumerate(results, 1):
            title   = item.get("title", "")
            url     = item.get("url", "")
            snippet = item.get("content", "")
            if title: lines.append(f"{i}. {title}")
            if snippet: lines.append(f"   {snippet[:300]}")
            if url: lines.append(f"   Source: {url}")
            lines.append("")

        raw_data = "\n".join(lines)
        print(f"[RevSearch] Got {len(results)} results, {len(raw_data)} chars")
        return raw_data

    except Exception as e:
        print(f"[RevSearch] error: {e}")
        return None


def _mistral_summarize(text: str, system_prompt: str = None) -> Optional[str]:
    """Summarize text using Mistral AI (always Mistral, as required for Pro mode)."""
    cfg  = get_cfg()
    keys = cfg.get("mistral_keys", [])
    if not keys:
        print("[Mistral] no keys for summarization")
        return None

    system = system_prompt or (
        "You are a data summarizer. Given raw web search results about an image, "
        "extract and summarize the key information: what the image likely shows, "
        "the subject identity, context, and any important facts. Be concise and factual."
    )
    for key in keys:
        tag = f"mistral:{key[:8]}"
        if tag in _dead_keys: continue
        try:
            r = http.post(
                MISTRAL_BASE,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model":      "mistral-small-latest",
                    "messages":   [
                        {"role": "system",  "content": system},
                        {"role": "user",    "content": text[:4000]},
                    ],
                    "max_tokens": 600,
                },
                timeout=30,
            )
            if r.status_code == 403: _dead_keys.add(tag); continue
            if r.status_code == 429: continue
            if r.status_code != 200:
                print(f"[Mistral Summarize] HTTP {r.status_code}")
                continue
            text_out = r.json()["choices"][0]["message"]["content"].strip()
            if text_out:
                print("[Mistral] summarization success")
                return text_out
        except Exception as e:
            print(f"[Mistral Summarize] {e}")
    return None
def download_tg_image(file_id: str) -> Optional[bytes]:
    res = tg("getFile", {"file_id": file_id})
    if not res.get("ok"): return None
    file_path = res["result"]["file_path"]
    file_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    try:
        r = http.get(file_url, timeout=40)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"[IMG] download error: {e}")
        return None

def upload_telegraph(img_bytes: bytes) -> Optional[str]:
    try:
        r = http.post(
            "https://telegra.ph/upload",
            files={"file": ("image.jpg", img_bytes, "image/jpeg")},
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                path = data[0].get("src", "")
                if path: return "https://telegra.ph" + path
    except Exception as e:
        print(f"[Telegraph] upload error: {e}")
    return None

# =============================================================================
# VOICE  (edge-tts)
# Uses subprocess to avoid asyncio event loop conflicts in threads
# =============================================================================
INDIAN_VOICES = {
    "Hindi Female (Swara)":    "hi-IN-SwaraNeural",
    "Hindi Male (Madhur)":     "hi-IN-MadhurNeural",
    "English Female (Neerja)": "en-IN-NeerjaNeural",
    "English Male (Prabhat)":  "en-IN-PrabhatNeural",
    "Tamil Female (Pallavi)":  "ta-IN-PallaviNeural",
    "Tamil Male (Valluvar)":   "ta-IN-ValluvarNeural",
    "Telugu Female (Shruti)":  "te-IN-ShrutiNeural",
    "Telugu Male (Mohan)":     "te-IN-MohanNeural",
    "Kannada Female (Sapna)":  "kn-IN-SapnaNeural",
    "Kannada Male (Gagan)":    "kn-IN-GaganNeural",
}

VOICE_CLEAN_RE = re.compile(
    "["
    u"\U0001F600-\U0001F64F"
    u"\U0001F300-\U0001F5FF"
    u"\U0001F680-\U0001F6FF"
    u"\U0001F1E0-\U0001F1FF"
    u"\U00002702-\U000027B0"
    u"\U000024C2-\U0001F251"
    u"\u2640-\u2642"
    u"\u2600-\u2B55"
    u"\ufe0f\u200d\u23cf\u23e9\u231a\u3030"
    "]+",
    flags=re.UNICODE,
)

def clean_for_tts(text: str) -> str:
    # Strip markdown and HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\*+|_+|`+|#+|~+", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = VOICE_CLEAN_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1200]

# Inline async TTS script — run via subprocess to avoid event loop issues
_TTS_SCRIPT = """
import asyncio, sys, edge_tts

async def run(text, voice, out_path):
    comm = edge_tts.Communicate(text, voice)
    await comm.save(out_path)

text     = sys.argv[1]
voice    = sys.argv[2]
out_path = sys.argv[3]
asyncio.run(run(text, voice, out_path))
"""

def tts_to_bytes(text: str, voice: str) -> Optional[bytes]:
    """Generate TTS audio via subprocess — works reliably in threads."""
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".oga", delete=False)
        tmp.close()

        # Write helper script to temp file
        script_tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w")
        script_tmp.write(_TTS_SCRIPT)
        script_tmp.close()

        result = subprocess.run(
            [sys.executable, script_tmp.name, text, voice, tmp.name],
            timeout=30,
            capture_output=True,
            text=True,
        )

        os.unlink(script_tmp.name)

        if result.returncode != 0:
            print(f"[TTS] subprocess error: {result.stderr}")
            os.unlink(tmp.name)
            return None

        size = os.path.getsize(tmp.name)
        if size < 100:
            print(f"[TTS] file too small: {size} bytes")
            os.unlink(tmp.name)
            return None

        with open(tmp.name, "rb") as f:
            data = f.read()
        os.unlink(tmp.name)
        print(f"[TTS] generated {len(data)} bytes for voice={voice}")
        return data

    except subprocess.TimeoutExpired:
        print("[TTS] timeout")
        return None
    except Exception as e:
        print(f"[TTS] error: {e}")
        return None

def send_voice_reply(chat_id: int, text: str, voice_name: str):
    clean = clean_for_tts(text)
    if not clean:
        send_msg(chat_id, text)
        return

    audio = tts_to_bytes(clean, voice_name)
    if audio:
        caption = text[:900] if len(text) <= 900 else text[:897] + "…"
        res = tg(
            "sendVoice",
            {"chat_id": chat_id, "caption": caption},
            files={"voice": ("voice.oga", audio, "audio/ogg")},
            timeout=60,
        )
        if res.get("ok"):
            return
        print(f"[Voice] sendVoice failed: {res.get('description','')}")
    # fallback to text
    send_msg(chat_id, safe_send_text(text))

# =============================================================================
# INLINE KEYBOARDS  (only for menus, NOT attached to AI answers)
# =============================================================================
def switch_markup(uid: int) -> dict:
    current = get_model(uid)
    rows = []
    for key, label in [("lite", "Void Lite — fast"), ("flash", "Void Flash — balanced"), ("pro", "Void Pro — web AI")]:
        tick = " ✅" if key == current else ""
        rows.append([{"text": f"{label}{tick}", "callback_data": f"switch:{key}"}])
    return {"inline_keyboard": rows}

def voice_markup(uid: int) -> dict:
    voice_on, current = get_voice(uid)
    rows = []
    for display, code in INDIAN_VOICES.items():
        tick = " ✅" if code == current else ""
        rows.append([{"text": f"{display}{tick}", "callback_data": f"voice:{code}"}])
    state_text = "Turn OFF Voice" if voice_on else "Turn ON Voice"
    rows.append([{"text": state_text, "callback_data": "voice:toggle"}])
    return {"inline_keyboard": rows}

def admin_markup() -> dict:
    return {"inline_keyboard": [
        [
            {"text": "🔑 API Keys",    "callback_data": "admin:keys"},
            {"text": "📊 Stats",       "callback_data": "admin:stats"},
        ],
        [
            {"text": "🚫 Banned",      "callback_data": "admin:bans"},
            {"text": "⚡ Limits",      "callback_data": "admin:limits"},
        ],
    ]}

# =============================================================================
# COMMAND HANDLERS
# =============================================================================
def handle_start(chat_id: int, uid: int, name: str):
    set_firstname(uid, name)
    model  = get_model(uid)
    labels = {"lite": "Void Lite ⚡", "flash": "Void Flash ✨", "pro": "Void Pro 🌐"}
    send_msg(chat_id,
        f"👋 <b>Hey {esc(name)}! Welcome to VoidAI.</b>\n\n"
        f"Current mode: <b>{labels[model]}</b>\n\n"
        "Send any message and I'll answer.\n"
        "📷 Send a photo for image analysis.\n\n"
        "<b>Commands:</b>\n"
        "/switch — Change AI model\n"
        "/new — Clear chat history\n"
        "/web query — Web search\n"
        "/voice — Toggle voice replies\n"
        "/usage — View your usage & limits\n"
        "/summarize — Summarize a replied message\n"
        "/help — All commands"
    )

def handle_help(chat_id: int):
    send_msg(chat_id,
        "<b>VoidAI — Commands</b>\n\n"
        "💬 <b>Chat</b>\n"
        "/start — Welcome message\n"
        "/new — Clear chat history\n"
        "/switch — Change AI model\n\n"
        "🔍 <b>Search</b>\n"
        "/web &lt;query&gt; — Web search with pretty results\n"
        "  └ Buttons: More Results · AI Answer · Re-search\n\n"
        "📝 <b>Tools</b>\n"
        "/summarize — Reply to any message to summarize it\n"
        "/voice — Toggle voice replies &amp; pick language\n\n"
        "📊 <b>Usage &amp; Limits</b>\n"
        "/usage — View your hourly usage &amp; time until reset\n\n"
        "🖼️ <b>Images</b>\n"
        "Send any photo → AI analyzes it\n"
        "  └ Buttons: Describe · OCR · Style · Mood · Facts\n\n"
        "⚙️ <b>Models</b>\n"
        "  Void Lite ⚡ — Fast, no web\n"
        "  Void Flash ✨ — Balanced (default)\n"
        "  Void Pro 🌐 — Auto web search every reply\n\n"
        "/help — This menu"
    )

def handle_new(chat_id: int, uid: int):
    reset_sess(uid)
    send_msg(chat_id, "Chat history cleared.")

def handle_switch(chat_id: int, uid: int):
    send_msg_markup(chat_id, "Select AI model:", switch_markup(uid))

def handle_voice_cmd(chat_id: int, uid: int):
    voice_on, _ = get_voice(uid)
    state = "ON" if voice_on else "OFF"
    send_msg_markup(chat_id, f"Voice replies: <b>{state}</b>\nSelect a voice:", voice_markup(uid))

def _format_web_results_html(query: str, results: List[Dict], page: int = 1) -> str:
    """Pretty-print web search results as Telegram HTML."""
    if not results:
        return f"🔍 No results found for <b>{esc(query)}</b>"

    lines = [f"🔍 <b>Search:</b> {esc(query)}\n"]
    for i, r in enumerate(results, 1):
        title   = esc(r.get("title", "Untitled"))
        url     = r.get("link", "")
        snippet = esc((r.get("snippet") or "")[:200])
        lines.append(f"<b>{i}. {title}</b>")
        if snippet:
            lines.append(f"   <i>{snippet}</i>")
        if url:
            lines.append(f'   🔗 <a href="{url}">{esc(url[:60])}…</a>')
        lines.append("")

    lines.append(f"<i>Page {page} · Tap below for more</i>")
    return "\n".join(lines)

def _web_results_markup(query: str, page: int, has_more: bool) -> dict:
    """Inline buttons for web search results."""
    rows = []
    if has_more:
        rows.append([{"text": "📄 More Results", "callback_data": f"web_more:{page+1}:{query[:80]}"}])
    rows.append([
        {"text": "🤖 AI Answer",   "callback_data": f"web_ai:{query[:80]}"},
        {"text": "🔄 Re-search",   "callback_data": f"web_redo:{query[:80]}"},
    ])
    return {"inline_keyboard": rows}

def handle_web_cmd(chat_id: int, uid: int, query: str):
    if not query.strip():
        send_msg(chat_id, "Usage: /web your search query")
        return
    if not check_limit(uid, "searches"):
        send_msg(chat_id, limit_exceeded_msg(uid, "searches"))
        return
    record_usage(uid, "searches")

    msg_id, stop = start_typing(chat_id)
    stop.set()

    results = web_search(query, max_results=5)
    if not results:
        edit_msg(chat_id, msg_id, f"❌ No results found for: <b>{esc(query)}</b>")
        return

    html    = _format_web_results_html(query, results, page=1)
    markup  = _web_results_markup(query, page=1, has_more=len(results) >= 5)

    payload = {
        "chat_id":                  chat_id,
        "message_id":               msg_id,
        "text":                     html[:4000],
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
        "reply_markup":             json.dumps(markup),
    }
    tg("editMessageText", payload, timeout=20)


def handle_summarize(chat_id: int, uid: int, replied_text: str):
    """Summarize a replied message."""
    if not replied_text or not replied_text.strip():
        send_msg(chat_id, "↩️ Reply to a message with /summarize to summarize it.")
        return

    msg_id, stop = start_typing(chat_id)
    prompt = f"Summarize the following text in a concise, clear way:\n\n{replied_text[:3000]}"

    answer = ask_ai(
        prompt=prompt,
        history=[],
        firstname=get_firstname(uid),
    )
    typewriter_edit(chat_id, msg_id, "📝 <b>Summary:</b>\n\n" + answer, stop)


# =============================================================================
# QUESTION HANDLER
# =============================================================================
def handle_question(chat_id: int, uid: int, text: str, force_web: bool = False):
    if not check_limit(uid, "messages"):
        send_msg(chat_id, limit_exceeded_msg(uid, "messages"))
        return
    record_usage(uid, "messages")

    msg_id, stop = start_typing(chat_id)
    try:
        model         = get_model(uid)
        extra_context = None

        if model == "pro" or force_web:
            if check_limit(uid, "searches"):
                record_usage(uid, "searches")
                results = web_search(text, max_results=5)
                if results:
                    extra_context = format_context(text, results)
                    print(f"[Search] context built: {len(extra_context)} chars")

        answer = ask_ai(
            prompt=text,
            history=get_history(uid),
            extra_context=extra_context,
            firstname=get_firstname(uid),
        )

        add_history(uid, "user",      text)
        add_history(uid, "assistant", answer)

        voice_on, voice_name = get_voice(uid)
        if voice_on:
            stop.set()
            tg("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
            send_voice_reply(chat_id, answer, voice_name)
        else:
            typewriter_edit(chat_id, msg_id, answer, stop)

    except Exception as e:
        stop.set()
        print(f"[Question] error: {e}")
        edit_msg(chat_id, msg_id, f"❌ Error: {esc(str(e))}")

# =============================================================================
# IMAGE STORE  (short-lived in-memory store for action buttons)
# =============================================================================
_img_store: Dict[str, str] = {}
_img_order: List[str]      = []

def _store_img(b64: str) -> str:
    import hashlib
    key = hashlib.md5(b64[:256].encode()).hexdigest()[:10]
    if key not in _img_store:
        _img_store[key] = b64
        _img_order.append(key)
        if len(_img_order) > 30:
            _img_store.pop(_img_order.pop(0), None)
    return key

def _photo_action_markup(key: str) -> dict:
    return {"inline_keyboard": [
        [
            {"text": "🔍 Describe More",  "callback_data": f"img:describe:{key}"},
            {"text": "📝 Extract Text",   "callback_data": f"img:ocr:{key}"},
        ],
        [
            {"text": "🎨 Identify Style", "callback_data": f"img:style:{key}"},
            {"text": "🌐 Search Similar", "callback_data": f"img:search:{key}"},
        ],
        [
            {"text": "😄 Describe Mood",  "callback_data": f"img:mood:{key}"},
            {"text": "💡 Fun Fact",       "callback_data": f"img:fact:{key}"},
        ],
    ]}

# =============================================================================
# IMAGE HANDLER
# =============================================================================
def handle_image(chat_id: int, uid: int, photo_sizes: list, caption: str = ""):
    if not check_limit(uid, "images"):
        send_msg(chat_id, limit_exceeded_msg(uid, "images"))
        return
    record_usage(uid, "images")

    # Use upload_photo action during image analysis
    msg_id, stop = start_typing(chat_id, action="upload_photo")
    try:
        img_bytes = download_tg_image(photo_sizes[-1]["file_id"])
        if not img_bytes:
            stop.set()
            edit_msg(chat_id, msg_id, "❌ Could not download image. Try again.")
            return

        image_b64 = base64.b64encode(img_bytes).decode()
        img_key   = _store_img(image_b64)
        user_q    = caption or ""
        model     = get_model(uid)

        # ── Show progress animation ──────────────────────────────────
        stop.set()  # stop typing indicator
        _show_progress(chat_id, msg_id)

        extra_ctx = None

        if model == "pro":
            # Pro pipeline:
            # 1) Reverse image search → raw data
            # 2) Mistral summarizes raw data
            # 3) SearXNG search on summary
            # 4) Combined context → primary AI
            tg("editMessageText", {
                "chat_id":    chat_id,
                "message_id": msg_id,
                "text":       "🔍 <i>Running reverse image search…</i>",
                "parse_mode": "HTML",
            }, timeout=10)

            raw_data = reverse_image_search(img_bytes)

            if raw_data:
                tg("editMessageText", {
                    "chat_id":    chat_id,
                    "message_id": msg_id,
                    "text":       "🧠 <i>Summarizing search data with Mistral…</i>",
                    "parse_mode": "HTML",
                }, timeout=10)

                summary = _mistral_summarize(raw_data)

                if summary:
                    tg("editMessageText", {
                        "chat_id":    chat_id,
                        "message_id": msg_id,
                        "text":       "🌐 <i>Searching for more context…</i>",
                        "parse_mode": "HTML",
                    }, timeout=10)

                    search_results = web_search(summary[:200], max_results=5)
                    parts = [f"[IMAGE REVERSE SEARCH SUMMARY]\n{summary}"]
                    if search_results:
                        parts.append(format_context(summary[:100], search_results))
                    extra_ctx = "\n\n".join(parts)

            # Reset progress before final AI call
            tg("editMessageText", {
                "chat_id":    chat_id,
                "message_id": msg_id,
                "text":       "🤖 <i>Analyzing with AI…</i>",
                "parse_mode": "HTML",
            }, timeout=10)

        elif model in ("lite", "flash"):
            # Non-pro: basic reverse search for context, AI does the heavy lifting
            raw_data = reverse_image_search(img_bytes)
            if raw_data:
                extra_ctx = raw_data

        # Build prompt
        if user_q:
            prompt = f"{user_q}\n\n[Use any image context provided to give the best answer.]"
        else:
            prompt = (
                "Analyze this image thoroughly. Using any reverse search context provided, "
                "identify the subject, describe what you see (objects, colors, mood, text, composition), "
                "and share any relevant facts or context."
            )

        answer = ask_ai(
            prompt=prompt,
            history=get_history(uid),
            image_b64=image_b64,
            extra_context=extra_ctx,
            firstname=get_firstname(uid),
        )

        add_history(uid, "user",      f"[Image] {user_q or 'Analyze this image'}")
        add_history(uid, "assistant", answer)

        voice_on, voice_name = get_voice(uid)
        if voice_on:
            tg("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
            send_voice_reply(chat_id, answer, voice_name)
        else:
            formatted = safe_send_text("🖼️ <b>Image Analysis</b>\n\n" + answer)
            tg("editMessageText", {
                "chat_id":                  chat_id,
                "message_id":               msg_id,
                "text":                     formatted[:4000],
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
                "reply_markup":             json.dumps(_photo_action_markup(img_key)),
            }, timeout=20)

    except Exception as e:
        stop.set()
        print(f"[Image] error: {e}")
        edit_msg(chat_id, msg_id, f"❌ Image error: {esc(str(e))}")

# =============================================================================
# ADMIN HANDLERS
# =============================================================================
def handle_admin(chat_id: int, uid: int):
    if not is_admin(uid):
        send_msg(chat_id, "⛔ Admin access required.")
        return
    cfg = get_cfg()
    g_keys = len(cfg.get('gemini_keys', []))
    q_keys = len(cfg.get('groq_keys', []))
    m_keys = len(cfg.get('mistral_keys', []))
    admins = len(cfg.get('admin_ids', []))
    banned = len(cfg.get('banned_users', []))
    total_keys = g_keys + q_keys + m_keys

    def _dot(n): return "🟢" if n > 0 else "🔴"

    send_msg_markup(chat_id,
        "╔══════════════════════╗\n"
        "║   🛡️  <b>VoidAI Admin</b>     ║\n"
        "╚══════════════════════╝\n\n"
        "━━━ <b>API Keys</b> ━━━━━━━━━━━━\n"
        f"{_dot(g_keys)} Gemini   — <b>{g_keys}</b> key(s)\n"
        f"{_dot(q_keys)} Groq     — <b>{q_keys}</b> key(s)\n"
        f"{_dot(m_keys)} Mistral  — <b>{m_keys}</b> key(s)\n\n"
        "━━━ <b>Users</b> ━━━━━━━━━━━━━━\n"
        f"👑 Admins  — <b>{admins}</b>\n"
        f"🚫 Banned  — <b>{banned}</b>\n\n"
        "━━━ <b>Quick Commands</b> ━━━━━\n"
        "<code>/addkey gemini|groq|mistral KEY</code>\n"
        "<code>/delkey gemini|groq|mistral INDEX</code>\n"
        "<code>/listkeys</code>  <code>/addadmin ID</code>  <code>/ban ID</code>\n"
        "<code>/setlimit TIER ACTION VALUE</code>\n"
        "<code>/stats</code>",
        admin_markup(),
    )

def handle_addkey(chat_id: int, uid: int, args: list, msg_id_to_delete: int = None):
    if not is_admin(uid):
        send_msg(chat_id, "Admin access required."); return
    if len(args) < 2:
        send_msg(chat_id, "Usage: /addkey gemini|groq|mistral YOUR_KEY"); return
    provider, key = args[0].lower(), args[1].strip()
    key_map = {"gemini": "gemini_keys", "groq": "groq_keys", "mistral": "mistral_keys"}
    if provider not in key_map:
        send_msg(chat_id, "Unknown provider. Use: gemini, groq, mistral"); return
    cfg   = get_cfg()
    field = key_map[provider]
    if key in cfg[field]:
        send_msg(chat_id, "Key already exists.")
    else:
        cfg[field].append(key)
        save_cfg(cfg)
        send_msg(chat_id, f"{provider.capitalize()} key added. Total: {len(cfg[field])}")
    # Delete the message containing the key for security
    if msg_id_to_delete:
        tg("deleteMessage", {"chat_id": chat_id, "message_id": msg_id_to_delete})

def handle_delkey(chat_id: int, uid: int, args: list):
    if not is_admin(uid):
        send_msg(chat_id, "Admin access required."); return
    key_map = {"gemini": "gemini_keys", "groq": "groq_keys", "mistral": "mistral_keys"}
    if len(args) < 2:
        cfg = get_cfg()
        lines = ["<b>API Keys</b> (use /delkey provider index)\n"]
        for p, f in key_map.items():
            keys = cfg.get(f, [])
            lines.append(f"<b>{p.capitalize()}</b>: {len(keys)} key(s)")
            for i, k in enumerate(keys, 1):
                lines.append(f"  {i}. ...{k[-8:]}")
        send_msg(chat_id, "\n".join(lines)); return
    provider = args[0].lower()
    if provider not in key_map:
        send_msg(chat_id, "Unknown provider."); return
    try:
        index = int(args[1]) - 1
    except ValueError:
        send_msg(chat_id, "Index must be a number."); return
    cfg   = get_cfg()
    field = key_map[provider]
    keys  = cfg.get(field, [])
    if index < 0 or index >= len(keys):
        send_msg(chat_id, f"Invalid index. {provider} has {len(keys)} key(s)."); return
    removed = keys.pop(index)
    cfg[field] = keys
    save_cfg(cfg)
    send_msg(chat_id, f"Deleted {provider} key ...{removed[-8:]}. Remaining: {len(keys)}")

def handle_listkeys(chat_id: int, uid: int):
    if not is_admin(uid):
        send_msg(chat_id, "Admin access required."); return
    cfg = get_cfg()
    lines = ["<b>API Keys</b>\n"]
    for p, f in [("Gemini","gemini_keys"),("Groq","groq_keys"),("Mistral","mistral_keys")]:
        keys = cfg.get(f, [])
        lines.append(f"<b>{p}</b>: {len(keys)}")
        for i, k in enumerate(keys, 1):
            lines.append(f"  {i}. ...{k[-8:]}")
    send_msg(chat_id, "\n".join(lines))

def handle_addadmin(chat_id: int, uid: int, args: list):
    if not is_admin(uid):
        send_msg(chat_id, "Admin access required."); return
    if not args:
        send_msg(chat_id, "Usage: /addadmin USER_ID"); return
    try:
        target = int(args[0])
    except ValueError:
        send_msg(chat_id, "Invalid user ID."); return
    cfg = get_cfg()
    if target not in cfg["admin_ids"]:
        cfg["admin_ids"].append(target)
        save_cfg(cfg)
        send_msg(chat_id, f"User {target} is now an admin.")
    else:
        cfg["admin_ids"].remove(target)
        save_cfg(cfg)
        send_msg(chat_id, f"User {target} admin access removed.")

def handle_ban(chat_id: int, uid: int, args: list, reply_uid: int = None):
    if not is_admin(uid): return
    target = reply_uid
    if not target and args:
        try: target = int(args[0])
        except ValueError: pass
    if not target:
        send_msg(chat_id, "Usage: /ban USER_ID or reply to a message"); return
    cfg = get_cfg()
    if target not in cfg["banned_users"]:
        cfg["banned_users"].append(target)
        save_cfg(cfg)
        send_msg(chat_id, f"User {target} banned.")
    else:
        cfg["banned_users"].remove(target)
        save_cfg(cfg)
        send_msg(chat_id, f"User {target} unbanned.")

def handle_usage(chat_id: int, uid: int):
    """Show detailed usage dashboard for the user."""
    model    = get_model(uid)
    stats    = get_usage_stats(uid)
    mult     = _get_multiplier(uid)
    name     = get_firstname(uid)
    tier_lbl = {"lite": "Void Lite ⚡", "flash": "Void Flash ✨", "pro": "Void Pro 🌐"}.get(model, model)
    now      = time.time()

    def bar(used, limit):
        if limit == 0: return "░░░░░░░░░░ 0%"
        pct  = min(used / limit, 1.0)
        fill = int(pct * 10)
        bar  = "█" * fill + "░" * (10 - fill)
        return f"{bar} {int(pct*100)}%"

    def status_icon(used, limit):
        if limit == 0: return "⚫"
        pct = used / limit
        if pct >= 1.0: return "🔴"
        if pct >= 0.8: return "🟡"
        return "🟢"

    msgs = stats["messages"]
    imgs = stats["images"]
    srch = stats["searches"]

    # Calculate global reset — when will the window fully clear (oldest stamp + 1hr)
    all_stamps = []
    for action in ["messages", "images", "searches"]:
        all_stamps += _usage.get(uid, {}).get(action, [])
    if all_stamps:
        full_reset_in = int((min(all_stamps) + LIMIT_WINDOW) - now)
        full_reset_in = max(0, full_reset_in)
        reset_str = fmt_time_left(full_reset_in)
    else:
        reset_str = "No usage yet"

    mult_line = f"✨ <b>Multiplier:</b> ×{mult}\n" if mult != 1.0 else ""

    lines = [
        f"📊 <b>Usage Dashboard — {esc(name)}</b>",
        f"",
        f"🏷️ Plan: <b>{tier_lbl}</b>   {mult_line.strip()}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"{status_icon(msgs['used'], msgs['limit'])} <b>💬 Messages</b>",
        f"   {msgs['used']} / {msgs['limit']} used",
        f"   <code>{bar(msgs['used'], msgs['limit'])}</code>",
        f"   🕐 Resets in: <b>{fmt_time_left(msgs['reset_in_sec']) if msgs['used'] > 0 else 'N/A'}</b>",
        f"",
        f"{status_icon(imgs['used'], imgs['limit'])} <b>🖼️ Images</b>",
        f"   {imgs['used']} / {imgs['limit']} used",
        f"   <code>{bar(imgs['used'], imgs['limit'])}</code>",
        f"   🕐 Resets in: <b>{fmt_time_left(imgs['reset_in_sec']) if imgs['used'] > 0 else 'N/A'}</b>",
        f"",
        f"{status_icon(srch['used'], srch['limit'])} <b>🔍 Web Searches</b>",
        f"   {srch['used']} / {srch['limit']} used",
        f"   <code>{bar(srch['used'], srch['limit'])}</code>",
        f"   🕐 Resets in: <b>{fmt_time_left(srch['reset_in_sec']) if srch['used'] > 0 else 'N/A'}</b>",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"🔄 <b>Full limit reset in:</b> <b>{reset_str}</b>",
        f"<i>All limits reset on a rolling 1-hour window</i>",
    ]

    if mult_line:
        lines.insert(3, mult_line.rstrip())

    send_msg(chat_id, "\n".join(lines))


def handle_limit(chat_id: int, uid: int, args: list, reply_uid: int = None):
    if not is_admin(uid): return
    cfg = get_cfg()

    def _parse_mult(s: str) -> Optional[float]:
        """Parse '2x', '2X', '1.5', '0.5x' → float or None."""
        s = s.strip().rstrip("xX")
        try: return float(s)
        except ValueError: return None

    # Reply-based: /limit 2x  while replying to a user → multiply that user's limit
    if reply_uid and len(args) == 1:
        mult = _parse_mult(args[0])
        if mult is None:
            send_msg(chat_id, "Usage: reply to a user and send /limit MULTIPLIER (e.g. /limit 2x)"); return
        current  = float(cfg["user_limit_multipliers"].get(str(reply_uid), 1))
        new_mult = round(current * mult, 2)
        cfg["user_limit_multipliers"][str(reply_uid)] = new_mult
        save_cfg(cfg)
        send_msg(chat_id,
            f"✅ <b>Limit updated for user {reply_uid}</b>\n\n"
            f"Previous: ×{current}\n"
            f"Multiplier applied: ×{mult}\n"
            f"<b>New limit: ×{new_mult}</b>\n\n"
            f"<i>All their hourly limits are now {new_mult}× the base</i>"
        )
        return

    if len(args) == 2:
        # /limit USER_ID MULTIPLIER
        try:    target = int(args[0])
        except ValueError:
            send_msg(chat_id, "Invalid user ID."); return
        mult = _parse_mult(args[1])
        if mult is None:
            send_msg(chat_id, "Invalid multiplier."); return
        cfg["user_limit_multipliers"][str(target)] = mult
        save_cfg(cfg)
        send_msg(chat_id,
            f"✅ User <b>{target}</b> limit set to <b>×{mult}</b>\n"
            f"<i>All hourly limits are {mult}× the base for this user</i>"
        )
    elif len(args) == 1:
        mult = _parse_mult(args[0])
        if mult is None:
            send_msg(chat_id, "Invalid multiplier. Use e.g. /limit 2x or /limit 1.5"); return
        cfg["user_limit_multipliers"]["global"] = mult
        save_cfg(cfg)
        send_msg(chat_id,
            f"✅ <b>Global limit multiplier set to ×{mult}</b>\n"
            f"<i>Applies to all users without a custom multiplier</i>"
        )
    else:
        send_msg(chat_id,
            "<b>⚡ /limit — Manage Rate Limits</b>\n\n"
            "↩️ <b>Reply to user</b> + <code>/limit 2x</code> — multiply that user's limit by 2\n"
            "<code>/limit USER_ID MULTIPLIER</code> — set per-user multiplier\n"
            "<code>/limit MULTIPLIER</code> — set global multiplier\n\n"
            "Multiplier examples: <code>2x</code>, <code>0.5x</code>, <code>1.5</code>\n\n"
            "<b>Set per-tier base limits:</b>\n"
            "<code>/setlimit TIER ACTION VALUE</code>\n"
            "  e.g. <code>/setlimit pro images 20</code>\n"
            "  Tiers: lite flash pro | Actions: messages images searches"
        )

def handle_setlimit(chat_id: int, uid: int, args: list):
    """Admin: /setlimit TIER ACTION VALUE — e.g. /setlimit pro images 20"""
    if not is_admin(uid): return
    if len(args) < 3:
        send_msg(chat_id,
            "Usage: /setlimit TIER ACTION VALUE\n"
            "Tiers: lite, flash, pro\n"
            "Actions: messages, images, searches\n"
            "Example: /setlimit pro images 20"
        ); return
    tier, action, val_str = args[0].lower(), args[1].lower(), args[2]
    if tier not in ("lite", "flash", "pro"):
        send_msg(chat_id, "Invalid tier. Use: lite, flash, pro"); return
    if action not in ("messages", "images", "searches"):
        send_msg(chat_id, "Invalid action. Use: messages, images, searches"); return
    try:
        value = int(val_str)
    except ValueError:
        send_msg(chat_id, "Value must be an integer."); return
    cfg = get_cfg()
    cfg.setdefault("limits", {}).setdefault(tier, {})[action] = value
    save_cfg(cfg)
    send_msg(chat_id, f"✅ <b>{tier.capitalize()}</b> {action} limit set to <b>{value}/hour</b>")

def handle_stats(chat_id: int, uid: int):
    if not is_admin(uid): return
    cfg        = get_cfg()
    total_msgs = sum(len([t for t in d.get("messages",[]) if t > _window()]) for d in _usage.values())
    total_srch = sum(len([t for t in d.get("searches",[]) if t > _window()]) for d in _usage.values())
    total_imgs = sum(len([t for t in d.get("images",  []) if t > _window()]) for d in _usage.values())
    g_keys = len(cfg.get('gemini_keys', []))
    q_keys = len(cfg.get('groq_keys', []))
    m_keys = len(cfg.get('mistral_keys', []))
    def _dot(n): return "🟢" if n > 0 else "🔴"
    send_msg(chat_id,
        "📊 <b>Stats</b> <i>(last hour)</i>\n\n"
        "━━━ <b>Activity</b> ━━━━━━━━━━━━\n"
        f"👥 Active users  — <b>{len(_usage)}</b>\n"
        f"💬 Messages      — <b>{total_msgs}</b>\n"
        f"🔍 Searches      — <b>{total_srch}</b>\n"
        f"🖼️ Images        — <b>{total_imgs}</b>\n"
        f"🚫 Banned        — <b>{len(cfg.get('banned_users',[]))}</b>\n\n"
        "━━━ <b>API Keys</b> ━━━━━━━━━━━━\n"
        f"{_dot(g_keys)} Gemini  — <b>{g_keys}</b>\n"
        f"{_dot(q_keys)} Groq    — <b>{q_keys}</b>\n"
        f"{_dot(m_keys)} Mistral — <b>{m_keys}</b>"
    )

# =============================================================================
# CALLBACK HANDLER
# =============================================================================
def _run_img_action(chat_id: int, uid: int, msg_id: int, action: str, img_key: str):
    """Run an image action in a background thread and edit the message."""
    image_b64 = _img_store.get(img_key)
    if not image_b64:
        edit_msg(chat_id, msg_id, "❌ Image expired. Please send it again.")
        return

    prompts = {
        "describe": "Give a very detailed description of everything you see in this image: objects, people, text, colors, composition, background, lighting, and any fine details.",
        "ocr":      "Extract and transcribe all text visible in this image exactly as written. If there's no text, say so.",
        "style":    "Analyze the artistic and visual style of this image: art style, photography technique, color palette, mood, era, and any artistic influences you can identify.",
        "mood":     "Describe the emotional mood, atmosphere, and feelings this image evokes. What story does it tell?",
        "fact":     "Give 3 interesting or surprising facts related to the main subject of this image.",
        "search":   "What would be the best search query to find more images like this one? Also describe what makes this image unique.",
    }

    prompt = prompts.get(action, "Analyze this image.")
    stop   = threading.Event()
    stop.set()  # no typing loop needed, message already exists

    answer = ask_ai(
        prompt=prompt,
        history=[],
        image_b64=image_b64,
        firstname=get_firstname(uid),
    )

    labels = {
        "describe": "🔍 Detailed Description",
        "ocr":      "📝 Extracted Text",
        "style":    "🎨 Style Analysis",
        "mood":     "😄 Mood & Atmosphere",
        "fact":     "💡 Fun Facts",
        "search":   "🌐 Search Tips",
    }
    header    = labels.get(action, "🖼️ Analysis")
    formatted = safe_send_text(f"<b>{header}</b>\n\n{answer}")
    tg("editMessageText", {
        "chat_id":                  chat_id,
        "message_id":               msg_id,
        "text":                     formatted[:4000],
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
        "reply_markup":             json.dumps(_photo_action_markup(img_key)),
    }, timeout=20)


def _run_web_more(chat_id: int, uid: int, msg_id: int, page: int, query: str, bypass_cache: bool = False):
    """Fetch a specific page of search results fresh from SearXNG."""
    # Show a brief "loading" indicator
    tg("editMessageText", {
        "chat_id":    chat_id,
        "message_id": msg_id,
        "text":       f"🔍 <i>Fetching page {page} results…</i>",
        "parse_mode": "HTML",
    }, timeout=10)

    results = web_search(query, max_results=5, page=page, bypass_cache=bypass_cache)
    if not results:
        tg("editMessageText", {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "text":       f"📭 No more results for <b>{esc(query)}</b>",
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": [[
                {"text": "🔄 Re-search", "callback_data": f"web_redo:{query[:80]}"},
            ]]}),
        }, timeout=15)
        return

    html_text = _format_web_results_html(query, results, page=page)
    markup    = _web_results_markup(query, page=page, has_more=len(results) >= 5)
    tg("editMessageText", {
        "chat_id":                  chat_id,
        "message_id":               msg_id,
        "text":                     html_text[:4000],
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
        "reply_markup":             json.dumps(markup),
    }, timeout=20)


def _run_web_ai(chat_id: int, uid: int, msg_id: int, query: str):
    """Get AI answer for the web search query."""
    results   = web_search(query, max_results=5)
    extra_ctx = format_context(query, results) if results else None
    answer    = ask_ai(
        prompt=query,
        history=get_history(uid),
        extra_context=extra_ctx,
        firstname=get_firstname(uid),
    )
    add_history(uid, "user",      query)
    add_history(uid, "assistant", answer)

    formatted = safe_send_text(f"🤖 <b>AI Answer</b> for: <i>{esc(query)}</i>\n\n{answer}")
    tg("editMessageText", {
        "chat_id":                  chat_id,
        "message_id":               msg_id,
        "text":                     formatted[:4000],
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }, timeout=20)


def handle_callback(cb_id: str, uid: int, chat_id: int, msg_id: int, data: str):
    answer_cb(cb_id)

    # ── Model switch ────────────────────────────────────────────────
    if data.startswith("switch:"):
        model  = data.split(":")[1]
        set_model(uid, model)
        labels = {"lite": "Void Lite ⚡", "flash": "Void Flash ✨", "pro": "Void Pro 🌐"}
        edit_msg_markup(chat_id, msg_id,
            f"✅ Switched to <b>{labels.get(model, model)}</b>.\nSend a message to start.",
            switch_markup(uid))

    # ── Voice settings ──────────────────────────────────────────────
    elif data.startswith("voice:"):
        code = data.split(":", 1)[1]
        if code == "toggle":
            voice_on, vn = get_voice(uid)
            set_voice(uid, not voice_on, vn)
            new_state = "ON 🔊" if not voice_on else "OFF 🔇"
            edit_msg_markup(chat_id, msg_id,
                f"Voice replies turned <b>{new_state}</b>.",
                voice_markup(uid))
        else:
            set_voice(uid, True, code)
            display = {v: k for k, v in INDIAN_VOICES.items()}.get(code, code)
            edit_msg_markup(chat_id, msg_id,
                f"🎙️ Voice: <b>{esc(display)}</b> selected.\nVoice replies are now ON.",
                voice_markup(uid))

    # ── Web search: more results ────────────────────────────────────
    elif data.startswith("web_more:"):
        # format: web_more:PAGE:QUERY
        parts = data.split(":", 2)
        if len(parts) == 3:
            try:    page = int(parts[1])
            except: page = 2
            query = parts[2]
            threading.Thread(
                target=_run_web_more,
                args=(chat_id, uid, msg_id, page, query),
                daemon=True,
            ).start()

    # ── Web search: get AI answer ───────────────────────────────────
    elif data.startswith("web_ai:"):
        query = data.split(":", 1)[1]
        tg("editMessageText", {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "text":       "🤖 <i>Getting AI answer…</i>",
            "parse_mode": "HTML",
        }, timeout=10)
        threading.Thread(
            target=_run_web_ai,
            args=(chat_id, uid, msg_id, query),
            daemon=True,
        ).start()

    # ── Web search: redo search ─────────────────────────────────────
    elif data.startswith("web_redo:"):
        query = data.split(":", 1)[1]
        tg("editMessageText", {
            "chat_id":    chat_id,
            "message_id": msg_id,
            "text":       f"🔄 <i>Re-searching: {esc(query)}…</i>",
            "parse_mode": "HTML",
        }, timeout=10)
        threading.Thread(
            target=_run_web_more,
            args=(chat_id, uid, msg_id, 1, query, True),  # bypass_cache=True
            daemon=True,
        ).start()

    # ── Image actions ───────────────────────────────────────────────
    elif data.startswith("img:"):
        # format: img:ACTION:KEY
        parts = data.split(":", 2)
        if len(parts) == 3:
            action, img_key = parts[1], parts[2]
            tg("editMessageText", {
                "chat_id":    chat_id,
                "message_id": msg_id,
                "text":       "⏳ <i>Analyzing…</i>",
                "parse_mode": "HTML",
            }, timeout=10)
            threading.Thread(
                target=_run_img_action,
                args=(chat_id, uid, msg_id, action, img_key),
                daemon=True,
            ).start()

    # ── Admin panel ─────────────────────────────────────────────────
    elif data.startswith("admin:"):
        if not is_admin(uid): return
        action = data.split(":")[1]
        cfg    = get_cfg()
        if action == "stats":
            handle_stats(chat_id, uid)
        elif action == "keys":
            lines = ["🔑 <b>API Keys</b>\n"]
            for p, f in [("Gemini","gemini_keys"),("Groq","groq_keys"),("Mistral","mistral_keys")]:
                keys = cfg.get(f, [])
                icon = "🟢" if keys else "🔴"
                lines.append(f"{icon} <b>{p}</b> — {len(keys)} key(s)")
                for i, k in enumerate(keys, 1):
                    lines.append(f"   {i}. <code>…{k[-8:]}</code>")
            lines.append(
                "\n━━━ <b>Commands</b> ━━━━━━━━━━\n"
                "<code>/addkey provider KEY</code>\n"
                "<code>/delkey provider INDEX</code>\n"
                "<i>providers: gemini · groq · mistral</i>"
            )
            edit_msg(chat_id, msg_id, "\n".join(lines))
        elif action == "bans":
            bans = cfg.get("banned_users", [])
            text = "🚫 <b>Banned Users</b>\n\n"
            if bans:
                text += "\n".join(f"• <code>{b}</code>" for b in bans)
            else:
                text += "<i>No banned users.</i>"
            text += "\n\n<code>/ban USER_ID</code> — ban or unban"
            edit_msg(chat_id, msg_id, text)
        elif action == "limits":
            limits = cfg.get("limits", {})
            lines  = ["⚡ <b>Rate Limits</b> <i>(per hour, 1-hour rolling window)</i>\n"]
            tier_icons = {"lite": "⚡", "flash": "✨", "pro": "🌐"}
            for tier in ("lite", "flash", "pro"):
                lim = limits.get(tier, {})
                icon = tier_icons.get(tier, "•")
                lines.append(
                    f"{icon} <b>{tier.capitalize()}</b>\n"
                    f"   💬 Messages: <b>{lim.get('messages','?')}</b>  "
                    f"🖼️ Images: <b>{lim.get('images','?')}</b>  "
                    f"🔍 Searches: <b>{lim.get('searches','?')}</b>"
                )
            mults = cfg.get("user_limit_multipliers", {})
            g_mult = mults.get("global", 1)
            lines.append(
                f"\n━━━ <b>Multipliers</b> ━━━━━━━━━━\n"
                f"🌐 Global: ×<b>{g_mult}</b>\n"
            )
            user_mults = {k: v for k, v in mults.items() if k != "global"}
            if user_mults:
                for uid_str, mult in list(user_mults.items())[:10]:
                    lines.append(f"  • User <code>{uid_str}</code> → ×{mult}")
            else:
                lines.append("  <i>No custom user multipliers set</i>")
            lines.append(
                "\n━━━ <b>Commands</b> ━━━━━━━━━━\n"
                "<code>/setlimit TIER ACTION VALUE</code>\n"
                "  e.g. <code>/setlimit pro images 20</code>\n\n"
                "<b>Multipliers:</b>\n"
                "<code>/limit 2x</code> — set global ×2\n"
                "<code>/limit USER_ID 2x</code> — per user\n"
                "↩️ Reply to user + <code>/limit 2x</code>"
            )
            edit_msg(chat_id, msg_id, "\n".join(lines))

# =============================================================================
# MAIN LOOP
# =============================================================================
def main():
    print(f"VoidAI starting...")
    print(f"Admin ID:    {BOOTSTRAP_ADMIN}")
    print(f"Config file: {CONFIG_FILE}")
    print(f"SearXNG:     {SEARXNG_URL or 'disabled'}")

    cfg = get_cfg()
    print(f"Groq keys:   {len(cfg.get('groq_keys',[]))}")
    print(f"Gemini keys: {len(cfg.get('gemini_keys',[]))}")
    print(f"Mistral keys:{len(cfg.get('mistral_keys',[]))}")
    print()

    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception as e:
                    print(f"[UPDATE] error: {e}")
        except Exception as e:
            print(f"[LOOP] error: {e}")
            time.sleep(3)

def process_update(update: dict):
    # ── Callback query ─────────────────────────────────────────────
    if "callback_query" in update:
        cq      = update["callback_query"]
        uid     = cq["from"]["id"]
        cb_id   = cq["id"]
        data    = cq.get("data", "")
        msg     = cq.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        msg_id  = msg.get("message_id", 0)
        # Store firstname from callback too
        fname = cq["from"].get("first_name", "")
        if fname:
            set_firstname(uid, fname)
        if chat_id:
            handle_callback(cb_id, uid, chat_id, msg_id, data)
        return

    msg     = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return

    uid     = msg.get("from", {}).get("id", 0)
    name    = msg.get("from", {}).get("first_name", "") or ""
    text    = (msg.get("text") or "").strip()
    photos  = msg.get("photo", [])
    caption = (msg.get("caption") or "").strip()
    msg_id  = msg.get("message_id", 0)

    # Always update firstname from any message
    if name:
        set_firstname(uid, name)

    if is_banned(uid):
        return

    # ── Commands ────────────────────────────────────────────────────
    if text.startswith("/"):
        parts     = text.split()
        command   = parts[0].split("@")[0].lower()
        args      = parts[1:]
        reply_msg = msg.get("reply_to_message", {})
        reply_uid = reply_msg.get("from", {}).get("id")
        reply_txt = (reply_msg.get("text") or reply_msg.get("caption") or "").strip()

        if   command == "/start":     handle_start(chat_id, uid, name or "there")
        elif command == "/help":      handle_help(chat_id)
        elif command == "/new":       handle_new(chat_id, uid)
        elif command == "/switch":    handle_switch(chat_id, uid)
        elif command == "/voice":     handle_voice_cmd(chat_id, uid)
        elif command == "/web":       handle_web_cmd(chat_id, uid, " ".join(args))
        elif command == "/usage":     handle_usage(chat_id, uid)
        elif command == "/summarize":
            threading.Thread(
                target=handle_summarize,
                args=(chat_id, uid, reply_txt),
                daemon=True,
            ).start()
        elif command == "/admin":     handle_admin(chat_id, uid)
        elif command == "/addkey":    handle_addkey(chat_id, uid, args, msg_id)
        elif command == "/delkey":    handle_delkey(chat_id, uid, args)
        elif command == "/listkeys":  handle_listkeys(chat_id, uid)
        elif command == "/addadmin":  handle_addadmin(chat_id, uid, args)
        elif command == "/ban":       handle_ban(chat_id, uid, args, reply_uid)
        elif command == "/limit":     handle_limit(chat_id, uid, args, reply_uid)
        elif command == "/setlimit":  handle_setlimit(chat_id, uid, args)
        elif command == "/stats":     handle_stats(chat_id, uid)
        return

    # ── Photo ────────────────────────────────────────────────────────
    if photos:
        threading.Thread(
            target=handle_image,
            args=(chat_id, uid, photos, caption),
            daemon=True,
        ).start()
        return

    # ── Text message ─────────────────────────────────────────────────
    if text:
        threading.Thread(
            target=handle_question,
            args=(chat_id, uid, text),
            daemon=True,
        ).start()

if __name__ == "__main__":
    main()

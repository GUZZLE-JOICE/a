"""J.A.R.V.I.S. local bridge for Windows.

Serves the HUD, owns the embedded research browser, and launches installed
applications without ever passing voice text to a command shell.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import difflib
import html
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jarvis_desktop_agent import (
    LearnedMemory,
    execute_desktop_action,
    focused_control_is_editable,
    plan_desktop_command,
    plan_text_entry,
    type_literal_if_editable,
)
from jarvis_overlay import JarvisWindowsOverlay
from jarvis_business import BusinessHub, PROVIDERS as BUSINESS_PROVIDERS


APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUILD_ID = "jarvis-v3.3-classic-app-scroll-fixed-20260807"


def locate_config() -> Path:
    if getattr(sys, "frozen", False):
        # Prefer an explicitly supplied config beside a frozen build. A
        # single-file PyInstaller payload must never persist into sys._MEIPASS:
        # that directory is temporary and disappears when the app exits.
        directories = (RUN_DIR,)
    else:
        directories = (RUN_DIR, RUN_DIR.parent, APP_DIR)
    for directory in directories:
        candidate = directory / "jarvis_server_config.json"
        if candidate.is_file():
            return candidate
    if getattr(sys, "frozen", False):
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or RUN_DIR)
        return local_appdata / "JARVIS" / "jarvis_server_config.json"
    return RUN_DIR / "jarvis_server_config.json"


CONFIG_PATH = locate_config()


def load_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "access_code": "",
        "host": "127.0.0.1",
        "port": 5015,
        "remote_server_port": 5005,
        "ollama_base_url": "http://127.0.0.1:11434",
        "remote_ollama_model": "llava:7b",
        "remote_request_timeout_seconds": 180,
        "remote_max_prompt_chars": 24000,
        "remote_max_image_bytes": 6 * 1024 * 1024,
        "remote_rate_limit_per_minute": 18,
        "allow_desktop_automation": True,
        "desktop_action_confirmation_seconds": 60,
        "learned_memory_limit": 120,
        "browser_extension_pairing_code": "",
        "browser_extension_command_seconds": 60,
        "discord_default_recipient": "",
        "desktop_overlay_vertical_percent": 0.84,
        "desktop_overlay_monitor": 1,
        "desktop_overlay_collapsed": False,
        "focus_existing_apps": True,
        "preferred_launch_monitor": 0,
        "open_as_app": False,
        "browser_channel": "msedge",
        "action_confirmation_seconds": 60,
        "allow_admin_launches": False,
        "allow_automatic_print_start": False,
        "makerworld_search_url": "https://makerworld.com/en/search/models?keyword={query}",
        "browser_stream_fps": 15,
        "browser_screenshot_quality": 45,
        "browser_gpu_acceleration": False,
        "browser_viewport_width": 800,
        "browser_viewport_height": 450,
        "business_mode_enabled": False,
        "business_daily_brief_enabled": True,
        "business_connectors": {
            "buffer": {"enabled": False, "mcp_url": "https://mcp.buffer.com/mcp"},
            "metricool": {"enabled": False, "mcp_url": "https://ai.metricool.com/mcp"},
            "revenuecat": {"enabled": False, "mcp_url": "https://mcp.revenuecat.ai/mcp"},
            "gmail": {"enabled": False, "mcp_url": "https://gmailmcp.googleapis.com/mcp/v1"},
            "meta": {"enabled": False, "mcp_url": "https://mcp.facebook.com/ads", "mode": "read_only"},
        },
        "app_aliases": {},
    }
    # A frozen one-file build carries the factory config inside the executable
    # and stores only user overrides under LocalAppData. This keeps the bundled
    # application payload self-contained while preserving aliases and defaults.
    sources = []
    embedded = APP_DIR / "jarvis_server_config.json"
    if getattr(sys, "frozen", False) and embedded != CONFIG_PATH:
        sources.append(embedded)
    sources.append(CONFIG_PATH)
    for source in sources:
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update(data)
        except (OSError, ValueError):
            pass
    return defaults


CONFIG = load_config()
CONFIG_LOCK = threading.RLock()
HOST = "127.0.0.1"  # Deliberately loopback-only, even if config is edited.
PORT = int(CONFIG.get("port", 5015))
BRIDGE_TOKEN = secrets.token_urlsafe(32)
ACTION_TTL = max(15, min(180, int(CONFIG.get("action_confirmation_seconds", 60))))
STREAM_TARGET_FPS = max(3, min(20, int(CONFIG.get("browser_stream_fps", 15))))
SCREENSHOT_QUALITY = max(35, min(75, int(CONFIG.get("browser_screenshot_quality", 45))))
VIEWPORT_WIDTH = max(640, min(1280, int(CONFIG.get("browser_viewport_width", 800))))
VIEWPORT_HEIGHT = max(360, min(720, int(CONFIG.get("browser_viewport_height", 450))))
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}
PENDING_LOCK = threading.Lock()
LEARNED_MEMORY = LearnedMemory(
    CONFIG_PATH.parent / "jarvis_learned_memory.json",
    int(CONFIG.get("learned_memory_limit", 120)),
)
EXTENSION_LOCK = threading.RLock()
EXTENSION_QUEUE: list[dict[str, Any]] = []
EXTENSION_RESULTS: dict[str, dict[str, Any]] = {}
EXTENSION_STATE: dict[str, Any] = {"last_seen": 0.0, "origin": "", "name": ""}
START_APPS_LOCK = threading.RLock()
START_APPS_CACHE: tuple[float, list[dict[str, str]]] = (0.0, [])
DESKTOP_FOCUS_LOCK = threading.RLock()
OVERLAY = JarvisWindowsOverlay()
BUSINESS = BusinessHub(CONFIG_PATH.parent)


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist shared settings for both the desktop and remote AI servers."""
    with CONFIG_LOCK:
        data: dict[str, Any] = {}
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update(saved)
        except (OSError, ValueError):
            pass
        data.update(updates)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, CONFIG_PATH)
        CONFIG.clear()
        CONFIG.update(load_config())
        return dict(CONFIG)


def current_owner_access_code() -> str:
    return str(CONFIG.get("access_code") or "")


def validate_owner_access_code(value: Any) -> str:
    code = str(value or "").strip()
    if len(code) < 8 or len(code) > 128:
        raise ValueError("Access codes must be 8 to 128 characters.")
    if any(ord(character) < 33 or ord(character) > 126 for character in code):
        raise ValueError("Access codes cannot contain spaces or special control characters.")
    return code


def fresh_owner_access_code() -> str:
    return "JRV-" + secrets.token_urlsafe(15)


def current_extension_pairing_code() -> str:
    """Return the separate local-only code used by the optional browser add-on."""
    code = str(CONFIG.get("browser_extension_pairing_code") or "").strip()
    if len(code) >= 12:
        return code
    code = "JEX-" + secrets.token_urlsafe(15)
    update_config({"browser_extension_pairing_code": code})
    return code


def fresh_extension_pairing_code() -> str:
    code = "JEX-" + secrets.token_urlsafe(15)
    update_config({"browser_extension_pairing_code": code})
    with EXTENSION_LOCK:
        EXTENSION_QUEUE.clear()
        EXTENSION_RESULTS.clear()
        EXTENSION_STATE.update({"last_seen": 0.0, "origin": "", "name": ""})
    return code


SAFE_EXTENSION_KEYS = {
    "backspace", "tab", "enter", "esc", "space", "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown", "delete", "insert", "f1", "f2", "f3",
    "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
}
EXTENSION_KEY_ALIASES = {"return": "enter", "escape": "esc", "spacebar": "space", "page up": "pageup", "page down": "pagedown"}
SENSITIVE_AUTOMATION_TEXT = re.compile(
    r"\b(?:password|passcode|one[- ]?time code|verification code|security code|credit card|social security)\b",
    re.I,
)


def normalize_extension_command(value: Any) -> dict[str, Any]:
    """Validate a small, explicit set of commands for a paired local browser."""
    if not isinstance(value, dict):
        raise ValueError("Browser command must be an object.")
    operation = str(value.get("operation") or "").strip().casefold()
    if operation in {"research_model", "research_web"}:
        query = re.sub(r"\s+", " ", str(value.get("query") or "").strip())
        if not query or len(query) > 240:
            raise ValueError("Browser research needs a short search phrase.")
        if SENSITIVE_AUTOMATION_TEXT.search(query):
            raise ValueError("Do not put passwords or verification codes into a web search.")
        label = "Follow a MakerWorld model result" if operation == "research_model" else "Research and follow a web result"
        return {
            "operation": operation,
            "query": query,
            "label": label,
            "prompt": f"{label} for {query[:120]}?",
        }
    if operation == "read_page":
        return {"operation": operation, "label": "Read current browser page", "prompt": "Read the current page in your paired browser?"}
    if operation == "open_url":
        raw_url = str(value.get("url") or "").strip()
        if not raw_url or len(raw_url) > 2000:
            raise ValueError("Enter a website address or a short search.")
        if re.match(r"^https?://", raw_url, re.I):
            parsed = urllib.parse.urlparse(raw_url)
            if not parsed.hostname:
                raise ValueError("That website address is incomplete.")
            url = raw_url
        else:
            url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(raw_url)
        return {
            "operation": operation,
            "url": url,
            "label": "Open browser page",
            "prompt": f"Open {raw_url[:120]} in your paired browser?",
        }
    if operation in {"click_text", "type_text"}:
        text = re.sub(r"\s+", " ", str(value.get("text") or "").strip())
        if not text or len(text) > 1000:
            raise ValueError("That browser text must be between 1 and 1,000 characters.")
        if operation == "type_text" and SENSITIVE_AUTOMATION_TEXT.search(text):
            raise ValueError("For privacy, type passwords and verification codes yourself.")
        label = "Click browser text" if operation == "click_text" else "Type in browser"
        verb = "Click" if operation == "click_text" else "Type"
        preview = text if len(text) <= 80 else text[:77] + "…"
        return {"operation": operation, "text": text, "label": label, "prompt": f"{verb} “{preview}” in your paired browser?"}
    if operation == "scroll":
        direction = str(value.get("direction") or "").casefold()
        amount = int(value.get("amount") or 3)
        if direction not in {"up", "down"} or not 1 <= amount <= 30:
            raise ValueError("Browser scrolling needs up or down and an amount from 1 to 30.")
        return {"operation": operation, "direction": direction, "amount": amount, "label": f"Scroll browser {direction}", "prompt": f"Scroll {direction} in your paired browser?"}
    if operation == "press":
        key = EXTENSION_KEY_ALIASES.get(str(value.get("key") or "").casefold().strip(), str(value.get("key") or "").casefold().strip())
        if key not in SAFE_EXTENSION_KEYS:
            raise ValueError("I can press Enter, Escape, arrows, navigation keys, or F1 through F12 in the paired browser.")
        return {"operation": operation, "key": key, "label": f"Press {key} in browser", "prompt": f"Press {key} in your paired browser?"}
    raise ValueError("That browser extension action is not available.")


def store_extension_action(command: Any) -> tuple[str, dict[str, Any]]:
    action = normalize_extension_command(command)
    token = secrets.token_urlsafe(24)
    ttl = max(15, min(180, int(CONFIG.get("browser_extension_command_seconds", ACTION_TTL))))
    with PENDING_LOCK:
        now = time.time()
        for old in [key for key, item in PENDING_ACTIONS.items() if item["expires"] < now]:
            PENDING_ACTIONS.pop(old, None)
        PENDING_ACTIONS[token] = {"kind": "extension", "extension": action, "expires": now + ttl}
    return token, action


def queue_extension_command(command: dict[str, Any]) -> str:
    command_id = secrets.token_urlsafe(18)
    lifetime = max(15, min(180, int(CONFIG.get("browser_extension_command_seconds", ACTION_TTL))))
    now = time.time()
    queued = dict(command)
    queued["command_id"] = command_id
    queued["expires"] = now + lifetime
    with EXTENSION_LOCK:
        EXTENSION_QUEUE[:] = [item for item in EXTENSION_QUEUE if float(item.get("expires") or 0) > now][-11:]
        EXTENSION_QUEUE.append(queued)
        EXTENSION_RESULTS.pop(command_id, None)
    return command_id


def extension_next_command() -> dict[str, Any] | None:
    now = time.time()
    with EXTENSION_LOCK:
        EXTENSION_STATE["last_seen"] = now
        EXTENSION_QUEUE[:] = [item for item in EXTENSION_QUEUE if float(item.get("expires") or 0) > now]
        if not EXTENSION_QUEUE:
            return None
        command = dict(EXTENSION_QUEUE.pop(0))
        command.pop("expires", None)
        return command


def extension_status() -> dict[str, Any]:
    with EXTENSION_LOCK:
        last_seen = float(EXTENSION_STATE.get("last_seen") or 0)
        return {
            "connected": bool(last_seen and time.time() - last_seen < 45),
            "last_seen": last_seen,
            "name": str(EXTENSION_STATE.get("name") or ""),
            "queued": len(EXTENSION_QUEUE),
        }


def store_extension_result(command_id: str, ok: bool, message: str, data: Any) -> None:
    if not command_id or len(command_id) > 160:
        raise ValueError("Invalid browser command result.")
    # Keep result memory bounded even if a page is very large.
    if not isinstance(data, dict):
        data = {}
    if len(json.dumps(data, ensure_ascii=False)) > 18000:
        data = {"title": str(data.get("title") or "")[:300], "url": str(data.get("url") or "")[:2000], "text": str(data.get("text") or "")[:12000]}
    now = time.time()
    with EXTENSION_LOCK:
        EXTENSION_STATE["last_seen"] = now
        EXTENSION_RESULTS[command_id] = {"ok": bool(ok), "message": str(message or "")[:600], "data": data, "updated_at": now}
        old = [key for key, item in EXTENSION_RESULTS.items() if now - float(item.get("updated_at") or 0) > 300]
        for key in old:
            EXTENSION_RESULTS.pop(key, None)


def extension_result(command_id: str) -> dict[str, Any] | None:
    with EXTENSION_LOCK:
        value = EXTENSION_RESULTS.get(command_id)
        return dict(value) if value else None


def on_windows() -> bool:
    return os.name == "nt"


def clean_app_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip().strip('"\''))
    return re.sub(r"\s+(?:as|with)\s+(?:an?\s+)?admin(?:istrator)?(?:\s+permissions?)?\s*$", "", value, flags=re.I).strip()


def wants_admin(value: str) -> bool:
    return bool(re.search(r"\b(?:as|with)\s+(?:an?\s+)?admin(?:istrator)?(?:\s+permissions?)?\b", value, re.I))


def parse_launch_utterance(utterance: str) -> tuple[str, bool]:
    admin = wants_admin(utterance)
    target = clean_app_name(utterance)
    target = re.sub(r"^\s*(?:hey\s+)?jarvis\s*[,;:\-]?\s*", "", target, flags=re.I)
    target = re.sub(r"^\s*(?:please\s+)+", "", target, flags=re.I)
    target = re.sub(r"^\s*(?:(?:can|could|would|will)\s+you\s+)(?:please\s+)?", "", target, flags=re.I)
    target = re.sub(r"^\s*(?:please\s+)+", "", target, flags=re.I)
    target = re.sub(r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:open(?:\s+up)?|launch|start|run)\s+", "", target, flags=re.I)
    target = re.sub(r"^(?:the\s+|my\s+)", "", target, flags=re.I).strip()
    target = re.sub(r"\s+(?:please|for\s+me)\s*[.!?]*$", "", target, flags=re.I).strip()
    if target.casefold() in {"note pad", "note pad app"}:
        target = "notepad"
    return target, admin


def parse_compound_launch_text_utterance(utterance: str) -> dict[str, str] | None:
    """Split an explicit "open app and write/type text" request in Python.

    This deliberately duplicates the HUD-side recognition so a stale browser
    tab cannot turn the entire phrase into an app name and silently drop the
    second action.
    """
    text = re.sub(r"\s+", " ", str(utterance or "").strip())
    text = re.sub(r"^\s*(?:hey\s+)?jarvis\s*[,;:\-]?\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:please\s+)+", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:(?:can|could|would|will)\s+you\s+)(?:please\s+)?", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:please\s+)+", "", text, flags=re.I)
    if not text or len(text) > 2400:
        return None
    match = re.match(
        r"^(?:open(?:\s+up)?|launch|start|run)\s+"
        r"(?:(?:the|my|a|an)\s+)?(?P<app>.+?)\s*[,;]?\s+"
        r"(?:and(?:\s+then)?|then)\s+(?P<verb>write|right|type)\s+(?P<instruction>.+?)\s*[.!?]*$",
        text,
        re.I,
    )
    if not match:
        return None
    app = clean_app_name(match.group("app"))
    instruction = match.group("instruction").strip().strip('"')
    mode = match.group("verb").casefold()
    if mode == "right":  # Common speech-to-text homophone for "write".
        mode = "write"
    if not app or not instruction:
        return None
    return {"app": app, "mode": mode, "instruction": instruction}


def should_generate_desktop_draft(instruction: str) -> bool:
    """Use AI for obvious writing tasks; keep short literal text literal."""
    text = re.sub(r"\s+", " ", instruction.casefold()).strip()
    if not text:
        return False
    writing_noun = re.search(
        r"\b(?:letter|note|email|message|essay|paragraph|poem|story|speech|summary|draft|description|bio)\b",
        text,
    )
    requested_piece = re.match(r"^(?:me\s+)?(?:a|an|the|some|one|\d+)\b", text)
    return bool(writing_noun and requested_piece) or text.startswith(("something about ", "a few sentences about "))


def generate_desktop_draft(instruction: str) -> str:
    """Draft text with the owner's local Ollama for bridge-side fallback."""
    base = str(CONFIG.get("ollama_base_url") or "http://127.0.0.1:11434").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Automatic writing needs the local Ollama server configured on this PC.")
    model = str(CONFIG.get("remote_ollama_model") or "llava:7b").strip() or "llava:7b"
    prompt = (
        "Write the exact finished text requested below for insertion into a desktop document. "
        "Return only the finished text: no preamble, quotation marks, markdown fences, or explanation. "
        "Keep it concise, non-explicit, appropriate for a general audience, and under 1,500 characters.\n\n"
        f"Request: {instruction}"
    )
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 420, "temperature": 0.45},
    }).encode("utf-8")
    request = urllib.request.Request(
        base + "/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = max(20, min(180, int(CONFIG.get("remote_request_timeout_seconds", 180))))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(2 * 1024 * 1024) or b"{}")
    except Exception as exc:
        raise RuntimeError("I could not draft the text with local Ollama. Make sure Ollama is running, then try again.") from exc
    draft = str(data.get("response") or "").strip()
    draft = re.sub(r"^```(?:text)?\s*", "", draft, flags=re.I)
    draft = re.sub(r"\s*```$", "", draft).strip()
    if not draft:
        raise RuntimeError("Local Ollama returned an empty draft.")
    return draft[:1900].strip()


def extract_explicit_launch_candidate(utterance: str) -> str | None:
    """Find a direct app-launch request even when speech adds filler words."""
    text = re.sub(r"\s+", " ", str(utterance or "").strip())
    text = re.sub(r"^\s*(?:hey\s+)?jarvis\s*[,;:\-]?\s*", "", text, flags=re.I)
    if not text or len(text) > 300:
        return None
    # Questions about how/why to open something should stay with the AI.
    if re.search(
        r"\b(?:tell me (?:how|whether|if)|show me how|explain how|what happens if|what if|how (?:do|can|would|should|to))\b",
        text,
        re.I,
    ) or re.match(r"^\s*(?:why|when|where|which|what)\b", text, re.I):
        return None
    match = re.search(
        r"\b(?:open(?:\s+up)?|launch|start|run)\s+(?:(?:the|my|a|an)\s+)?[^\r\n]+",
        text,
        re.I,
    )
    return match.group(0).strip() if match else None


def expand_candidate(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value.strip()))


def is_safe_windows_uri(value: str) -> bool:
    return bool(re.match(
        r"^(?:ms-settings:[A-Za-z0-9._-]*|microsoft\.windows\.camera:|windowsdefender:|shell:[A-Za-z0-9 _-]+)$",
        value,
        re.I,
    ))


def app_alias_candidates(name: str) -> list[str]:
    aliases = CONFIG.get("app_aliases") or {}
    normalized = name.casefold()
    result: list[str] = []
    for alias, values in aliases.items():
        alias_key = str(alias).casefold()
        if normalized == alias_key or normalized in alias_key or alias_key in normalized:
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                result.extend(expand_candidate(str(item)) for item in values)
    return result


def start_menu_links() -> list[Path]:
    if not on_windows():
        return []
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    links: list[Path] = []
    for root in roots:
        if root.is_dir():
            try:
                links.extend(root.rglob("*.lnk"))
            except OSError:
                continue
    return links


def registry_app_path(name: str) -> str | None:
    if not on_windows():
        return None
    try:
        import winreg
    except ImportError:
        return None
    exe_name = name if name.casefold().endswith(".exe") else name + ".exe"
    keys = [
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
    ]
    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, None)
                if value and Path(value).is_file():
                    return str(Path(value))
        except OSError:
            continue
    return None


def windows_start_apps() -> list[dict[str, str]]:
    """Read Windows Start-app registrations using a constant, non-user command."""
    global START_APPS_CACHE
    if not on_windows():
        return []
    with START_APPS_LOCK:
        cached_at, cached_apps = START_APPS_CACHE
        if cached_apps and time.time() - cached_at < 120:
            return list(cached_apps)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        data = []
    if isinstance(data, dict):
        data = [data]
    apps = [
        {"name": str(item.get("Name") or "").strip(), "app_id": str(item.get("AppID") or "").strip()}
        for item in data
        if isinstance(item, dict) and item.get("Name") and item.get("AppID")
    ] if isinstance(data, list) else []
    with START_APPS_LOCK:
        START_APPS_CACHE = (time.time(), apps)
    return list(apps)


def resolve_windows_start_app(name: str) -> dict[str, str] | None:
    wanted = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    best: tuple[float, dict[str, str]] | None = None
    for app in windows_start_apps():
        label = re.sub(r"[^a-z0-9]+", " ", app["name"].casefold()).strip()
        score = difflib.SequenceMatcher(None, wanted, label).ratio()
        if wanted == label:
            score = 1.0
        elif wanted in label or label in wanted:
            score = max(score, 0.86)
        if best is None or score > best[0]:
            best = (score, app)
    if best and best[0] >= 0.62:
        return {"kind": "app_id", "path": best[1]["app_id"], "label": best[1]["name"]}
    return None


def resolve_launch_target(name: str) -> dict[str, str] | None:
    name = clean_app_name(name)
    if not name or len(name) > 180 or "\n" in name or "\r" in name:
        return None
    if re.match(r"^https?://", name, re.I):
        return {"kind": "url", "path": name, "label": name}
    if is_safe_windows_uri(name):
        return {"kind": "uri", "path": name, "label": name.split(":", 1)[0]}

    direct = Path(expand_candidate(name))
    if direct.is_file() and direct.suffix.casefold() in {".exe", ".com", ".lnk", ".msc"}:
        return {"kind": "file", "path": str(direct.resolve()), "label": direct.stem}

    candidates = app_alias_candidates(name)
    registry = registry_app_path(name)
    if registry:
        candidates.insert(0, registry)
    which = shutil.which(name) or shutil.which(name + ".exe")
    if which:
        candidates.insert(0, which)
    for value in candidates:
        if is_safe_windows_uri(value):
            return {"kind": "uri", "path": value, "label": name}
        found = shutil.which(value) if not Path(value).is_file() else value
        if found:
            return {"kind": "file", "path": str(found), "label": name}

    wanted = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    best: tuple[float, Path] | None = None
    for link in start_menu_links():
        label = re.sub(r"[^a-z0-9]+", " ", link.stem.casefold()).strip()
        score = difflib.SequenceMatcher(None, wanted, label).ratio()
        if wanted == label:
            score = 1.0
        elif wanted in label or label in wanted:
            score = max(score, 0.82)
        if best is None or score > best[0]:
            best = (score, link)
    if best and best[0] >= 0.58:
        return {"kind": "file", "path": str(best[1]), "label": best[1].stem}
    start_app = resolve_windows_start_app(name)
    if start_app:
        return start_app
    return None


def launch_resolved(resolved: dict[str, str], elevated: bool = False) -> None:
    if not on_windows():
        raise RuntimeError("Application launching is available when Jarvis is running on Windows.")
    kind, target = resolved["kind"], resolved["path"]
    if elevated:
        if not bool(CONFIG.get("allow_admin_launches", False)):
            raise RuntimeError("Administrator launches are disabled in jarvis_server_config.json.")
        if kind != "file":
            raise RuntimeError("Only installed programs can be launched as administrator.")
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, None, str(Path(target).parent), 1)
        if result <= 32:
            raise RuntimeError("Windows did not approve the administrator launch.")
        return
    if kind == "app_id":
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + target], close_fds=True)
    elif kind in {"url", "uri"}:
        os.startfile(target)  # type: ignore[attr-defined]
    elif target.casefold().endswith((".lnk", ".msc")):
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen([target], cwd=str(Path(target).parent), close_fds=True)


def _window_title(hwnd: int) -> str:
    if not on_windows() or not hwnd:
        return ""
    user32 = ctypes.windll.user32
    length = int(user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _window_matches_app(title: str, resolved: dict[str, str]) -> bool:
    title_key = re.sub(r"[^a-z0-9]+", " ", str(title or "").casefold()).strip()
    if not title_key:
        return False
    labels = [str(resolved.get("label") or "")]
    if resolved.get("kind") == "file":
        labels.append(Path(str(resolved.get("path") or "")).stem)
    for label in labels:
        label_key = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
        if len(label_key) >= 3 and label_key in title_key:
            return True
    return False


def focus_launched_app(resolved: dict[str, str], timeout: float = 5.0) -> bool:
    """Verify/restore the launched app before any automatic text is entered."""
    if not on_windows():
        raise RuntimeError("Desktop automation is available when JARVIS is running on Windows.")
    user32 = ctypes.windll.user32
    deadline = time.time() + max(0.5, min(float(timeout), 8.0))
    while time.time() < deadline:
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground and _window_matches_app(_window_title(foreground), resolved):
            return True

        matches: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def enum_callback(hwnd: int, _lparam: int) -> bool:
            handle = int(hwnd or 0)
            if handle and user32.IsWindowVisible(handle) and _window_matches_app(_window_title(handle), resolved):
                matches.append(handle)
            return True

        user32.EnumWindows(enum_callback, 0)
        if matches:
            handle = matches[0]
            user32.ShowWindow(handle, 9)  # SW_RESTORE
            user32.SetForegroundWindow(handle)
            time.sleep(0.18)
            foreground = int(user32.GetForegroundWindow() or 0)
            if foreground == handle or _window_matches_app(_window_title(foreground), resolved):
                return True
        time.sleep(0.18)
    return False


def launch_or_focus_resolved(resolved: dict[str, str], elevated: bool = False) -> str:
    """Prefer an existing app window, matching the reference JARVIS behavior."""
    if (
        not elevated
        and bool(CONFIG.get("focus_existing_apps", True))
        and resolved.get("kind") in {"file", "app_id"}
        and focus_launched_app(resolved, timeout=0.55)
    ):
        return "focused"
    launch_resolved(resolved, elevated)
    return "launched"


def _windows_monitor_rects() -> list[tuple[int, int, int, int]]:
    if not on_windows():
        return []
    from ctypes import wintypes

    rects: list[tuple[int, int, int, int]] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    @callback_type
    def callback(_monitor: int, _dc: int, rect_pointer: Any, _data: int) -> bool:
        rect = rect_pointer.contents
        rects.append((int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    rects.sort(key=lambda item: (item[0], item[1]))
    return rects


def position_resolved_on_preferred_monitor(resolved: dict[str, str], timeout: float = 4.5) -> bool:
    """Move an app to the optional monitor selected in JARVIS Settings."""
    if not on_windows():
        return False
    if resolved.get("kind") not in {"file", "app_id"}:
        return False
    try:
        requested = int(CONFIG.get("preferred_launch_monitor", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        return False
    rects = _windows_monitor_rects()
    if not rects:
        return False
    requested = max(1, min(requested, len(rects)))
    if not focus_launched_app(resolved, timeout=timeout):
        return False
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return False
    current = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(current)):
        return False
    left, top, right, bottom = rects[requested - 1]
    monitor_width = max(320, right - left)
    monitor_height = max(240, bottom - top)
    width = min(max(480, int(current.right - current.left)), monitor_width)
    height = min(max(320, int(current.bottom - current.top)), monitor_height)
    x = left + max(0, (monitor_width - width) // 2)
    y = top + max(0, (monitor_height - height) // 2)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    SWP_NOZORDER = 0x0004
    SWP_SHOWWINDOW = 0x0040
    user32.SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_SHOWWINDOW)
    return True


def _restore_window(handle: int) -> None:
    """Bring an already-open window back without clicking inside it."""
    if not on_windows() or not handle:
        return
    user32 = ctypes.windll.user32
    if not user32.IsWindow(handle):
        return
    user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.SetForegroundWindow(handle)


def _focused_uia_control() -> Any | None:
    """Remember the exact edit/control focus so a targeted task can restore it."""
    if not on_windows():
        return None
    try:
        import uiautomation as auto  # type: ignore

        return auto.GetFocusedControl()
    except Exception:
        return None


def _restore_uia_control(control: Any | None) -> None:
    if control is None:
        return
    try:
        control.SetFocus()
    except Exception:
        pass


def _focus_discord_message_box_uia(handle: int) -> bool:
    """Focus Discord's composer through accessibility without moving the mouse."""
    if not on_windows() or not handle:
        return False
    try:
        import uiautomation as auto  # type: ignore

        root = auto.ControlFromHandle(handle)
        if root is None:
            return False
        candidates: list[tuple[float, Any]] = []
        pending: list[tuple[Any, int]] = [(root, 0)]
        seen = 0
        while pending and seen < 900:
            control, depth = pending.pop(0)
            seen += 1
            try:
                control_type = str(getattr(control, "ControlTypeName", "") or "")
                name = str(getattr(control, "Name", "") or "")
                enabled = bool(getattr(control, "IsEnabled", True))
                rectangle = getattr(control, "BoundingRectangle", None)
                if enabled and control_type in {"EditControl", "DocumentControl"} and rectangle:
                    width = max(0, int(rectangle.right) - int(rectangle.left))
                    height = max(0, int(rectangle.bottom) - int(rectangle.top))
                    score = float(width)
                    lowered = name.casefold()
                    if "message" in lowered or "send" in lowered:
                        score += 5000
                    if int(rectangle.top) > 300:
                        score += 1000
                    if width >= 220 and height >= 20:
                        candidates.append((score, control))
                if depth < 12:
                    try:
                        children = control.GetChildren() or []
                    except Exception:
                        children = []
                    pending.extend((child, depth + 1) for child in children)
            except Exception:
                continue
        if not candidates:
            return False
        candidates.sort(key=lambda item: item[0], reverse=True)
        candidates[0][1].SetFocus()
        return True
    except Exception:
        return False


def set_discord_default_recipient(value: Any) -> str:
    recipient = re.sub(r"\s+", " ", str(value or "").strip())
    if not 1 <= len(recipient) <= 80 or any(ord(character) < 32 for character in recipient):
        raise ValueError("Give me the Discord display name you want ‘my friend’ to mean.")
    update_config({"discord_default_recipient": recipient})
    return recipient


def send_discord_message_preserving_focus(recipient_value: Any, message_value: Any) -> str:
    """Send a user-requested Discord DM, then restore the user's prior window."""
    if not on_windows():
        raise RuntimeError("Discord app control is available when JARVIS is running on Windows.")
    if not bool(CONFIG.get("allow_desktop_automation", True)):
        raise RuntimeError("Desktop automation is disabled in jarvis_server_config.json.")

    recipient = re.sub(r"\s+", " ", str(recipient_value or "").strip())
    if recipient.casefold() in {"my friend", "friend", "my discord friend"}:
        recipient = str(CONFIG.get("discord_default_recipient") or "").strip()
        if not recipient:
            raise ValueError("Teach me the name once: ‘Jarvis, my Discord friend is Jude.’")
    if not 1 <= len(recipient) <= 80:
        raise ValueError("Tell me the Discord display name to message.")

    message = str(message_value or "").strip()
    message_action = plan_text_entry(message)
    recipient_action = plan_text_entry("@" + recipient)
    resolved = resolve_launch_target("discord")
    if not resolved:
        raise RuntimeError("I could not find the Discord desktop app. Open Discord once from Windows Start, then try again.")

    user32 = ctypes.windll.user32
    previous_window = int(user32.GetForegroundWindow() or 0)
    previous_control = _focused_uia_control()
    with DESKTOP_FOCUS_LOCK:
        try:
            if not focus_launched_app(resolved, timeout=1.0):
                launch_resolved(resolved, False)
                if not focus_launched_app(resolved, timeout=6.0):
                    raise RuntimeError("Discord opened, but Windows would not give its window focus.")

            try:
                import pyautogui  # type: ignore
            except ImportError as exc:
                raise RuntimeError("Discord control support is missing from this build. Rebuild or re-download the JARVIS EXE.") from exc

            # Keyboard-only navigation: never reposition the user's real mouse.
            pyautogui.PAUSE = 0.035
            pyautogui.hotkey("ctrl", "k")
            time.sleep(0.20)
            execute_desktop_action(recipient_action)
            time.sleep(0.28)
            pyautogui.press("enter")
            time.sleep(0.42)

            discord_window = int(user32.GetForegroundWindow() or 0)
            if not focused_control_is_editable():
                _focus_discord_message_box_uia(discord_window)
                time.sleep(0.10)
            if not focused_control_is_editable():
                raise RuntimeError(
                    "I opened the Discord conversation, but I could not verify its message box. "
                    "I sent nothing and restored your previous app."
                )

            execute_desktop_action(message_action)
            pyautogui.press("enter")
            time.sleep(0.08)
            return f"Sent to {recipient} on Discord and restored your previous app and edit focus."
        finally:
            if previous_window:
                _restore_window(previous_window)
                time.sleep(0.05)
            _restore_uia_control(previous_control)


def store_action(resolved: dict[str, str], elevated: bool) -> str:
    token = secrets.token_urlsafe(24)
    with PENDING_LOCK:
        now = time.time()
        for old in [key for key, item in PENDING_ACTIONS.items() if item["expires"] < now]:
            PENDING_ACTIONS.pop(old, None)
        PENDING_ACTIONS[token] = {
            "kind": "launch",
            "resolved": resolved,
            "elevated": elevated,
            "expires": now + ACTION_TTL,
        }
    return token


def store_compound_launch_text_action(resolved: dict[str, str], mode: str, instruction: str) -> str:
    token = secrets.token_urlsafe(24)
    with PENDING_LOCK:
        now = time.time()
        for old in [key for key, item in PENDING_ACTIONS.items() if item["expires"] < now]:
            PENDING_ACTIONS.pop(old, None)
        PENDING_ACTIONS[token] = {
            "kind": "launch_text",
            "resolved": resolved,
            "mode": mode,
            "instruction": instruction,
            "generate": mode == "write" and should_generate_desktop_draft(instruction),
            "expires": now + ACTION_TTL,
        }
    return token


def plan_compound_launch_text_action(utterance: str) -> dict[str, Any] | None:
    compound = parse_compound_launch_text_utterance(utterance)
    if not compound:
        return None
    target, elevated = parse_launch_utterance("open " + compound["app"])
    if elevated:
        raise ValueError("Administrator launches cannot be chained to automatic typing.")
    resolved = resolve_launch_target(target)
    if not resolved:
        raise ValueError(f"I could not find an installed app called {target or 'that'}.")
    if resolved.get("kind") in {"url", "uri"}:
        raise ValueError("Automatic text entry is only available for an installed desktop app.")
    if not bool(CONFIG.get("allow_desktop_automation", True)):
        raise ValueError("Desktop automation is disabled in jarvis_server_config.json.")
    # Validate the request before storing it. Generated output is validated
    # again immediately before it is entered.
    plan_text_entry(compound["instruction"])
    action_id = store_compound_launch_text_action(
        resolved,
        compound["mode"],
        compound["instruction"],
    )
    action_word = "writing" if compound["mode"] == "write" else "typing"
    return {
        "action_id": action_id,
        "kind": "launch_text",
        "label": f"{resolved['label']} + {compound['mode']}",
        "elevated": False,
        "needs_confirmation": False,
        "prompt": f"Opening {resolved['label']} and {action_word} the requested text.",
    }


def consume_action(token: str) -> dict[str, Any] | None:
    with PENDING_LOCK:
        item = PENDING_ACTIONS.pop(token, None)
    if not item or item["expires"] < time.time():
        return None
    return item


def store_desktop_action(utterance: str) -> tuple[str, dict[str, Any]]:
    if not bool(CONFIG.get("allow_desktop_automation", True)):
        raise RuntimeError("Desktop automation is disabled in jarvis_server_config.json.")
    action = plan_desktop_command(utterance)
    if not action:
        raise ValueError("Try a direct command such as type hello, press Enter, scroll down, copy, paste, or click at 500, 300.")
    token = secrets.token_urlsafe(24)
    ttl = max(15, min(180, int(CONFIG.get("desktop_action_confirmation_seconds", ACTION_TTL))))
    with PENDING_LOCK:
        now = time.time()
        for old in [key for key, item in PENDING_ACTIONS.items() if item["expires"] < now]:
            PENDING_ACTIONS.pop(old, None)
        PENDING_ACTIONS[token] = {"kind": "desktop", "desktop": action, "expires": now + ttl}
    return token, action


def system_action_from_utterance(utterance: str) -> dict[str, str] | None:
    text = re.sub(r"\s+", " ", utterance.casefold()).strip()
    if re.search(r"\b(cancel|abort|stop) (the )?shutdown\b", text):
        return {"name": "cancel_shutdown", "label": "cancel the pending shutdown"}
    if text in {"shutdown", "shut down", "power off", "turn off"} or re.search(r"\b(?:shut ?down|power off|turn off) (?:my |the )?(?:pc|computer|system)\b", text):
        return {"name": "shutdown", "label": "shut down this PC"}
    if text in {"restart", "reboot"} or re.search(r"\b(?:restart|reboot) (?:my |the )?(?:pc|computer|system)\b", text):
        return {"name": "restart", "label": "restart this PC"}
    if re.search(r"\b(?:put |send )?(?:my |the )?(?:pc|computer|system) to sleep\b|\bsleep (?:my |the )?(?:pc|computer|system)\b", text):
        return {"name": "sleep", "label": "put this PC to sleep"}
    if re.search(r"\block (?:my |the )?(?:pc|computer|screen)\b", text):
        return {"name": "lock", "label": "lock this PC"}
    if re.search(r"\b(show desktop|minimize all windows)\b", text):
        return {"name": "show_desktop", "label": "show the desktop"}
    return None


def store_system_action(action: dict[str, str]) -> str:
    token = secrets.token_urlsafe(24)
    with PENDING_LOCK:
        now = time.time()
        for old in [key for key, item in PENDING_ACTIONS.items() if item["expires"] < now]:
            PENDING_ACTIONS.pop(old, None)
        PENDING_ACTIONS[token] = {"kind": "system", "system": action, "expires": now + ACTION_TTL}
    return token


def perform_system_action(action: dict[str, str]) -> str:
    if not on_windows():
        raise RuntimeError("System actions are available when Jarvis is running on Windows.")
    name = action.get("name")
    if name == "cancel_shutdown":
        result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return "There was no pending shutdown to cancel."
        return "The pending shutdown was cancelled."
    if name == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "30"], close_fds=True)
        return "Your PC will shut down in 30 seconds. Say cancel shutdown if you changed your mind."
    if name == "restart":
        subprocess.Popen(["shutdown", "/r", "/t", "30"], close_fds=True)
        return "Your PC will restart in 30 seconds. Say cancel shutdown if you changed your mind."
    if name == "lock":
        if not ctypes.windll.user32.LockWorkStation():
            raise RuntimeError("Windows could not lock the PC.")
        return "This PC is locked."
    if name == "sleep":
        result = ctypes.windll.powrprof.SetSuspendState(False, True, False)
        if not result:
            raise RuntimeError("Windows could not put the PC to sleep.")
        return "Putting this PC to sleep."
    if name == "show_desktop":
        user32 = ctypes.windll.user32
        vk_lwin, vk_d, keyup = 0x5B, 0x44, 0x0002
        user32.keybd_event(vk_lwin, 0, 0, 0)
        user32.keybd_event(vk_d, 0, 0, 0)
        user32.keybd_event(vk_d, 0, keyup, 0)
        user32.keybd_event(vk_lwin, 0, keyup, 0)
        return "Showing the desktop."
    raise RuntimeError("That system action is not available.")


class BrowserWorker:
    """Own Playwright on one thread; HTTP request threads use queued RPC."""

    def __init__(self) -> None:
        self.requests: queue.Queue[tuple[str, dict[str, Any], queue.Queue[Any]]] = queue.Queue()
        self.thread: threading.Thread | None = None

    def call(self, operation: str, timeout: float = 30, **kwargs: Any) -> Any:
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="jarvis-browser", daemon=True)
            self.thread.start()
        response: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.requests.put((operation, kwargs, response))
        try:
            ok, value = response.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError("The browser bridge timed out.") from exc
        if ok:
            return value
        raise RuntimeError(str(value))

    def _run(self) -> None:
        playwright = browser = context = None
        tabs: dict[str, Any] = {}
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            channel = str(CONFIG.get("browser_channel") or "msedge")
            launch_args: list[str] = []
            if bool(CONFIG.get("browser_gpu_acceleration", False)):
                launch_args = ["--enable-gpu"]
            else:
                launch_args = ["--disable-gpu", "--disable-gpu-compositing"]
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True, args=launch_args)
            except Exception:
                browser = playwright.chromium.launch(headless=True, args=launch_args)
            context = browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                device_scale_factor=1,
                locale="en-US",
            )
        except Exception as exc:
            startup_error = (
                "The embedded browser could not start. Rebuild or re-download the JARVIS EXE. " + str(exc)
            )
        else:
            startup_error = ""

        while True:
            operation, args, response = self.requests.get()
            try:
                if startup_error:
                    raise RuntimeError(startup_error)
                value = self._operate(operation, args, context, tabs)
                response.put((True, value))
            except Exception as exc:
                response.put((False, str(exc)))

    @staticmethod
    def _snapshot(page: Any) -> dict[str, Any]:
        image = base64.b64encode(
            page.screenshot(
                type="jpeg",
                quality=SCREENSHOT_QUALITY,
                animations="allow",
                scale="css",
            )
        ).decode("ascii")
        return {"image": image, "title": page.title() or "Browser", "url": page.url}

    def _operate(self, operation: str, args: dict[str, Any], context: Any, tabs: dict[str, Any]) -> Any:
        if operation in {"new", "browse", "model"}:
            page = context.new_page()
            tab_id = secrets.token_urlsafe(10)
            tabs[tab_id] = page
            browse_search = False
            if operation == "new":
                url = "https://duckduckgo.com/"
            elif operation == "browse":
                query = str(args.get("query") or "").strip()
                browse_search = not bool(re.match(r"^https?://", query, re.I))
                url = query if not browse_search else "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(query)
            else:
                query = str(args.get("query") or "3d model").strip()
                template = str(CONFIG.get("makerworld_search_url"))
                url = template.replace("{query}", urllib.parse.quote_plus(query))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1800 if operation != "model" else 3500)
            except Exception:
                pass
            result = {"tab_id": tab_id, **self._snapshot(page)}
            if operation == "browse" and browse_search:
                # A research request should end on a useful page, not leave the
                # user staring at a search-results screen. Follow the first
                # normal result link, with a navigation fallback if the site's
                # click handler is blocked by automation protection.
                result_link = None
                result_url = ""
                result_title = ""
                try:
                    candidates = page.locator("a[data-testid='result-title-a'], a.result__a, article h2 a, h2 a[href]")
                    for index in range(min(candidates.count(), 24)):
                        link = candidates.nth(index)
                        href = link.get_attribute("href") or ""
                        text = " ".join((link.inner_text(timeout=900) or "").split())
                        absolute = urllib.parse.urljoin(page.url, href)
                        parsed = urllib.parse.urlparse(absolute)
                        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not text:
                            continue
                        result_link = link
                        result_url = absolute
                        result_title = text[:160]
                        break
                except Exception:
                    pass
                if result_link is not None and result_url:
                    try:
                        before_click = page.url
                        result_link.scroll_into_view_if_needed(timeout=2000)
                        result_link.click(timeout=4500)
                        page.wait_for_timeout(1000)
                        if page.url == before_click:
                            page.goto(result_url, wait_until="domcontentloaded", timeout=25000)
                        page.wait_for_timeout(1400)
                        result.update(self._snapshot(page))
                        result.update({"followed_result": True, "result_title": result_title, "result_url": result_url})
                    except Exception:
                        pass
            if operation == "model":
                candidate_title = ""
                candidate_url = ""
                candidate_link = None
                try:
                    links = page.locator("a[href*='/models/']")
                    query_terms = [term for term in re.findall(r"[a-z0-9]+", query.casefold()) if len(term) >= 2 and term not in {"the", "best", "good", "model", "for"}]
                    best_score = -1
                    for index in range(min(links.count(), 40)):
                        link = links.nth(index)
                        href = link.get_attribute("href") or ""
                        text = " ".join((link.inner_text(timeout=1200) or "").split())
                        if not href or "/search/" in href or not text:
                            continue
                        haystack = (text + " " + href).casefold()
                        score = sum(12 if term in text.casefold() else 4 if term in haystack else 0 for term in query_terms)
                        if query_terms and all(term in haystack for term in query_terms):
                            score += 25
                        # MakerWorld already sorts its own results by relevance;
                        # keep a small preference for earlier links on ties.
                        score += max(0, 6 - index // 5)
                        if score <= best_score:
                            continue
                        best_score = score
                        candidate_url = urllib.parse.urljoin(page.url, href)
                        candidate_title = text[:120]
                        candidate_link = link
                except Exception:
                    pass
                if candidate_url:
                    try:
                        # Follow the actual MakerWorld result link instead of
                        # replacing the address bar with the destination URL.
                        # This behaves like a browser agent clicking a result.
                        before_click = page.url
                        if candidate_link is not None:
                            candidate_link.scroll_into_view_if_needed(timeout=2500)
                            candidate_link.click(timeout=5000)
                            page.wait_for_timeout(1200)
                        if page.url == before_click:
                            page.goto(candidate_url, wait_until="domcontentloaded", timeout=25000)
                        page.wait_for_timeout(2200)
                        result.update(self._snapshot(page))
                    except Exception:
                        pass
                result.update({
                    "candidate_title": candidate_title or f"top MakerWorld results for {query}",
                    "candidate_url": candidate_url or page.url,
                    "answer": (
                        f"I opened a top MakerWorld result for {query}. "
                        "Check that it fits what you want, then I can open Bambu Studio for the final slice and print checks."
                    ),
                })
            return result

        tab_id = str(args.get("tab_id") or "")
        page = tabs.get(tab_id)
        if not page:
            raise RuntimeError("That browser tab is no longer open.")
        if operation == "snapshot":
            return self._snapshot(page)
        if operation == "close":
            page.close()
            tabs.pop(tab_id, None)
            return {"ok": True}
        if operation == "click":
            page.mouse.click(float(args.get("x", 0)), float(args.get("y", 0)))
        elif operation == "scroll":
            page.mouse.wheel(0, float(args.get("delta_y", 0)))
        elif operation == "type":
            page.keyboard.insert_text(str(args.get("text") or ""))
        elif operation == "key":
            key = str(args.get("key") or "")
            aliases = {" ": "Space", "Esc": "Escape"}
            key = aliases.get(key, key)
            modifiers = []
            if args.get("ctrl"):
                modifiers.append("Control")
            if args.get("alt"):
                modifiers.append("Alt")
            if args.get("shift"):
                modifiers.append("Shift")
            if args.get("meta"):
                modifiers.append("Meta")
            chord = "+".join(modifiers + [key]) if key else ""
            if chord:
                page.keyboard.press(chord)
        elif operation == "back":
            page.go_back(wait_until="domcontentloaded", timeout=20000)
        elif operation == "refresh":
            page.reload(wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(80)
        return self._snapshot(page)


BROWSER = BrowserWorker()
STREAM_TOKENS: dict[str, tuple[str, float]] = {}


def extract_uploaded_text(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "file")
    raw = base64.b64decode(str(payload.get("data") or ""), validate=True)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("File is larger than 12 MB.")
    suffix = Path(name).suffix.casefold()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(raw)).pages)[:80000]
        except ImportError as exc:
            raise ValueError("PDF extraction needs the optional pypdf package.") from exc
    if suffix == ".docx":
        try:
            from docx import Document

            return "\n".join(p.text for p in Document(io.BytesIO(raw)).paragraphs)[:80000]
        except ImportError as exc:
            raise ValueError("DOCX extraction needs the optional python-docx package.") from exc
    return raw.decode("utf-8", errors="replace")[:80000]


class JarvisHandler(BaseHTTPRequestHandler):
    server_version = "JarvisBridge/2.11.8"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[JARVIS] " + (fmt % args))

    def _is_extension_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        return origin.startswith("chrome-extension://") or origin.startswith("edge-extension://")

    def _json(self, data: dict[str, Any], status: int = 200, *, extension_cors: bool = False) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if extension_cors and self._is_extension_origin():
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self) -> dict[str, Any]:
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("JSON request required.")
        length = min(int(self.headers.get("Content-Length", "0") or 0), 14 * 1024 * 1024)
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object required.")
        return data

    def _trusted_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        return not origin or origin in allowed

    def _authorized(self) -> bool:
        return self._trusted_origin() and secrets.compare_digest(self.headers.get("X-Jarvis-Token", ""), BRIDGE_TOKEN)

    def _need_auth(self) -> bool:
        if self._authorized():
            return False
        self._json({"error": "Jarvis bridge authorization failed."}, HTTPStatus.FORBIDDEN)
        return True

    def _need_extension_auth(self) -> bool:
        supplied = self.headers.get("X-Jarvis-Extension", "")
        valid = self._is_extension_origin() and secrets.compare_digest(supplied, current_extension_pairing_code())
        if valid:
            return False
        self._json({"error": "Jarvis browser extension authorization failed."}, HTTPStatus.FORBIDDEN, extension_cors=self._is_extension_origin())
        return True

    def _need_owner_origin(self) -> bool:
        # These routes are served only by the loopback desktop bridge.  The
        # public remote AI server has no owner-management routes at all.
        if self._trusted_origin():
            return False
        self._json({"error": "Owner settings can only be changed from the local Jarvis app."}, HTTPStatus.FORBIDDEN)
        return True

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        if self._is_extension_origin():
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Jarvis-Extension")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")
        elif self._trusted_origin():
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Jarvis-Token, X-Jarvis-Code")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/business/oauth/callback/"):
            provider = urllib.parse.unquote(path.rsplit("/", 1)[-1]).casefold().strip()
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            params = {key: (values[0] if values else "") for key, values in query.items()}
            result = BUSINESS.oauth_callback(provider, params)
            ok = bool(result.get("ok"))
            title = "JARVIS CONNECTED" if ok else "JARVIS SIGN-IN"
            message = html.escape(str(result.get("message") or "Return to JARVIS."))
            color = "#6fffd2" if ok else "#ffb11b"
            body = ("<!doctype html><meta charset='utf-8'><title>" + title + "</title>"
                    "<style>body{margin:0;background:#020a0d;color:#d8f9ff;font:16px Segoe UI,sans-serif;display:grid;place-items:center;height:100vh}"
                    ".card{border:1px solid #00d9ef;padding:30px 34px;max-width:620px;background:#031317;border-radius:16px;box-shadow:0 0 40px #00d9ef22}"
                    "h1{font-size:18px;letter-spacing:3px;color:" + color + ";margin:0 0 14px}p{line-height:1.55;color:#a9cbd0}</style>"
                    "<div class='card'><h1>" + title + "</h1><p>" + message + "</p><p>You can close this tab after JARVIS reports CONNECTED.</p></div>")
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
            return
        if path == "/owner/access-code":
            if self._need_owner_origin():
                return
            self._json({
                "code": current_owner_access_code(),
                "port": int(CONFIG.get("remote_server_port", 5005)),
                "web_path": "/web",
            })
            return
        if path == "/owner/browser-extension-code":
            if self._need_owner_origin():
                return
            extension_folder = CONFIG_PATH.parent / "browser_extension"
            if not extension_folder.is_dir():
                extension_folder = APP_DIR / "browser_extension"
            self._json({
                "code": current_extension_pairing_code(),
                "status": extension_status(),
                "extension_path": str(extension_folder) if extension_folder.is_dir() else "",
            })
            return
        if path == "/extension/next":
            if self._need_extension_auth():
                return
            self._json({"command": extension_next_command()}, extension_cors=True)
            return
        if path == "/health":
            self._json({
                "ok": True,
                "build": BUILD_ID,
                "search_engine": "playwright-edge",
                "action_engine": "windows-start-apps-and-safe-launcher",
                "desktop_agent": bool(CONFIG.get("allow_desktop_automation", True)),
                "administrator_launches": bool(CONFIG.get("allow_admin_launches", False)),
                "learned_memory_count": len(LEARNED_MEMORY.search(limit=500)),
                "bridge_token": BRIDGE_TOKEN,
                "preferred_local_model": str(CONFIG.get("remote_ollama_model") or "llava:7b"),
                "automatic_print_start": bool(CONFIG.get("allow_automatic_print_start", False)),
                "browser_stream_target_fps": STREAM_TARGET_FPS,
                "desktop_overlay": OVERLAY.status(),
                "business_mode": bool(CONFIG.get("business_mode_enabled", False)),
                "business_agents": 5,
            })
            return
        if path.startswith("/frame_stream/"):
            self._frame_stream(path, urllib.parse.parse_qs(parsed.query))
            return
        main_page_request = path in {"/", "/jarvis_v2.html", "/index.html"}
        if main_page_request:
            # A fresh/reloaded page always starts with the native strip hidden.
            # The HUD explicitly reveals it only after the boot sequence ends.
            OVERLAY.set_visible(False)
        if path == "/":
            path = "/jarvis_v2.html"
        static_files = {
            "/jarvis_v2.html": "jarvis_v2.html",
            "/index.html": "jarvis_v2.html",
            "/manifest.webmanifest": "manifest.webmanifest",
            "/service-worker.js": "service-worker.js",
        }
        filename = static_files.get(path)
        if not filename:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        source = APP_DIR / filename
        if not source.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = source.read_bytes()
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _frame_stream(self, path: str, query: dict[str, list[str]]) -> None:
        tab_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        token = (query.get("token") or [""])[0]
        valid = STREAM_TOKENS.pop(token, None)
        if not valid or valid[0] != tab_id or valid[1] < time.time():
            self._json({"error": "Invalid stream token."}, HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sequence = 0
        previous_capture_at = 0.0
        measured_fps = 0.0
        frame_interval = 1.0 / STREAM_TARGET_FPS
        try:
            while True:
                frame_started_at = time.perf_counter()
                shot = BROWSER.call("snapshot", timeout=15, tab_id=tab_id)
                captured_at = time.perf_counter()
                if previous_capture_at:
                    instantaneous_fps = 1.0 / max(0.001, captured_at - previous_capture_at)
                    measured_fps = instantaneous_fps if not measured_fps else measured_fps * 0.72 + instantaneous_fps * 0.28
                previous_capture_at = captured_at
                sequence += 1
                packet = {
                    "status": "ready",
                    "seq": sequence,
                    "frame_seq": sequence,
                    "capture_fps": measured_fps,
                    "target_fps": STREAM_TARGET_FPS,
                    **shot,
                }
                self.wfile.write((json.dumps(packet) + "\n").encode("utf-8"))
                self.wfile.flush()
                remaining = frame_interval - (time.perf_counter() - frame_started_at)
                if remaining > 0:
                    time.sleep(remaining)
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            return

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/business/status":
                if self._need_auth():
                    return
                self._json({"ok": True, "state": BUSINESS.public_state(CONFIG)})
                return
            if path == "/business/oauth/start":
                if self._need_auth():
                    return
                provider = str(self._body().get("provider") or "").casefold().strip()
                self._json({"ok": True, "result": BUSINESS.start_oauth(provider, CONFIG)})
                return
            if path == "/business/oauth/status":
                if self._need_auth():
                    return
                provider = str(self._body().get("provider") or "").casefold().strip()
                self._json({"ok": True, "result": BUSINESS.oauth_flow_status(provider)})
                return
            if path == "/business/oauth/disconnect":
                if self._need_auth():
                    return
                provider = str(self._body().get("provider") or "").casefold().strip()
                BUSINESS.clear_oauth(provider)
                self._json({"ok": True, "state": BUSINESS.public_state(CONFIG)})
                return
            if path == "/business/oauth/app":
                if self._need_auth():
                    return
                data = self._body()
                provider = str(data.get("provider") or "").casefold().strip()
                BUSINESS.set_oauth_app_credentials(
                    provider,
                    str(data.get("client_id") or ""),
                    str(data.get("client_secret") or ""),
                )
                self._json({"ok": True, "state": BUSINESS.public_state(CONFIG)})
                return
            if path == "/business/config":
                if self._need_auth():
                    return
                data = self._body()
                updates: dict[str, Any] = {}
                if "business_mode_enabled" in data:
                    updates["business_mode_enabled"] = bool(data.get("business_mode_enabled"))
                if "daily_brief_enabled" in data:
                    updates["business_daily_brief_enabled"] = bool(data.get("daily_brief_enabled"))
                provider = str(data.get("provider") or "").casefold().strip()
                if provider:
                    if provider not in BUSINESS_PROVIDERS:
                        raise ValueError("Unknown business connector.")
                    current = CONFIG.get("business_connectors")
                    connectors = json.loads(json.dumps(current)) if isinstance(current, dict) else {}
                    provider_config = connectors.get(provider)
                    if not isinstance(provider_config, dict):
                        provider_config = {}
                    provider_config["enabled"] = bool(data.get("enabled", False))
                    provider_config.setdefault("mcp_url", BUSINESS_PROVIDERS[provider]["endpoint"])
                    if provider == "meta":
                        provider_config["mode"] = "read_only"
                    connectors[provider] = provider_config
                    updates["business_connectors"] = connectors
                if updates:
                    update_config(updates)
                self._json({"ok": True, "state": BUSINESS.public_state(CONFIG)})
                return
            if path == "/business/secret":
                if self._need_auth():
                    return
                data = self._body()
                provider = str(data.get("provider") or "").casefold().strip()
                value = str(data.get("value") or "")
                BUSINESS.set_secret(provider, value)
                self._json({"ok": True, "state": BUSINESS.public_state(CONFIG)})
                return
            if path == "/business/test":
                if self._need_auth():
                    return
                provider = str(self._body().get("provider") or "").casefold().strip()
                self._json({"ok": True, "result": BUSINESS.test_connector(provider, CONFIG)})
                return
            if path == "/business/dispatch":
                if self._need_auth():
                    return
                task = str(self._body().get("task") or "")
                self._json(BUSINESS.dispatch(task, CONFIG))
                return
            if path == "/business/brief":
                if self._need_auth():
                    return
                force = bool(self._body().get("force", False))
                self._json(BUSINESS.daily_brief(CONFIG, force=force))
                return
            if path == "/owner/access-code":
                if self._need_owner_origin():
                    return
                code = validate_owner_access_code(self._body().get("code"))
                update_config({"access_code": code})
                self._json({"ok": True, "code": code, "port": int(CONFIG.get("remote_server_port", 5005))})
                return
            if path == "/owner/access-code/refresh":
                if self._need_owner_origin():
                    return
                code = fresh_owner_access_code()
                update_config({"access_code": code})
                self._json({"ok": True, "code": code, "port": int(CONFIG.get("remote_server_port", 5005))})
                return
            if path == "/owner/browser-extension-code/refresh":
                if self._need_owner_origin():
                    return
                self._json({"ok": True, "code": fresh_extension_pairing_code(), "status": extension_status()})
                return
            if path == "/extension/register":
                if self._need_extension_auth():
                    return
                data = self._body()
                with EXTENSION_LOCK:
                    EXTENSION_STATE.update({
                        "last_seen": time.time(),
                        "origin": self.headers.get("Origin", ""),
                        "name": str(data.get("name") or "JARVIS Local Browser Link")[:120],
                    })
                self._json({"ok": True, "message": "JARVIS browser link paired."}, extension_cors=True)
                return
            if path == "/extension/result":
                if self._need_extension_auth():
                    return
                data = self._body()
                store_extension_result(
                    str(data.get("command_id") or ""),
                    bool(data.get("ok")),
                    str(data.get("message") or ""),
                    data.get("data"),
                )
                self._json({"ok": True}, extension_cors=True)
                return
            if path == "/extension/plan":
                if self._need_auth():
                    return
                action_id, action = store_extension_action(self._body().get("command"))
                self._json({
                    "action_id": action_id,
                    "kind": "browser_extension",
                    "label": action["label"],
                    "needs_confirmation": True,
                    "prompt": action["prompt"],
                    "safety_note": "This controls only a browser you paired on this Windows PC. It is never available through your public web link.",
                })
                return
            if path == "/extension/execute":
                if self._need_auth():
                    return
                item = consume_action(str(self._body().get("action_id") or ""))
                if not item or item.get("kind") != "extension":
                    self._json({"error": "That browser action expired. Ask me again."}, HTTPStatus.GONE)
                    return
                command_id = queue_extension_command(item["extension"])
                self._json({"ok": True, "command_id": command_id, "message": "Sent to the paired browser."})
                return
            if path == "/extension/status":
                if self._need_auth():
                    return
                self._json({"ok": True, "status": extension_status()})
                return
            if path == "/extension/research":
                if self._need_auth():
                    return
                if not extension_status().get("connected"):
                    self._json({"error": "External browsing is selected, but the JARVIS browser extension is not connected."}, HTTPStatus.CONFLICT)
                    return
                command = normalize_extension_command(self._body().get("command"))
                if command.get("operation") not in {"research_model", "research_web"}:
                    self._json({"error": "This route only follows explicit research results."}, HTTPStatus.BAD_REQUEST)
                    return
                command_id = queue_extension_command(command)
                self._json({"ok": True, "command_id": command_id, "message": "External browser research started."})
                return
            if path == "/extension/command-result":
                if self._need_auth():
                    return
                result = extension_result(str(self._body().get("command_id") or ""))
                self._json({"ok": True, "result": result})
                return
            if path == "/desktop/plan":
                if self._need_auth():
                    return
                action_id, action = store_desktop_action(str(self._body().get("utterance") or ""))
                self._json({
                    "action_id": action_id,
                    "kind": "desktop",
                    "label": action["label"],
                    "needs_confirmation": True,
                    "prompt": action["prompt"],
                    "safety_note": "This controls only the app currently in front on this Windows PC. It is never available to the public web version.",
                })
                return
            if path == "/desktop/execute":
                if self._need_auth():
                    return
                item = consume_action(str(self._body().get("action_id") or ""))
                if not item or item.get("kind") != "desktop":
                    self._json({"error": "That desktop action expired. Ask me again."}, HTTPStatus.GONE)
                    return
                self._json({"ok": True, "message": execute_desktop_action(item["desktop"])})
                return
            if path == "/desktop/type-now":
                if self._need_auth():
                    return
                # Intentional fast path: literal standalone "write/type" has
                # one semantic gate only -- whether keyboard focus is editable.
                # It never asks an AI model, classifies the text, or opens a
                # confirmation dialog.
                text_value = str(self._body().get("text") or "")
                try:
                    message = type_literal_if_editable(text_value)
                except RuntimeError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                    return
                self._json({"ok": True, "message": message})
                return
            if path == "/desktop/preferences":
                if self._need_auth():
                    return
                data = self._body()
                updates: dict[str, Any] = {}
                if "focus_existing_apps" in data:
                    updates["focus_existing_apps"] = bool(data.get("focus_existing_apps"))
                if "preferred_launch_monitor" in data:
                    try:
                        monitor = max(0, int(data.get("preferred_launch_monitor") or 0))
                    except (TypeError, ValueError):
                        monitor = 0
                    monitor_count = len(_windows_monitor_rects()) if on_windows() else 1
                    updates["preferred_launch_monitor"] = min(monitor, max(1, monitor_count)) if monitor else 0
                if updates:
                    update_config(updates)
                self._json({
                    "ok": True,
                    "focus_existing_apps": bool(CONFIG.get("focus_existing_apps", True)),
                    "preferred_launch_monitor": int(CONFIG.get("preferred_launch_monitor", 0) or 0),
                    "monitor_count": max(1, len(_windows_monitor_rects())) if on_windows() else 1,
                })
                return
            if path == "/desktop/overlay/state":
                if self._need_auth():
                    return
                data = self._body()
                updates: dict[str, Any] = {}
                if "vertical_percent" in data:
                    value = OVERLAY.set_vertical_percent(data.get("vertical_percent"))
                    updates["desktop_overlay_vertical_percent"] = value
                elif "temporary_vertical_percent" in data:
                    # Chat mode can temporarily lift the always-on-top dock
                    # above its composer without overwriting the operator's
                    # saved desktop position for every other app.
                    OVERLAY.set_vertical_percent(data.get("temporary_vertical_percent"))
                if "monitor" in data:
                    value = OVERLAY.set_monitor(data.get("monitor"))
                    updates["desktop_overlay_monitor"] = value
                if "collapsed" in data:
                    value = bool(data.get("collapsed"))
                    OVERLAY.set_collapsed(value)
                    updates["desktop_overlay_collapsed"] = value
                if "buttons" in data:
                    OVERLAY.set_button_states(data.get("buttons"))
                if "visible" in data:
                    OVERLAY.set_visible(bool(data.get("visible")))
                if updates:
                    update_config(updates)
                self._json({"ok": True, "overlay": OVERLAY.status()})
                return
            if path == "/desktop/overlay/cursor":
                if self._need_auth():
                    return
                data = self._body()
                if bool(data.get("visible", True)):
                    cursor = OVERLAY.update_cursor(data.get("x"), data.get("y"), visible=True, mode=str(data.get("mode") or "normal"))
                else:
                    OVERLAY.hide_cursor()
                    cursor = OVERLAY.status().get("cursor", {})
                self._json({"ok": True, "cursor": cursor})
                return
            if path == "/desktop/overlay/gesture":
                if self._need_auth():
                    return
                if not bool(CONFIG.get("allow_desktop_automation", True)):
                    self._json({"error": "Desktop automation is disabled in jarvis_server_config.json."}, HTTPStatus.FORBIDDEN)
                    return
                data = self._body()
                message = OVERLAY.gesture(
                    str(data.get("operation") or ""),
                    data.get("x"),
                    data.get("y"),
                    data.get("amount", 0),
                )
                self._json({"ok": True, "message": message, "overlay": OVERLAY.status()})
                return
            if path == "/desktop/overlay/events":
                if self._need_auth():
                    return
                data = self._body()
                wait = max(0.0, min(float(data.get("wait") or 0.0), 22.0))
                self._json({"ok": True, "event": OVERLAY.next_event(wait)})
                return
            if path == "/desktop/pointer":
                # Backwards-compatible route: V2.11 deliberately redirects old
                # hand-pointer packets to the JARVIS overlay. It never moves the
                # user's real Windows pointer.
                if self._need_auth():
                    return
                data = self._body()
                message = OVERLAY.gesture(
                    str(data.get("operation") or "move"),
                    data.get("x"),
                    data.get("y"),
                    data.get("amount", 0),
                )
                self._json({"ok": True, "message": message})
                return
            if path == "/desktop/discord-recipient":
                if self._need_auth():
                    return
                recipient = set_discord_default_recipient(self._body().get("recipient"))
                self._json({"ok": True, "recipient": recipient, "message": f"I’ll remember {recipient} as your Discord friend."})
                return
            if path == "/desktop/discord-message":
                if self._need_auth():
                    return
                data = self._body()
                message = send_discord_message_preserving_focus(data.get("recipient"), data.get("message"))
                self._json({"ok": True, "message": message})
                return
            if path == "/memory/remember":
                if self._need_auth():
                    return
                record = LEARNED_MEMORY.remember(str(self._body().get("text") or ""))
                self._json({"ok": True, "memory": record})
                return
            if path == "/memory/search":
                if self._need_auth():
                    return
                data = self._body()
                self._json({"ok": True, "memories": LEARNED_MEMORY.search(str(data.get("query") or ""), int(data.get("limit") or 12))})
                return
            if path == "/memory/clear":
                if self._need_auth():
                    return
                LEARNED_MEMORY.clear()
                self._json({"ok": True, "message": "Learned memories cleared from this PC."})
                return
            if path == "/actions/plan":
                if self._need_auth():
                    return
                data = self._body()
                utterance = str(data.get("utterance") or "")
                compound_plan = plan_compound_launch_text_action(utterance)
                if compound_plan:
                    self._json(compound_plan)
                    return
                system_action = system_action_from_utterance(utterance)
                if system_action:
                    action_id = store_system_action(system_action)
                    needs_confirmation = system_action["name"] in {"shutdown", "restart", "sleep", "lock"}
                    prompt = (
                        f"{system_action['label'].capitalize()}?"
                        if needs_confirmation
                        else f"{system_action['label'].capitalize()}."
                    )
                    note = (
                        "This will schedule the action with a 30-second cancel window."
                        if system_action["name"] in {"shutdown", "restart"}
                        else ""
                    )
                    self._json({
                        "action_id": action_id,
                        "kind": "system",
                        "label": system_action["label"],
                        "elevated": False,
                        "needs_confirmation": needs_confirmation,
                        "prompt": prompt,
                        "safety_note": note,
                    })
                    return
                target, elevated = parse_launch_utterance(utterance)
                if elevated and not bool(CONFIG.get("allow_admin_launches", False)):
                    self._json({
                        "error": "Administrator launches are disabled in this safe build. Open the app normally, then use Windows elevation manually if you truly need it."
                    }, HTTPStatus.FORBIDDEN)
                    return
                resolved = resolve_launch_target(target)
                if not resolved:
                    self._json({"error": f"I could not find an installed app called {target or 'that'}."}, 404)
                    return
                action_id = store_action(resolved, elevated)
                prompt = (
                    f"Launch {resolved['label']} as administrator? Windows will also show its UAC confirmation."
                    if elevated else f"Launching {resolved['label']}."
                )
                self._json({
                    "action_id": action_id,
                    "kind": "launch",
                    "label": resolved["label"],
                    "elevated": elevated,
                    "needs_confirmation": bool(elevated),
                    "prompt": prompt,
                })
                return
            if path == "/actions/route":
                if self._need_auth():
                    return
                utterance = str(self._body().get("utterance") or "")
                candidate = extract_explicit_launch_candidate(utterance)
                if not candidate:
                    self._json({"matched": False})
                    return
                compound_plan = plan_compound_launch_text_action(candidate)
                if compound_plan:
                    compound_plan["matched"] = True
                    self._json(compound_plan)
                    return
                target, elevated = parse_launch_utterance(candidate)
                if elevated and not bool(CONFIG.get("allow_admin_launches", False)):
                    self._json({
                        "matched": True,
                        "error": "Administrator launches are disabled in this safe build. Open the app normally first."
                    })
                    return
                resolved = resolve_launch_target(target)
                if not resolved:
                    self._json({
                        "matched": True,
                        "error": f"I heard an app-launch command, but I could not find an installed app called {target or 'that'}."
                    })
                    return
                action_id = store_action(resolved, elevated)
                prompt = (
                    f"Launch {resolved['label']} as administrator? Windows will also show its UAC confirmation."
                    if elevated else f"Launching {resolved['label']}."
                )
                self._json({
                    "matched": True,
                    "action_id": action_id,
                    "kind": "launch",
                    "label": resolved["label"],
                    "elevated": elevated,
                    "needs_confirmation": bool(elevated),
                    "prompt": prompt,
                })
                return
            if path == "/actions/launch-and-type":
                if self._need_auth():
                    return
                data = self._body()
                utterance = str(data.get("utterance") or "")
                text_value = str(data.get("text") or "")
                if system_action_from_utterance(utterance):
                    self._json({"error": "Power and system commands cannot be chained to automatic typing."}, HTTPStatus.BAD_REQUEST)
                    return
                target, elevated = parse_launch_utterance(utterance)
                if elevated:
                    self._json({"error": "Administrator launches cannot auto-type. Open the app normally or approve elevation separately."}, HTTPStatus.FORBIDDEN)
                    return
                resolved = resolve_launch_target(target)
                if not resolved:
                    self._json({"error": f"I could not find an installed app called {target or 'that'}."}, HTTPStatus.NOT_FOUND)
                    return
                if resolved.get("kind") in {"url", "uri"}:
                    self._json({"error": "Automatic text entry is only available for an installed desktop app."}, HTTPStatus.BAD_REQUEST)
                    return
                if not bool(CONFIG.get("allow_desktop_automation", True)):
                    self._json({"error": "Desktop automation is disabled in jarvis_server_config.json."}, HTTPStatus.FORBIDDEN)
                    return
                desktop_action = plan_text_entry(text_value)
                launch_or_focus_resolved(resolved, False)
                if not focus_launched_app(resolved):
                    self._json({
                        "error": f"{resolved['label']} opened, but I could not safely verify that its window had focus, so I did not type into another app."
                    }, HTTPStatus.CONFLICT)
                    return
                position_resolved_on_preferred_monitor(resolved, timeout=0.7)
                execute_desktop_action(desktop_action)
                self._json({
                    "ok": True,
                    "label": resolved["label"],
                    "message": f"Opened {resolved['label']} and typed the requested text.",
                })
                return
            if path == "/actions/execute":
                if self._need_auth():
                    return
                item = consume_action(str(self._body().get("action_id") or ""))
                if not item:
                    self._json({"error": "That launch request expired. Ask me to launch it again."}, 410)
                    return
                if item.get("kind") == "system":
                    self._json({"ok": True, "message": perform_system_action(item["system"])})
                    return
                if item.get("kind") == "launch_text":
                    instruction = str(item.get("instruction") or "")
                    text_value = generate_desktop_draft(instruction) if bool(item.get("generate")) else instruction
                    desktop_action = plan_text_entry(text_value)
                    resolved = item["resolved"]
                    launch_or_focus_resolved(resolved, False)
                    if not focus_launched_app(resolved):
                        self._json({
                            "error": f"{resolved['label']} opened, but I could not verify that its window had focus, so I did not type into another app."
                        }, HTTPStatus.CONFLICT)
                        return
                    position_resolved_on_preferred_monitor(resolved, timeout=0.7)
                    execute_desktop_action(desktop_action)
                    self._json({
                        "ok": True,
                        "message": f"Opened {resolved['label']} and typed the requested text.",
                    })
                    return
                launch_mode = launch_or_focus_resolved(item["resolved"], bool(item["elevated"]))
                if not bool(item["elevated"]):
                    position_resolved_on_preferred_monitor(item["resolved"])
                verb = "Focused" if launch_mode == "focused" else "Launching"
                self._json({"ok": True, "message": f"{verb} {item['resolved']['label']}{' as administrator' if item['elevated'] else ''}."})
                return
            if path == "/models/prepare":
                if self._need_auth():
                    return
                data = self._body()
                bambu = resolve_launch_target("bambu studio")
                if not bambu:
                    self._json({"error": "Bambu Studio is not installed or could not be found."}, 404)
                    return
                launch_or_focus_resolved(bambu, False)
                position_resolved_on_preferred_monitor(bambu)
                model_url = str(data.get("model_url") or "").strip()
                if model_url:
                    parsed_model = urllib.parse.urlparse(model_url)
                    host = (parsed_model.hostname or "").casefold()
                    if parsed_model.scheme == "https" and (host == "makerworld.com" or host.endswith(".makerworld.com")):
                        os.startfile(model_url)  # type: ignore[attr-defined]
                self._json({
                    "ok": True,
                    "message": "Bambu Studio and the MakerWorld handoff are open. Choose a print profile, then review the printer, plate, filament, supports, and sliced preview before pressing Print Plate.",
                    "automatic_print_start": False,
                })
                return
            if path == "/models/start-print":
                if self._need_auth():
                    return
                self._json({
                    "error": "Automatic print start is disabled. Confirm the sliced preview and start the job inside Bambu Studio.",
                    "automatic_print_start": False,
                }, 409)
                return
            if path == "/chat/extract-file":
                if self._need_auth():
                    return
                self._json({"text": extract_uploaded_text(self._body())})
                return

            browser_operations = {
                "/new_tab": "new",
                "/browse": "browse",
                "/models/search": "model",
                "/close_tab": "close",
                "/click": "click",
                "/scroll": "scroll",
                "/type": "type",
                "/key": "key",
                "/back": "back",
                "/refresh": "refresh",
            }
            if path in browser_operations:
                if self._need_auth():
                    return
                result = BROWSER.call(browser_operations[path], **self._body())
                self._json(result)
                return
            if path.startswith("/stream_token/"):
                if self._need_auth():
                    return
                tab_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                token = secrets.token_urlsafe(24)
                STREAM_TOKENS[token] = (tab_id, time.time() + 45)
                self._json({"token": token})
                return
            self._json({"error": "Unknown Jarvis bridge route."}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)


def open_app_window(url: str) -> None:
    time.sleep(0.8)
    if on_windows():
        candidates = [
            shutil.which("msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        edge = next((value for value in candidates if value and Path(value).is_file()), None)
        if edge:
            # Use the operator's normal Edge profile.  This keeps installed
            # extensions, the normal voice catalogue, cookies, and browser
            # preferences available to JARVIS.  The flags below affect only
            # background throttling for this launch; no alternate profile or
            # Incognito/App mode is created.
            arguments = [
                edge,
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion",
                "--new-window",
                url,
            ]
            try:
                subprocess.Popen(arguments, close_fds=True)
                return
            except OSError:
                pass
    try:
        webbrowser.open_new(url)
    except OSError:
        webbrowser.open(url)


def serve_local_bridge(*, port: int | None = None, open_window: bool = True) -> None:
    """Run the local-control HTTP bridge for source or single-EXE builds."""
    global PORT
    if len(current_owner_access_code()) < 8:
        update_config({"access_code": fresh_owner_access_code()})
    if port is not None:
        PORT = int(port)
    url = f"http://127.0.0.1:{PORT}"
    if on_windows():
        OVERLAY.start()
        OVERLAY.set_vertical_percent(CONFIG.get("desktop_overlay_vertical_percent", 0.84))
        OVERLAY.set_monitor(CONFIG.get("desktop_overlay_monitor", 1))
        OVERLAY.set_collapsed(bool(CONFIG.get("desktop_overlay_collapsed", False)))
        OVERLAY.set_visible(False)
    server = ThreadingHTTPServer((HOST, PORT), JarvisHandler)
    server.daemon_threads = True
    if open_window:
        threading.Thread(target=open_app_window, args=(url,), daemon=True).start()
    print(f"J.A.R.V.I.S local bridge: {url}")
    print("The bridge is loopback-only. Administrator launches always use Windows UAC.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        OVERLAY.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local JARVIS bridge")
    parser.add_argument("--no-open", action="store_true", help="Do not open the app window")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    serve_local_bridge(port=int(args.port), open_window=not args.no_open)


if __name__ == "__main__":
    main()

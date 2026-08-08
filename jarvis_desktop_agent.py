"""Local-only desktop automation and opt-in learned-memory helpers for JARVIS.

Nothing in this file exposes a network listener.  jarvis_browser.py calls it
only after local bridge authorization.  Higher-impact actions remain gated by
the HUD while narrowly scoped text entry can be chained to an app the user
explicitly asked JARVIS to open.
"""

from __future__ import annotations

import json
import ctypes
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any


SAFE_KEYS = {
    "backspace", "tab", "enter", "esc", "space", "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown", "delete", "insert", "f1", "f2", "f3",
    "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
}
KEY_ALIASES = {"return": "enter", "escape": "esc", "spacebar": "space", "page up": "pageup", "page down": "pagedown"}
HOTKEYS = {
    "copy": ("ctrl", "c"), "paste": ("ctrl", "v"), "cut": ("ctrl", "x"),
    "select all": ("ctrl", "a"), "undo": ("ctrl", "z"), "redo": ("ctrl", "y"),
    "save": ("ctrl", "s"), "save as": ("ctrl", "shift", "s"), "find": ("ctrl", "f"),
    "find next": ("ctrl", "g"), "find previous": ("ctrl", "shift", "g"),
    "new tab": ("ctrl", "t"), "close tab": ("ctrl", "w"), "reopen tab": ("ctrl", "shift", "t"),
    "new window": ("ctrl", "n"), "close window": ("alt", "f4"), "next tab": ("ctrl", "tab"),
    "previous tab": ("ctrl", "shift", "tab"), "refresh": ("ctrl", "r"),
    "focus address bar": ("ctrl", "l"), "zoom in": ("ctrl", "+"), "zoom out": ("ctrl", "-"),
    "reset zoom": ("ctrl", "0"), "print dialog": ("ctrl", "p"), "fullscreen": ("f11",),
    "switch apps": ("alt", "tab"), "close app": ("alt", "f4"),
}
SENSITIVE_TEXT = re.compile(r"\b(?:password|passcode|one[- ]?time code|verification code|security code|credit card|social security)\b", re.I)


def _focused_win32_edit_is_writable() -> bool:
    """Fallback for classic Win32 edit controls when UI Automation is absent."""
    if os.name != "nt":
        return False

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("hwndActive", ctypes.c_void_p),
            ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p),
            ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p),
            ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", ctypes.c_long * 4),
        ]

    try:
        user32 = ctypes.windll.user32
        foreground = int(user32.GetForegroundWindow() or 0)
        if not foreground:
            return False
        process_id = ctypes.c_uint32()
        thread_id = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id))
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return False
        focus = int(info.hwndFocus or 0)
        if not focus:
            return False
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(focus, class_name, len(class_name))
        control_class = class_name.value.casefold()
        if "edit" not in control_class:
            return False
        # ES_READONLY is the one flag that makes a classic Edit/RichEdit box
        # non-writable. Everything else is deliberately ignored here.
        style = int(user32.GetWindowLongW(focus, -16))
        return not bool(style & 0x0800)
    except Exception:
        return False


def focused_control_is_editable() -> bool:
    """Return one answer only: can the keyboard-focused control accept text?"""
    if os.name != "nt":
        return False
    try:
        import uiautomation as auto  # type: ignore

        control = auto.GetFocusedControl()
        if not control:
            return False
        control_type = str(getattr(control, "ControlTypeName", "") or "")
        localized_type = str(getattr(control, "LocalizedControlType", "") or "").casefold()
        editable_role = control_type == "EditControl" or localized_type in {"edit", "text box", "textbox"}
        if editable_role:
            try:
                value_pattern = control.GetValuePattern()
                if bool(getattr(value_pattern, "IsReadOnly", False)):
                    return False
            except Exception:
                # Some editable browser/Electron controls expose the Edit role
                # without ValuePattern. The role itself is still authoritative.
                pass
            return True
        # Modern Windows editors can expose their main writing surface as a
        # Document control instead of an Edit control. A focused, keyboard-
        # focusable document is the accessibility signal that it can receive
        # text; passive browser/PDF documents normally are not keyboard focusable.
        if control_type == "DocumentControl":
            return bool(getattr(control, "IsEnabled", True)) and bool(getattr(control, "IsKeyboardFocusable", False))
        try:
            value_pattern = control.GetValuePattern()
            return not bool(getattr(value_pattern, "IsReadOnly", True))
        except Exception:
            return False
    except Exception:
        return _focused_win32_edit_is_writable()


def _type_literal_text(text: str) -> None:
    """Type literal text without interpreting or content-classifying it."""
    try:
        import pyautogui  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Desktop automation support is missing from this build. Rebuild or re-download the JARVIS EXE.") from exc
    pyautogui.PAUSE = 0.02
    try:
        import pyperclip  # type: ignore

        original = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        pyperclip.copy(original)
    except Exception:
        if any(ord(character) > 127 for character in text):
            raise RuntimeError("Unicode typing support is missing from this build. Rebuild or re-download the JARVIS EXE.")
        pyautogui.write(text, interval=0.006)


def type_literal_if_editable(text: str) -> str:
    """Instant-write path: exactly one semantic gate, focused editability."""
    if not focused_control_is_editable():
        raise RuntimeError("Click a text box first, then say ‘write’ again.")
    _type_literal_text(str(text or ""))
    return "Typed it."


def execute_pointer_action(operation: str, x_ratio: float | None = None, y_ratio: float | None = None, amount: int = 0) -> str:
    """Reject the retired camera-to-physical-mouse path.

    V2.11+ routes hand input through ``JarvisWindowsOverlay`` so the camera can
    own a separate visible cursor. Keeping this compatibility symbol prevents
    stale imports from crashing while guaranteeing they cannot move the user's
    hardware pointer behind their back.
    """
    _ = (operation, x_ratio, y_ratio, amount)
    raise RuntimeError("Legacy physical-mouse hand control is disabled. Use the JARVIS overlay hand-input route.")


def _clean_command(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^(?:jarvis[,:]?\s*)", "", text, flags=re.I)
    return text


def _number(value: str, label: str, maximum: int) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not 0 <= number <= maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}.")
    return number


def plan_text_entry(value: str) -> dict[str, Any]:
    """Validate literal text before it can be pasted into a focused app."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("Say the text you want me to type.")
    if len(text) > 2000:
        raise ValueError("That is too much text to type in one action.")
    if SENSITIVE_TEXT.search(text):
        raise ValueError("For privacy, type passwords and verification codes yourself.")
    one_line = re.sub(r"\s+", " ", text)
    preview = one_line if len(one_line) <= 80 else one_line[:77] + "…"
    return {
        "operation": "type", "payload": {"text": text}, "label": "Type text",
        "prompt": f"Type “{preview}” into the currently focused app?",
    }


def plan_desktop_command(utterance: str) -> dict[str, Any] | None:
    """Turn a narrow, explicit local command into a confirmable action plan."""
    text = _clean_command(utterance)
    lower = text.casefold()
    if not text:
        return None

    for phrase, keys in HOTKEYS.items():
        if lower in {phrase, f"{phrase} please"}:
            return {
                "operation": "hotkey", "payload": {"keys": list(keys)},
                "label": phrase.capitalize(), "prompt": f"{phrase.capitalize()} in the currently focused app?",
            }

    match = re.match(r"^(?:type|write)\s+(?:this\s+|the following\s+)?(.+)$", text, re.I)
    if match:
        return plan_text_entry(match.group(1).strip().strip('"'))

    match = re.match(r"^(?:press|hit)\s+(.+)$", lower, re.I)
    if match:
        key = KEY_ALIASES.get(match.group(1).strip(), match.group(1).strip())
        if key not in SAFE_KEYS:
            raise ValueError("I can press Enter, Escape, arrows, navigation keys, or F1 through F12.")
        return {
            "operation": "press", "payload": {"key": key}, "label": f"Press {key}",
            "prompt": f"Press {key} in the currently focused app?",
        }

    match = re.match(r"^(?:scroll\s+)?(up|down)(?:\s+(\d+))?$", lower)
    if match:
        amount = int(match.group(2) or "3")
        if not 1 <= amount <= 30:
            raise ValueError("Scroll amount must be between 1 and 30.")
        direction = match.group(1)
        return {
            "operation": "scroll", "payload": {"amount": amount if direction == "up" else -amount},
            "label": f"Scroll {direction}", "prompt": f"Scroll {direction} in the currently focused app?",
        }

    match = re.match(r"^(double click|right click|click|move(?: the)? mouse to)\s+(?:at\s+)?(\d+)\s*[, ]\s*(\d+)$", lower)
    if match:
        kind, raw_x, raw_y = match.groups()
        x, y = _number(raw_x, "X", 10000), _number(raw_y, "Y", 10000)
        operation = {"click": "click", "double click": "doubleclick", "right click": "rightclick", "move mouse to": "move"}[kind]
        label = {"click": "Click", "doubleclick": "Double-click", "rightclick": "Right-click", "move": "Move mouse"}[operation]
        return {
            "operation": operation, "payload": {"x": x, "y": y}, "label": label,
            "prompt": f"{label} at screen position {x}, {y}?",
        }
    return None


def execute_desktop_action(action: dict[str, Any]) -> str:
    if os.name != "nt":
        raise RuntimeError("Desktop automation is available when JARVIS is running on Windows.")
    try:
        import pyautogui  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Desktop automation support is missing from this build. Rebuild or re-download the JARVIS EXE.") from exc
    operation = str(action.get("operation") or "")
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    pyautogui.PAUSE = 0.06
    if operation == "hotkey":
        keys = payload.get("keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError("Invalid keyboard shortcut.")
        pyautogui.hotkey(*keys)
        return "Shortcut sent to the focused app."
    if operation == "press":
        key = str(payload.get("key") or "")
        if key not in SAFE_KEYS:
            raise ValueError("Invalid key.")
        pyautogui.press(key)
        return f"Pressed {key}."
    if operation == "type":
        text = str(payload.get("text") or "")
        if not text or len(text) > 2000 or SENSITIVE_TEXT.search(text):
            raise ValueError("That text cannot be typed automatically.")
        try:
            import pyperclip  # type: ignore
            original = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.08)
            pyperclip.copy(original)
        except Exception:
            if any(ord(character) > 127 for character in text):
                raise RuntimeError("Unicode typing support is missing from this build. Rebuild or re-download the JARVIS EXE.")
            pyautogui.write(text, interval=0.01)
        return "Typed into the focused app."
    if operation == "scroll":
        amount = int(payload.get("amount") or 0)
        if not -30 <= amount <= 30 or amount == 0:
            raise ValueError("Invalid scroll amount.")
        pyautogui.scroll(amount)
        return "Scrolled the focused app."
    if operation in {"click", "doubleclick", "rightclick", "move"}:
        x, y = int(payload.get("x") or -1), int(payload.get("y") or -1)
        width, height = pyautogui.size()
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"That position is outside the current {width} by {height} screen.")
        if operation == "move":
            pyautogui.moveTo(x, y, duration=0.12)
            return "Moved the mouse."
        if operation == "doubleclick":
            pyautogui.doubleClick(x, y, interval=0.12)
            return "Double-clicked."
        pyautogui.click(x, y, button="right" if operation == "rightclick" else "left")
        return "Clicked."
    raise ValueError("That desktop action is not available.")


class LearnedMemory:
    """Small opt-in preference store. It never receives web-client requests."""

    def __init__(self, path: Path, limit: int = 120) -> None:
        self.path = path
        self.limit = max(10, min(500, int(limit)))
        self.lock = threading.RLock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(records[-self.limit:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def remember(self, text: str) -> dict[str, Any]:
        value = re.sub(r"\s+", " ", str(text or "").strip())
        if not 2 <= len(value) <= 600:
            raise ValueError("Memories must be 2 to 600 characters.")
        if SENSITIVE_TEXT.search(value):
            raise ValueError("For privacy, do not save passwords, verification codes, or financial details as memory.")
        with self.lock:
            records = self._load()
            now = int(time.time())
            for record in records:
                if str(record.get("text") or "").casefold() == value.casefold():
                    record["updated_at"] = now
                    self._save(records)
                    return record
            record = {"id": secrets.token_urlsafe(9), "text": value, "created_at": now, "updated_at": now}
            records.append(record)
            self._save(records)
            return record

    def search(self, query: str = "", limit: int = 12) -> list[dict[str, Any]]:
        words = {word for word in re.findall(r"[a-z0-9]{2,}", str(query or "").casefold())}
        with self.lock:
            records = self._load()
        if words:
            records = [record for record in records if words.intersection(re.findall(r"[a-z0-9]{2,}", str(record.get("text") or "").casefold()))]
        records.sort(key=lambda record: int(record.get("updated_at") or 0), reverse=True)
        return records[:max(1, min(50, int(limit)))]

    def clear(self) -> None:
        with self.lock:
            self._save([])

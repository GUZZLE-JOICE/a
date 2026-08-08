"""Native Windows overlay used by the local JARVIS desktop bridge.

The overlay intentionally does not move the real Windows mouse.  It owns a
small click-through hand-cursor window plus a no-activate quick-control dock.
Both windows are tool windows, so they stay above normal apps without adding
another Alt-Tab entry or taking keyboard focus away from the user's work.
"""

from __future__ import annotations

import ctypes
import math
import os
import queue
import sys
import threading
import time
from typing import Any


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return max(low, min(high, number))


class JarvisWindowsOverlay:
    """Always-on-top JARVIS hand cursor and quick controls for Windows."""

    ACTIONS = ("browser", "screen", "optics", "voice")
    DOCK_WIDTH = 322
    DOCK_HEIGHT = 74
    ARROW_WIDTH = 42
    BUTTON_START = 46
    BUTTON_WIDTH = 64
    BUTTON_GAP = 4
    CURSOR_SIZE = 54

    def __init__(self) -> None:
        self.available = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._state_lock = threading.RLock()
        self._root: Any = None
        self._dock: Any = None
        self._dock_canvas: Any = None
        self._cursor: Any = None
        self._cursor_canvas: Any = None
        self._visible = False
        self._collapsed = False
        self._vertical_percent = 0.84
        self._monitor_index = 1
        self._monitor_rect = (0, 0, 1920, 1080)
        self._monitor_rects: list[tuple[int, int, int, int]] = []
        self._dock_rect = (1598, 870, 1920, 944)
        self._cursor_point = (960, 540)
        self._cursor_visible = False
        self._cursor_mode = "normal"
        self._cursor_feedback_mode = ""
        self._cursor_feedback_started = 0.0
        self._cursor_feedback_until = 0.0
        self._button_states = {name: False for name in self.ACTIONS}
        self._pressed_action = ""
        self._pressed_until = 0.0
        self._animation_generation = 0

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._thread and self._thread.is_alive():
            return self.available
        self._thread = threading.Thread(target=self._run, name="JarvisOverlay", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self.available

    def stop(self) -> None:
        self._stop.set()
        self._commands.put(("stop", None))

    def set_visible(self, visible: bool) -> None:
        with self._state_lock:
            self._visible = bool(visible)
            if not self._visible:
                self._cursor_visible = False
        self._commands.put(("visibility", bool(visible)))

    def set_collapsed(self, collapsed: bool) -> None:
        with self._state_lock:
            self._collapsed = bool(collapsed)
        self._commands.put(("collapsed", bool(collapsed)))

    def set_vertical_percent(self, value: Any) -> float:
        value = _clamp(value, 0.12, 0.94, 0.84)
        with self._state_lock:
            self._vertical_percent = value
        self._commands.put(("position", None))
        return value

    def set_monitor(self, value: Any) -> int:
        try:
            requested = max(1, int(value))
        except (TypeError, ValueError):
            requested = 1
        with self._state_lock:
            self._monitor_index = requested
        self._commands.put(("monitor", requested))
        return requested

    def set_button_states(self, states: dict[str, Any] | None) -> None:
        if not isinstance(states, dict):
            return
        with self._state_lock:
            for name in self.ACTIONS:
                if name in states:
                    self._button_states[name] = bool(states[name])
        self._commands.put(("redraw-dock", None))

    def update_cursor(self, x_ratio: Any, y_ratio: Any, *, visible: bool = True, mode: str = "normal") -> dict[str, Any]:
        x_ratio = _clamp(x_ratio, 0.0, 1.0, 0.5)
        y_ratio = _clamp(y_ratio, 0.0, 1.0, 0.5)
        mode = str(mode or "normal").casefold()
        if mode not in {"normal", "precision", "clicking", "scrolling", "dragging"}:
            mode = "normal"
        with self._state_lock:
            left, top, right, bottom = self._monitor_rect
            x = int(round(left + x_ratio * max(1, right - left - 1)))
            y = int(round(top + y_ratio * max(1, bottom - top - 1)))
            self._cursor_point = (x, y)
            self._cursor_visible = bool(visible and self._visible)
            self._cursor_mode = mode
        self._commands.put(("cursor", None))
        return {"x": x, "y": y, "visible": self._cursor_visible, "mode": mode}

    def hide_cursor(self) -> None:
        with self._state_lock:
            self._cursor_visible = False
        self._commands.put(("cursor", None))

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "available": bool(self.available),
                "visible": self._visible,
                "collapsed": self._collapsed,
                "vertical_percent": self._vertical_percent,
                "monitor": self._monitor_index,
                "monitor_count": max(1, len(self._monitor_rects)),
                "dock_rect": list(self._dock_rect),
                "buttons": dict(self._button_states),
                "cursor": {"x": self._cursor_point[0], "y": self._cursor_point[1], "visible": self._cursor_visible},
            }

    def next_event(self, timeout: float = 0.0) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=max(0.0, min(float(timeout), 25.0)))
        except queue.Empty:
            return None

    def gesture(self, operation: str, x_ratio: Any, y_ratio: Any, amount: Any = 0) -> str:
        if sys.platform != "win32" or not self.available:
            raise RuntimeError("The Windows JARVIS overlay is not available.")
        operation = str(operation or "").casefold().strip()
        # V2.11.2 deliberately has no Windows drag operation.  The two-finger
        # gesture is reserved for camera-relative JARVIS browser-panel motion,
        # so it can never leave a synthetic LMB/touch contact held down.
        if operation in {"drag_start", "drag_move", "drag_end"}:
            raise ValueError("Two-finger dragging is JARVIS-tab-only and never holds the Windows mouse button.")
        self.update_cursor(x_ratio, y_ratio, visible=True, mode="normal")
        with self._state_lock:
            point = self._cursor_point
        if operation == "click":
            self._set_cursor_feedback("clicking", 0.28)
            if self._dock_action_at(*point):
                return "JARVIS overlay control activated without moving the Windows mouse."
            return self._post_click_without_moving_mouse(*point)
        if operation == "scroll":
            self._set_cursor_feedback("scrolling", 0.16)
            try:
                wheel = int(amount)
            except (TypeError, ValueError):
                wheel = 0
            return self._post_scroll_without_moving_mouse(point[0], point[1], wheel)
        if operation == "move":
            return "JARVIS hand cursor moved; the Windows mouse was left alone."
        raise ValueError("Overlay operation must be move, click, or scroll.")

    def _set_cursor_feedback(self, mode: str, seconds: float) -> None:
        now = time.monotonic()
        with self._state_lock:
            self._cursor_feedback_mode = mode
            self._cursor_feedback_started = now
            self._cursor_feedback_until = now + max(0.05, float(seconds))
        self._commands.put(("cursor", None))

    def _flash_button(self, action: str) -> None:
        with self._state_lock:
            self._pressed_action = str(action or "")
            self._pressed_until = time.monotonic() + 0.18
        self._commands.put(("redraw-dock", None))

    def _dock_action_at(self, x: int, y: int) -> bool:
        with self._state_lock:
            left, top, right, bottom = self._dock_rect
            collapsed = self._collapsed
        if not (left <= x < right and top <= y < bottom):
            return False
        local_x = x - left
        if local_x < self.ARROW_WIDTH:
            self._flash_button("arrow")
            self._toggle_collapsed()
            return True
        if collapsed:
            return True
        index = int((local_x - self.BUTTON_START) // (self.BUTTON_WIDTH + self.BUTTON_GAP))
        if 0 <= index < len(self.ACTIONS):
            within = (local_x - self.BUTTON_START) % (self.BUTTON_WIDTH + self.BUTTON_GAP)
            if within < self.BUTTON_WIDTH:
                action = self.ACTIONS[index]
                self._flash_button(action)
                self._events.put({"action": action})
        return True

    def _toggle_collapsed(self) -> None:
        with self._state_lock:
            self._collapsed = not self._collapsed
            value = self._collapsed
        self._events.put({"action": "overlay_collapsed", "collapsed": value})
        self._commands.put(("collapsed", value))

    def _desktop_window_at_point(self, x: int, y: int) -> tuple[int, int] | None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        current_pid = os.getpid()
        excluded: set[int] = set()
        for window in (self._dock, self._cursor):
            try:
                if window is not None:
                    excluded.add(int(window.winfo_id()))
            except Exception:
                pass
        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def enum_window(hwnd: int, _lparam: int) -> bool:
            handle = int(hwnd or 0)
            if not handle or handle in excluded or not user32.IsWindowVisible(handle) or user32.IsIconic(handle):
                return True
            # Tk may expose an inner HWND while EnumWindows sees its wrapper.
            # Skip every top-level HWND from this bridge process so JARVIS's
            # own always-on-top cursor/dock can never swallow the gesture.
            pid = wintypes.DWORD()
            try:
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if int(pid.value) == current_pid:
                    return True
            except Exception:
                pass
            rect = wintypes.RECT()
            if not user32.GetWindowRect(handle, ctypes.byref(rect)):
                return True
            if int(rect.left) <= x < int(rect.right) and int(rect.top) <= y < int(rect.bottom):
                found.append(handle)
                return False
            return True

        user32.EnumWindows(enum_window, 0)
        if not found:
            return None
        root = found[0]
        target = root
        CWP_SKIPINVISIBLE = 0x0001
        CWP_SKIPDISABLED = 0x0002
        CWP_SKIPTRANSPARENT = 0x0004
        for _ in range(8):
            point = wintypes.POINT(x, y)
            if not user32.ScreenToClient(target, ctypes.byref(point)):
                break
            child = int(user32.ChildWindowFromPointEx(target, point, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT) or 0)
            if not child or child == target or child in excluded:
                break
            target = child
        return root, target

    @staticmethod
    def _packed_xy(x: int, y: int) -> int:
        return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)

    def _post_click_without_moving_mouse(self, x: int, y: int) -> str:
        from ctypes import wintypes

        hit = self._desktop_window_at_point(x, y)
        if not hit:
            return "There is no app under the JARVIS hand cursor."
        root, target = hit
        user32 = ctypes.windll.user32
        user32.ShowWindow(root, 9)  # SW_RESTORE
        user32.SetForegroundWindow(root)
        if self._invoke_uia_default_action(target, x, y):
            return "Activated the Windows control under the JARVIS hand cursor without moving the Windows mouse."
        point = wintypes.POINT(x, y)
        user32.ScreenToClient(target, ctypes.byref(point))
        packed = self._packed_xy(point.x, point.y)
        WM_MOUSEMOVE = 0x0200
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        MK_LBUTTON = 0x0001
        user32.PostMessageW(target, WM_MOUSEMOVE, 0, packed)
        user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, packed)
        user32.PostMessageW(target, WM_LBUTTONUP, 0, packed)
        return "Clicked under the JARVIS hand cursor without moving the Windows mouse."

    @staticmethod
    def _invoke_uia_default_action(hwnd: int, x: int | None = None, y: int | None = None) -> bool:
        """Invoke the UI Automation element at a point without moving the mouse.

        Modern apps such as Edge and Discord often expose an entire surface as
        one HWND. ControlFromHandle therefore lands on the outer window instead
        of the button/link beneath the hand cursor. UI Automation's point hit
        test resolves the real descendant control first; the HWND route remains
        a fallback for classic Win32 apps.
        """
        try:
            import uiautomation as auto  # type: ignore
            controls = []
            if x is not None and y is not None:
                try:
                    point_control = auto.ControlFromPoint(int(x), int(y))
                    if point_control:
                        controls.append(point_control)
                except Exception:
                    pass
            try:
                handle_control = auto.ControlFromHandle(int(hwnd))
                if handle_control:
                    controls.append(handle_control)
            except Exception:
                pass
            for control in controls:
                for getter, method in (
                    ("GetInvokePattern", "Invoke"),
                    ("GetTogglePattern", "Toggle"),
                    ("GetSelectionItemPattern", "Select"),
                    ("GetLegacyIAccessiblePattern", "DoDefaultAction"),
                ):
                    try:
                        pattern = getattr(control, getter)()
                        action = getattr(pattern, method, None)
                        if callable(action):
                            action()
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    def _post_scroll_without_moving_mouse(self, x: int, y: int, amount: int) -> str:
        hit = self._desktop_window_at_point(x, y)
        if not hit:
            return "There is no app under the JARVIS hand cursor."
        _root, target = hit
        wheel_steps = max(-8, min(8, int(amount or 0)))
        if wheel_steps == 0:
            return "No hand-scroll movement to send."
        delta = wheel_steps * 120
        wheel_word = delta & 0xFFFF
        wparam = wheel_word << 16
        lparam = self._packed_xy(x, y)
        ctypes.windll.user32.PostMessageW(target, 0x020A, wparam, lparam)  # WM_MOUSEWHEEL
        return "Scrolled under the JARVIS hand cursor without moving the Windows mouse."

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            self._ready.set()
            return
        try:
            self._monitor_rects = self._enumerate_monitors()
            if self._monitor_rects:
                self._monitor_rect = self._monitor_rects[0]

            root = tk.Tk()
            root.withdraw()
            self._root = root

            dock = tk.Toplevel(root)
            dock.overrideredirect(True)
            dock.configure(bg="#010b0e")
            dock.attributes("-topmost", True)
            dock.withdraw()
            canvas = tk.Canvas(dock, width=self.DOCK_WIDTH, height=self.DOCK_HEIGHT, bg="#010b0e", highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True)
            canvas.bind("<Button-1>", self._on_dock_click)
            self._dock = dock
            self._dock_canvas = canvas

            cursor = tk.Toplevel(root)
            cursor.overrideredirect(True)
            cursor.configure(bg="#010203")
            cursor.attributes("-topmost", True)
            cursor.withdraw()
            try:
                cursor.attributes("-transparentcolor", "#010203")
            except Exception:
                pass
            cursor_canvas = tk.Canvas(cursor, width=self.CURSOR_SIZE, height=self.CURSOR_SIZE, bg="#010203", highlightthickness=0, bd=0)
            cursor_canvas.pack(fill="both", expand=True)
            self._cursor = cursor
            self._cursor_canvas = cursor_canvas

            root.update_idletasks()
            self._apply_window_styles()
            self._place_dock(immediate=True)
            self._draw_dock()
            self._draw_cursor()
            self.available = True
            self._ready.set()
            root.after(16, self._drain_commands)
            root.mainloop()
        except Exception:
            self.available = False
            self._ready.set()

    def _enumerate_monitors(self) -> list[tuple[int, int, int, int]]:
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
        return rects or [(0, 0, 1920, 1080)]

    def _apply_window_styles(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        user32.SetWindowRgn.restype = ctypes.c_int
        gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_LAYERED = 0x00080000
        HWND_TOPMOST = -1
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        for window, click_through in ((self._dock, False), (self._cursor, True)):
            raw_hwnd = int(window.winfo_id())
            hwnd = int(user32.GetAncestor(raw_hwnd, 2) or raw_hwnd)  # GA_ROOT
            style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
            style |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            if click_through:
                style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if window is self._dock:
                # Clip the actual native window, not only its painted border,
                # so the desktop strip has genuinely rounded corners.
                region = gdi32.CreateRoundRectRgn(0, 0, self.DOCK_WIDTH + 1, self.DOCK_HEIGHT + 1, 24, 24)
                if region:
                    user32.SetWindowRgn(hwnd, region, True)
        # Make the cursor's #010203 background transparent even on Tk builds
        # where the -transparentcolor attribute is not surfaced.
        cursor_raw_hwnd = int(self._cursor.winfo_id())
        cursor_hwnd = int(user32.GetAncestor(cursor_raw_hwnd, 2) or cursor_raw_hwnd)
        transparent_key = (3 << 16) | (2 << 8) | 1
        user32.SetLayeredWindowAttributes(cursor_hwnd, transparent_key, 255, 0x00000001)

    def _drain_commands(self) -> None:
        if self._stop.is_set():
            try:
                self._root.destroy()
            except Exception:
                pass
            return
        while True:
            try:
                command, _payload = self._commands.get_nowait()
            except queue.Empty:
                break
            if command == "stop":
                self._stop.set()
                break
            if command == "visibility":
                self._apply_visibility()
            elif command == "collapsed":
                self._animate_dock()
                self._draw_dock()
            elif command in {"position", "monitor"}:
                if command == "monitor":
                    self._select_monitor()
                self._place_dock(immediate=True)
                self._place_cursor()
            elif command == "cursor":
                self._place_cursor()
                self._draw_cursor()
            elif command == "redraw-dock":
                self._draw_dock()
        now = time.monotonic()
        with self._state_lock:
            cursor_animating = bool(self._cursor_feedback_mode and now < self._cursor_feedback_until)
            cursor_just_expired = bool(self._cursor_feedback_mode and now >= self._cursor_feedback_until)
            button_animating = bool(self._pressed_action and now < self._pressed_until)
            button_just_expired = bool(self._pressed_action and now >= self._pressed_until)
            if cursor_just_expired:
                self._cursor_feedback_mode = ""
            if button_just_expired:
                self._pressed_action = ""
        if cursor_animating or cursor_just_expired:
            self._draw_cursor()
        if button_animating or button_just_expired:
            self._draw_dock()
        try:
            self._root.after(16, self._drain_commands)
        except Exception:
            pass

    def _select_monitor(self) -> None:
        with self._state_lock:
            index = max(1, min(self._monitor_index, len(self._monitor_rects))) - 1
            self._monitor_index = index + 1
            self._monitor_rect = self._monitor_rects[index]

    def _expanded_dock_xy(self) -> tuple[int, int]:
        with self._state_lock:
            left, top, right, bottom = self._monitor_rect
            vertical = self._vertical_percent
        x = right - self.DOCK_WIDTH
        center_y = top + int((bottom - top) * vertical)
        y = max(top, min(bottom - self.DOCK_HEIGHT, center_y - self.DOCK_HEIGHT // 2))
        return x, y

    def _place_dock(self, *, immediate: bool) -> None:
        if not self._dock:
            return
        expanded_x, y = self._expanded_dock_xy()
        with self._state_lock:
            right = self._monitor_rect[2]
            target_x = right - self.ARROW_WIDTH if self._collapsed else expanded_x
        if immediate:
            self._dock.geometry(f"{self.DOCK_WIDTH}x{self.DOCK_HEIGHT}{target_x:+d}{y:+d}")
            with self._state_lock:
                self._dock_rect = (target_x, y, target_x + self.DOCK_WIDTH, y + self.DOCK_HEIGHT)

    def _animate_dock(self) -> None:
        if not self._dock:
            return
        self._animation_generation += 1
        generation = self._animation_generation
        try:
            start_x = int(self._dock.winfo_x())
            start_y = int(self._dock.winfo_y())
        except Exception:
            start_x, start_y = self._expanded_dock_xy()
        expanded_x, target_y = self._expanded_dock_xy()
        with self._state_lock:
            target_x = self._monitor_rect[2] - self.ARROW_WIDTH if self._collapsed else expanded_x

        steps = 18

        def frame(step: int) -> None:
            if generation != self._animation_generation or not self._dock:
                return
            t = step / steps
            eased = 1.0 - (1.0 - t) ** 3
            x = int(round(start_x + (target_x - start_x) * eased))
            y = int(round(start_y + (target_y - start_y) * eased))
            self._dock.geometry(f"{self.DOCK_WIDTH}x{self.DOCK_HEIGHT}{x:+d}{y:+d}")
            with self._state_lock:
                self._dock_rect = (x, y, x + self.DOCK_WIDTH, y + self.DOCK_HEIGHT)
            if step < steps:
                self._root.after(16, lambda: frame(step + 1))

        frame(1)

    def _apply_visibility(self) -> None:
        if not self._dock or not self._cursor:
            return
        with self._state_lock:
            visible = self._visible
            cursor_visible = self._cursor_visible and visible
        if visible:
            self._place_dock(immediate=True)
            self._dock.deiconify()
            self._dock.attributes("-topmost", True)
        else:
            self._dock.withdraw()
        if cursor_visible:
            self._place_cursor()
            self._cursor.deiconify()
            self._cursor.attributes("-topmost", True)
        else:
            self._cursor.withdraw()

    def _place_cursor(self) -> None:
        if not self._cursor:
            return
        with self._state_lock:
            x, y = self._cursor_point
            show = self._visible and self._cursor_visible
        half = self.CURSOR_SIZE // 2
        self._cursor.geometry(f"{self.CURSOR_SIZE}x{self.CURSOR_SIZE}{x-half:+d}{y-half:+d}")
        if show:
            self._cursor.deiconify()
            self._cursor.attributes("-topmost", True)
        else:
            self._cursor.withdraw()

    def _draw_cursor(self) -> None:
        canvas = self._cursor_canvas
        if canvas is None:
            return
        canvas.delete("all")
        size = self.CURSOR_SIZE
        center = size // 2
        now = time.monotonic()
        with self._state_lock:
            mode = self._cursor_mode
            feedback_mode = self._cursor_feedback_mode
            feedback_started = self._cursor_feedback_started
            feedback_until = self._cursor_feedback_until
        if feedback_mode and now < feedback_until:
            mode = feedback_mode
        color = "#00e5ff"
        if mode in {"precision", "clicking"}:
            color = "#3affc4"
        elif mode == "scrolling":
            color = "#ffb020"
        elif mode == "dragging":
            color = "#ffe05b"

        # Match the original browser cursor: one clean luminous ring, with a
        # subtle halo instead of the newer crosshair design.
        radius = 13.0
        if mode == "clicking":
            duration = max(0.001, feedback_until - feedback_started)
            progress = max(0.0, min(1.0, (now - feedback_started) / duration))
            pulse = math.sin(math.pi * progress)
            radius = 13.0 - 4.0 * pulse
            ripple = 15.0 + 7.0 * progress
            canvas.create_oval(center-ripple, center-ripple, center+ripple, center+ripple, outline="#176b5a", width=1)
            canvas.create_oval(center-4, center-4, center+4, center+4, fill="#3affc4", outline="")
        elif mode == "scrolling":
            canvas.create_arc(center-20, center-20, center+20, center+20, start=35, extent=100, style="arc", outline="#6e531c", width=2)
            canvas.create_arc(center-20, center-20, center+20, center+20, start=215, extent=100, style="arc", outline="#6e531c", width=2)
        elif mode == "dragging":
            radius = 15.0
            canvas.create_oval(center-20, center-20, center+20, center+20, outline="#5d5122", width=1)
        else:
            halo = "#075260" if mode == "normal" else "#176452"
            canvas.create_oval(center-18, center-18, center+18, center+18, outline=halo, width=1)
        canvas.create_oval(center-radius, center-radius, center+radius, center+radius, outline=color, width=2)

    @staticmethod
    def _rounded_rect(canvas: Any, x0: float, y0: float, x1: float, y1: float, radius: float, **kwargs: Any) -> int:
        radius = max(1.0, min(float(radius), (x1-x0)/2, (y1-y0)/2))
        points = [
            x0+radius,y0, x1-radius,y0, x1,y0, x1,y0+radius,
            x1,y1-radius, x1,y1, x1-radius,y1, x0+radius,y1,
            x0,y1, x0,y1-radius, x0,y0+radius, x0,y0,
        ]
        return int(canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs))

    def _draw_dock(self) -> None:
        canvas = self._dock_canvas
        if canvas is None:
            return
        canvas.delete("all")
        width, height = self.DOCK_WIDTH, self.DOCK_HEIGHT
        cyan = "#00b8d4"
        amber = "#ffb020"
        muted = "#587078"
        with self._state_lock:
            collapsed = self._collapsed
            states = dict(self._button_states)
            pressed = self._pressed_action if time.monotonic() < self._pressed_until else ""
        self._rounded_rect(canvas, 0, 0, width+2, height-1, 15, fill="#010b0e", outline="#07505b", width=1)
        arrow_down = pressed == "arrow"
        self._rounded_rect(
            canvas,
            5,
            7+(1 if arrow_down else 0),
            self.ARROW_WIDTH-5,
            height-7+(1 if arrow_down else 0),
            11,
            fill="#0a2b2e" if arrow_down else "#03181d",
            outline="#3affc4" if arrow_down else cyan,
            width=2 if arrow_down else 1,
        )
        canvas.create_text(
            self.ARROW_WIDTH//2,
            height//2+(1 if arrow_down else 0),
            text="‹" if collapsed else "›",
            fill="#3affc4" if arrow_down else amber,
            font=("Segoe UI", 20, "bold"),
        )
        for index, name in enumerate(self.ACTIONS):
            x0 = self.BUTTON_START + index * (self.BUTTON_WIDTH + self.BUTTON_GAP)
            x1 = x0 + self.BUTTON_WIDTH
            active = states.get(name, False)
            is_pressed = pressed == name
            color = "#3affc4" if active or is_pressed else amber
            y_shift = 1 if is_pressed else 0
            self._rounded_rect(
                canvas,
                x0+2,
                6+y_shift,
                x1-2,
                53+y_shift,
                10,
                fill="#0a2c2a" if is_pressed else ("#05201f" if active else "#021217"),
                outline="#3affc4" if is_pressed else ("#16816f" if active else "#073c45"),
                width=2 if is_pressed else 1,
            )
            cx = (x0 + x1) // 2
            self._draw_dock_icon(canvas, name, cx, 28+y_shift, color)
            canvas.create_text(cx, 63+y_shift, text=name.upper(), fill=color if active or is_pressed else muted, font=("Segoe UI", 6, "bold"))

    @staticmethod
    def _draw_dock_icon(canvas: Any, name: str, x: int, y: int, color: str) -> None:
        if name == "browser":
            JarvisWindowsOverlay._rounded_rect(canvas, x-10, y-8, x+10, y+8, 4, fill="", outline=color, width=2)
            canvas.create_line(x-4, y, x+4, y, fill=color, width=2)
            canvas.create_line(x, y-4, x, y+4, fill=color, width=2)
        elif name == "screen":
            JarvisWindowsOverlay._rounded_rect(canvas, x-12, y-9, x+12, y+7, 2, fill="", outline=color, width=2)
            canvas.create_line(x, y+7, x, y+11, fill=color, width=2)
            canvas.create_line(x-7, y+11, x+7, y+11, fill=color, width=2)
        elif name == "optics":
            JarvisWindowsOverlay._rounded_rect(canvas, x-13, y-8, x+8, y+8, 3, fill="", outline=color, width=2)
            canvas.create_oval(x-5, y-5, x+5, y+5, outline=color, width=1)
            canvas.create_polygon(x+8,y-5, x+14,y-8, x+14,y+8, x+8,y+5, outline=color, fill="#021217", width=2)
        elif name == "voice":
            JarvisWindowsOverlay._rounded_rect(canvas, x-5, y-10, x+5, y+3, 5, fill="", outline=color, width=2)
            canvas.create_arc(x-10, y-4, x+10, y+10, start=180, extent=180, style="arc", outline=color, width=2)
            canvas.create_line(x-10, y+3, x-10, y+4, fill=color, width=2)
            canvas.create_line(x+10, y+3, x+10, y+4, fill=color, width=2)
            canvas.create_line(x, y+10, x, y+13, fill=color, width=2)
            canvas.create_line(x-6, y+13, x+6, y+13, fill=color, width=2)

    def _on_dock_click(self, event: Any) -> None:
        local_x = int(event.x)
        if local_x < self.ARROW_WIDTH:
            self._flash_button("arrow")
            self._toggle_collapsed()
            return
        with self._state_lock:
            if self._collapsed:
                return
        index = int((local_x - self.BUTTON_START) // (self.BUTTON_WIDTH + self.BUTTON_GAP))
        if not 0 <= index < len(self.ACTIONS):
            return
        within = (local_x - self.BUTTON_START) % (self.BUTTON_WIDTH + self.BUTTON_GAP)
        if within < self.BUTTON_WIDTH:
            action = self.ACTIONS[index]
            self._flash_button(action)
            self._events.put({"action": action})


__all__ = ["JarvisWindowsOverlay"]

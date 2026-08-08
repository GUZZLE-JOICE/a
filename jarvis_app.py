"""Native Windows entry point for the installed JARVIS app.

The installed process owns both loopback services:
* 5015: private desktop-control/HUD bridge (never tunnel this port)
* 5005: AI-only share server that the user's existing tunnel may expose

The main interface runs in a native pywebview window backed by Edge WebView2,
so JARVIS has an application window instead of an Edge browser window.
"""

from __future__ import annotations

import threading
import ctypes
import json
import shutil
import time
import urllib.request

import jarvis_browser
import jarvis_remote_server

BUILD_ID = jarvis_browser.BUILD_ID


def _local_bridge_is_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5015/health", timeout=1.0) as response:
            data = json.loads(response.read(32 * 1024) or b"{}")
            return (
                response.status == 200
                and data.get("search_engine") == "playwright-edge"
                and data.get("build") == BUILD_ID
            )
    except Exception:
        return False


def _startup_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "JARVIS could not start", 0x10)
    except Exception:
        pass


def _run_remote() -> None:
    try:
        settings = jarvis_remote_server.config_snapshot()
        port = jarvis_remote_server.as_int(settings.get("remote_server_port"), 5005, 1024, 65535)
        jarvis_remote_server.serve_remote_ai(port=port)
    except OSError:
        # Another JARVIS instance (or another program) may already own the
        # share port. The private local-control app can still run normally.
        return


def _materialize_browser_extension() -> None:
    """Expose the bundled unpacked extension under LocalAppData when frozen."""
    source = jarvis_browser.APP_DIR / "browser_extension"
    target = jarvis_browser.CONFIG_PATH.parent / "browser_extension"
    if not source.is_dir() or source == target:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    except OSError:
        pass


def _wait_for_local_bridge(timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _local_bridge_is_running():
            return True
        time.sleep(0.10)
    return False


def _run_local_bridge() -> None:
    try:
        jarvis_browser.serve_local_bridge(port=5015, open_window=False)
    except OSError:
        return


def _open_native_window(url: str) -> None:
    """Run JARVIS in its own persisted WebView2 application window."""
    try:
        import webview
    except Exception as exc:
        raise RuntimeError("The JARVIS window engine is missing from this build.") from exc

    storage = jarvis_browser.CONFIG_PATH.parent / "webview_profile"
    storage.mkdir(parents=True, exist_ok=True)
    webview.create_window(
        "J.A.R.V.I.S.",
        url,
        width=1440,
        height=900,
        min_size=(900, 620),
        resizable=True,
        background_color="#03090b",
        text_select=True,
    )
    webview.start(
        gui="edgechromium",
        debug=False,
        private_mode=False,
        storage_path=str(storage),
    )


def main() -> None:
    if _local_bridge_is_running():
        try:
            _open_native_window("http://127.0.0.1:5015")
        except Exception as exc:
            _startup_error(str(exc))
        return

    _materialize_browser_extension()
    threading.Thread(target=_run_remote, name="jarvis-remote-ai", daemon=True).start()
    threading.Thread(target=_run_local_bridge, name="jarvis-local-bridge", daemon=True).start()
    if not _wait_for_local_bridge():
        _startup_error(
            "Port 5015 is already in use. Close any older JARVIS copy, then open J.A.R.V.I.S. again. "
            "The new app did not terminate or overwrite the other program."
        )
        return
    try:
        _open_native_window("http://127.0.0.1:5015")
    except Exception as exc:
        _startup_error(
            "JARVIS could not create its Windows app window. "
            "Make sure Microsoft Edge WebView2 Runtime is installed.\n\n" + str(exc)
        )


if __name__ == "__main__":
    main()

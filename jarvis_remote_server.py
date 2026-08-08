"""Public, AI-only J.A.R.V.I.S. Ollama server.

This process is deliberately separate from jarvis_browser.py.  It exposes only
the remote inference endpoints and the safe web client; it never launches apps,
reads local files, controls the mouse, or exposes the desktop bridge.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jarvis_accounts import AccountStore, DEFAULT_AZURE_VOICE


APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def locate_config() -> Path:
    directories = (
        (RUN_DIR,)
        if getattr(sys, "frozen", False)
        else (RUN_DIR, RUN_DIR.parent, APP_DIR)
    )
    for directory in directories:
        candidate = directory / "jarvis_server_config.json"
        if candidate.is_file():
            return candidate
    if getattr(sys, "frozen", False):
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or RUN_DIR)
        return local_appdata / "JARVIS" / "jarvis_server_config.json"
    return RUN_DIR / "jarvis_server_config.json"


CONFIG_PATH = locate_config()
WEB_CLIENT_PATH = APP_DIR / "jarvis_web.html"
ACCOUNT_DB_PATH = CONFIG_PATH.parent / "jarvis_accounts.sqlite3"
_ACCOUNT_STORE: AccountStore | None = None
CONFIG_LOCK = threading.RLock()
DEFAULTS: dict[str, Any] = {
    "access_code": "",
    "remote_server_port": 5005,
    "ollama_base_url": "http://127.0.0.1:11434",
    "remote_ollama_model": "llava:7b",
    "remote_request_timeout_seconds": 180,
    "remote_max_prompt_chars": 24000,
    "remote_max_image_bytes": 6 * 1024 * 1024,
    "remote_rate_limit_per_minute": 18,
}


def config_snapshot() -> dict[str, Any]:
    """Reload settings so a code refresh takes effect immediately."""
    with CONFIG_LOCK:
        settings = dict(DEFAULTS)
        sources = []
        embedded = APP_DIR / "jarvis_server_config.json"
        if getattr(sys, "frozen", False) and embedded != CONFIG_PATH:
            sources.append(embedded)
        sources.append(CONFIG_PATH)
        for source in sources:
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    settings.update(data)
            except (OSError, ValueError):
                pass
        return settings


def as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, collections.deque[float]] = {}

    def allow(self, client: str, per_minute: int) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            history = self._requests.setdefault(client, collections.deque())
            while history and history[0] < cutoff:
                history.popleft()
            if len(history) >= per_minute:
                return False
            history.append(now)
            # Keep memory bounded if the link is shared widely.
            if len(self._requests) > 500:
                self._requests = {key: values for key, values in self._requests.items() if values and values[-1] >= cutoff}
            return True


RATE_LIMITER = RateLimiter()
# One active generation keeps a shared GPU responsive.  Ollama itself still
# owns the model and VRAM; a second request receives a clear "busy" response.
GENERATION_GATE = threading.BoundedSemaphore(1)
AUTH_RATE_LIMITER = RateLimiter()


def account_store() -> AccountStore:
    global _ACCOUNT_STORE
    if _ACCOUNT_STORE is None:
        _ACCOUNT_STORE = AccountStore(ACCOUNT_DB_PATH)
    return _ACCOUNT_STORE


def remote_client_key(handler: BaseHTTPRequestHandler) -> str:
    # Cloudflare Tunnel and ngrok commonly provide one of these.  This is a
    # best-effort fairness limit, not an authorization mechanism.
    value = (handler.headers.get("CF-Connecting-IP") or handler.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return value[:120] or str(handler.client_address[0])


def allowed_options(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    if "num_predict" in raw:
        result["num_predict"] = as_int(raw.get("num_predict"), 160, 1, 512)
    if "temperature" in raw:
        try:
            result["temperature"] = max(0.0, min(1.5, float(raw.get("temperature"))))
        except (TypeError, ValueError):
            pass
    return result


def call_ollama(settings: dict[str, Any], prompt: str, images: list[str], options: dict[str, Any]) -> dict[str, Any]:
    base_url = str(settings.get("ollama_base_url") or DEFAULTS["ollama_base_url"]).rstrip("/")
    model = str(settings.get("remote_ollama_model") or DEFAULTS["remote_ollama_model"]).strip()
    if not model:
        raise ValueError("Set remote_ollama_model in jarvis_server_config.json first.")
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if images:
        payload["images"] = images
    if options:
        payload["options"] = options
    timeout = as_int(settings.get("remote_request_timeout_seconds"), 180, 15, 600)
    request = urllib.request.Request(
        base_url + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(str(detail or "Ollama rejected the request.")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Could not reach Ollama on this computer. Start Ollama and make sure the selected model is installed.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("response"), str):
        raise RuntimeError("Ollama returned an invalid response.")
    return {"response": data["response"].strip(), "model": model}


def call_azure_speech(user_id: int, text: str) -> bytes:
    """Synthesize speech with the Azure key attached to this account.

    The key is decrypted only inside the server process and never crosses the
    profile API back to the browser/app client.
    """
    clean = str(text or "").strip()
    if not clean or len(clean) > 2200:
        raise ValueError("Voice text must be between 1 and 2200 characters.")
    secret = account_store().voice_secret(user_id)
    region = secret["region"]
    voice = secret.get("voice") or DEFAULT_AZURE_VOICE
    ssml = (
        "<speak version='1.0' xml:lang='en-US'>"
        f"<voice name='{html.escape(voice, quote=True)}'>"
        f"<lang xml:lang='en-GB'>{html.escape(clean)}</lang>"
        "</voice></speak>"
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml,
        headers={
            "Ocp-Apim-Subscription-Key": secret["key"],
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "User-Agent": "JARVIS/3.3.3",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            audio = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("Azure rejected this Speech key or region. Check Settings → Voice.") from exc
        raise RuntimeError("Azure Speech could not synthesize this response.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Azure Speech is unreachable right now.") from exc
    if not audio:
        raise RuntimeError("Azure Speech returned empty audio.")
    return audio


class RemoteHandler(BaseHTTPRequestHandler):
    server_version = "JarvisRemoteAI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[JARVIS REMOTE] " + (fmt % args))

    def _cors(self) -> None:
        # There are no cookies or browser credentials.  The owner access code
        # is the explicit bearer credential for remote inference.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Jarvis-Code, ngrok-skip-browser-warning")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, data: dict[str, Any], status: int = 200, *, cors: bool = False) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self._cors()
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self) -> dict[str, Any]:
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("JSON request required.")
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("Invalid request size.") from exc
        if length < 1 or length > 12 * 1024 * 1024:
            raise ValueError("Request is too large.")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object required.")
        return data

    def _audio(self, audio: bytes, *, cors: bool = True) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self._cors()
        self.end_headers()
        self.wfile.write(audio)

    def _profile_user(self) -> Any | None:
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        token = authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""
        user = account_store().user_for_token(token)
        if user is None:
            self._json({"error": "Log in to your JARVIS account first."}, HTTPStatus.UNAUTHORIZED, cors=True)
        return user

    def _authorized(self, settings: dict[str, Any]) -> bool:
        expected = str(settings.get("access_code") or "")
        supplied = self.headers.get("X-Jarvis-Code", "")
        return len(expected) >= 8 and bool(supplied) and secrets.compare_digest(supplied, expected)

    def _need_code(self, settings: dict[str, Any]) -> bool:
        if self._authorized(settings):
            return False
        self._json({"error": "That Jarvis access code was not accepted."}, HTTPStatus.FORBIDDEN, cors=True)
        return True

    def _serve_web_client(self) -> None:
        if not WEB_CLIENT_PATH.is_file():
            self._json({"error": "jarvis_web.html is missing from this server."}, HTTPStatus.NOT_FOUND)
            return
        data = WEB_CLIENT_PATH.read_bytes()
        content_type = mimetypes.guess_type(WEB_CLIENT_PATH.name)[0] or "text/html"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/remote/") or path.startswith("/profile/"):
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/web", "/web/"}:
            self._serve_web_client()
            return
        if path.startswith("/remote/"):
            self._json({"error": "Use POST for this endpoint."}, HTTPStatus.METHOD_NOT_ALLOWED, cors=True)
            return
        if path == "/profile/data":
            user = self._profile_user()
            if user is not None:
                self._json({"ok": True, "data": account_store().get_profile(int(user["id"]))}, cors=True)
            return
        if path == "/profile/voice/status":
            user = self._profile_user()
            if user is not None:
                self._json({"ok": True, **account_store().voice_status(int(user["id"]))}, cors=True)
            return
        if path.startswith("/profile/"):
            self._json({"error": "Unknown JARVIS account route."}, HTTPStatus.NOT_FOUND, cors=True)
            return
        self._json({"error": "Unknown JARVIS remote route."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        settings = config_snapshot()
        if path in {"/profile/register", "/profile/login"}:
            if not AUTH_RATE_LIMITER.allow(remote_client_key(self), 12):
                self._json({"error": "Too many account attempts. Try again in a minute."}, HTTPStatus.TOO_MANY_REQUESTS, cors=True)
                return
            try:
                data = self._read_body()
                if path.endswith("/register"):
                    result = account_store().register(data.get("username", ""), data.get("password", ""))
                else:
                    result = account_store().login(data.get("username", ""), data.get("password", ""))
                self._json({"ok": True, **result}, cors=True)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, cors=True)
            return
        if path == "/profile/data":
            user = self._profile_user()
            if user is None:
                return
            try:
                data = self._read_body()
                profile = data.get("data")
                if not isinstance(profile, dict):
                    raise ValueError("Profile data must be an object.")
                account_store().set_profile(int(user["id"]), profile)
                self._json({"ok": True}, cors=True)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, cors=True)
            return
        if path == "/profile/voice/config":
            user = self._profile_user()
            if user is None:
                return
            try:
                data = self._read_body()
                account_store().set_voice(
                    int(user["id"]),
                    data.get("key", ""),
                    data.get("region", ""),
                    data.get("voice", DEFAULT_AZURE_VOICE),
                )
                self._json({"ok": True, **account_store().voice_status(int(user["id"]))}, cors=True)
            except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, cors=True)
            return
        if path == "/profile/voice/remove":
            user = self._profile_user()
            if user is None:
                return
            account_store().clear_voice(int(user["id"]))
            self._json({"ok": True, "configured": False}, cors=True)
            return
        if path == "/profile/voice/synthesize":
            user = self._profile_user()
            if user is None:
                return
            if not RATE_LIMITER.allow("voice:" + remote_client_key(self), 30):
                self._json({"error": "Voice is busy. Try again in a moment."}, HTTPStatus.TOO_MANY_REQUESTS, cors=True)
                return
            try:
                data = self._read_body()
                self._audio(call_azure_speech(int(user["id"]), data.get("text", "")))
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY, cors=True)
            return
        if path == "/remote/health":
            if self._need_code(settings):
                return
            self._json({
                "ok": True,
                "service": "jarvis-remote-ai-only",
                "model": str(settings.get("remote_ollama_model") or DEFAULTS["remote_ollama_model"]),
                "web_path": "/web",
            }, cors=True)
            return
        if path != "/remote/generate":
            self._json({"error": "Unknown JARVIS remote route."}, HTTPStatus.NOT_FOUND, cors=True)
            return
        if self._need_code(settings):
            return
        try:
            data = self._read_body()
            prompt = str(data.get("prompt") or "").strip()
            max_prompt = as_int(settings.get("remote_max_prompt_chars"), 24000, 1000, 100000)
            if not prompt:
                raise ValueError("A prompt is required.")
            if len(prompt) > max_prompt:
                raise ValueError("That prompt is too long for this shared server.")
            raw_images = data.get("images") or []
            if not isinstance(raw_images, list) or not all(isinstance(image, str) for image in raw_images):
                raise ValueError("Images must be a list of base64 strings.")
            max_image_bytes = as_int(settings.get("remote_max_image_bytes"), 6 * 1024 * 1024, 0, 20 * 1024 * 1024)
            image_limit = max_image_bytes * 4 // 3
            if len(raw_images) > 4 or sum(len(image) for image in raw_images) > image_limit:
                raise ValueError("Images are too large for this shared server.")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST, cors=True)
            return

        limit = as_int(settings.get("remote_rate_limit_per_minute"), 18, 1, 120)
        if not RATE_LIMITER.allow(remote_client_key(self), limit):
            self._json({"error": "This shared Jarvis server is rate-limited. Try again in a minute."}, HTTPStatus.TOO_MANY_REQUESTS, cors=True)
            return
        if not GENERATION_GATE.acquire(blocking=False):
            self._json({"error": "Jarvis is answering someone else right now. Please try again in a moment."}, HTTPStatus.TOO_MANY_REQUESTS, cors=True)
            return
        try:
            result = call_ollama(settings, prompt, raw_images, allowed_options(data.get("options")))
            self._json({"ok": True, **result}, cors=True)
        except (RuntimeError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY, cors=True)
        finally:
            GENERATION_GATE.release()


def serve_remote_ai(*, port: int | None = None) -> None:
    """Run the AI-only share server inside either its own process or JARVIS.exe."""
    settings = config_snapshot()
    selected_port = as_int(
        port if port is not None else settings.get("remote_server_port"),
        5005,
        1024,
        65535,
    )
    server = ThreadingHTTPServer(("127.0.0.1", selected_port), RemoteHandler)
    server.daemon_threads = True
    print(f"J.A.R.V.I.S remote AI server: http://127.0.0.1:{selected_port}/web")
    print("Tunnel this port only. Do not tunnel the local desktop bridge on port 5015.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    settings = config_snapshot()
    parser = argparse.ArgumentParser(description="Run the public AI-only JARVIS Ollama server")
    parser.add_argument("--port", type=int, default=as_int(settings.get("remote_server_port"), 5005, 1024, 65535))
    args = parser.parse_args()
    serve_remote_ai(port=int(args.port))


if __name__ == "__main__":
    main()

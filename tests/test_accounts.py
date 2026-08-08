from __future__ import annotations

import tempfile
import json
from pathlib import Path
import threading
import unittest
import urllib.request

from http.server import ThreadingHTTPServer

from jarvis_accounts import AccountStore
import jarvis_remote_server


class AccountStoreTests(unittest.TestCase):
    def test_account_login_profile_round_trip_and_secret_free_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(Path(directory) / "accounts.sqlite3")
            registered = store.register("test_user", "correct-horse")
            self.assertTrue(registered["token"])
            user = store.user_for_token(registered["token"])
            self.assertIsNotNone(user)
            store.set_profile(int(user["id"]), {"theme": "Gamma", "font": "Rajdhani"})
            self.assertEqual(store.get_profile(int(user["id"]))["theme"], "Gamma")
            logged_in = store.login("TEST_USER", "correct-horse")
            self.assertTrue(store.user_for_token(logged_in["token"]))
            status = store.voice_status(int(user["id"]))
            self.assertFalse(status["configured"])
            self.assertNotIn("key", status)

    def test_wrong_password_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(Path(directory) / "accounts.sqlite3")
            store.register("another_user", "correct-horse")
            with self.assertRaises(ValueError):
                store.login("another_user", "wrong-password")

    def test_profile_http_api_registers_and_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jarvis_remote_server._ACCOUNT_STORE = AccountStore(Path(directory) / "server-accounts.sqlite3")
            server = ThreadingHTTPServer(("127.0.0.1", 0), jarvis_remote_server.RemoteHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                register = urllib.request.Request(
                    base + "/profile/register",
                    data=json.dumps({"username": "http_user", "password": "correct-horse"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(register, timeout=3) as response:
                    created = json.loads(response.read())
                token = created["token"]
                save = urllib.request.Request(
                    base + "/profile/data",
                    data=json.dumps({"data": {"theme": "Gamma", "browsing": "external"}}).encode(),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
                    method="POST",
                )
                with urllib.request.urlopen(save, timeout=3) as response:
                    self.assertTrue(json.loads(response.read())["ok"])
                load = urllib.request.Request(
                    base + "/profile/data",
                    headers={"Authorization": "Bearer " + token},
                )
                with urllib.request.urlopen(load, timeout=3) as response:
                    loaded = json.loads(response.read())
                self.assertEqual(loaded["data"]["theme"], "Gamma")
                self.assertEqual(response.headers.get("Access-Control-Allow-Methods"), "GET, POST, OPTIONS")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                jarvis_remote_server._ACCOUNT_STORE = None


if __name__ == "__main__":
    unittest.main()

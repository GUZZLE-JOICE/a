from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "jarvis_v2.html").read_text(encoding="utf-8")
BROWSER = (ROOT / "jarvis_browser.py").read_text(encoding="utf-8")
FINAL_WORKFLOW = (ROOT / ".github" / "workflows" / "JARVIS-FINAL-BUILD.yml").read_text(encoding="utf-8")
APP = (ROOT / "jarvis_app.py").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
WIX = (ROOT / "installer" / "Package.wxs").read_text(encoding="utf-8")


class ClassicUiRegressionTests(unittest.TestCase):
    def test_classic_startup_sequence_is_present(self) -> None:
        for marker in ('id="startGate"', 'id="initBtn"', "INITIALIZE SYSTEM", 'id="bootOverlay"'):
            self.assertIn(marker, HTML)

    def test_compact_settings_rectangle_is_restored(self) -> None:
        self.assertRegex(HTML, r"#memPanel\{[^}]*width:min\(760px,calc\(100vw - 86px\)\)")
        self.assertRegex(HTML, r"#memPanel\{[^}]*height:min\(650px,calc\(100vh - 36px\)\)")
        self.assertNotIn("height:min(900px", HTML)

    def test_settings_scroll_viewport_is_grid_bounded(self) -> None:
        self.assertIn("grid-template-rows:54px minmax(0,1fr)", HTML)
        self.assertRegex(HTML, r"\.settingsPages\{[^}]*min-height:0")
        self.assertRegex(HTML, r"\.settingsPages\{[^}]*overflow-y:auto")
        self.assertNotRegex(HTML, r"\.settingsPages\{[^}]*height:100%")
        self.assertNotIn("installSettingsWheelFallback", HTML)
        self.assertNotIn("panel.addEventListener('wheel'", HTML)

    def test_colors_and_fonts_use_full_card_hitboxes(self) -> None:
        self.assertRegex(HTML, r"\.themeOption\{[^}]*display:block[^}]*width:100%[^}]*pointer-events:auto")
        self.assertRegex(HTML, r"\.fontOption\{[^}]*display:block[^}]*width:100%[^}]*pointer-events:auto")
        self.assertIn(".themeOption>*,.themeOption>* *,.fontOption>*,.fontOption>* *{pointer-events:none}", HTML)
        self.assertIn("b.addEventListener('click',function(){applyTheme(t,true);});", HTML)
        self.assertIn("b.addEventListener('click',function(){applyFont(n,true);});", HTML)

    def test_hover_never_applies_a_font_or_theme(self) -> None:
        self.assertNotRegex(HTML, r"(?:mouseenter|mouseover)[^\n]{0,300}applyFont")
        self.assertNotRegex(HTML, r"(?:mouseenter|mouseover)[^\n]{0,300}applyTheme")

    def test_font_preview_text_and_per_card_family_are_present(self) -> None:
        self.assertIn("Aa Bb Cc · JARVIS 0123456789", HTML)
        self.assertIn("b.style.setProperty('font-family',\"'\"+n+\"', sans-serif\",'important')", HTML)


class AppPackagingRegressionTests(unittest.TestCase):
    def test_native_webview_window_is_used(self) -> None:
        self.assertIn("import webview", APP)
        self.assertIn("webview.create_window", APP)
        self.assertIn('gui="edgechromium"', APP)
        self.assertIn("private_mode=False", APP)
        self.assertIn("pywebview>=6.2.1,<7", REQUIREMENTS)
        self.assertNotIn("open_app_window(", APP)

    def test_no_windows_autostart_or_shell_replacement_code(self) -> None:
        joined = "\n".join(p.read_text(encoding="utf-8") for p in ROOT.glob("*.py"))
        forbidden = ("Winlogon", "SetValueEx", "schtasks", "CurrentVersion\\\\Run", "explorer.exe /shell")
        for marker in forbidden:
            self.assertNotIn(marker.lower(), joined.lower())

    def test_factory_config_contains_no_personal_tokens(self) -> None:
        config = json.loads((ROOT / "jarvis_server_config.json").read_text(encoding="utf-8"))
        self.assertEqual(config.get("access_code"), "")
        self.assertEqual(config.get("browser_extension_pairing_code"), "")
        serialized = json.dumps(config).lower()
        for marker in ("api_key", "oauth_token", "refresh_token", "password"):
            self.assertNotIn(marker, serialized)

    def test_workflow_builds_the_files_that_exist(self) -> None:
        for filename in (
            "jarvis_app.py",
            "jarvis_v2.html",
            "jarvis_web.html",
            "jarvis_server_config.json",
            "manifest.webmanifest",
            "service-worker.js",
        ):
            self.assertTrue((ROOT / filename).is_file(), filename)
            self.assertIn(filename, FINAL_WORKFLOW)
        self.assertTrue((ROOT / "browser_extension").is_dir())
        self.assertIn("browser_extension:browser_extension", FINAL_WORKFLOW)

    def test_installer_build_is_the_downloaded_artifact(self) -> None:
        wixproj = (ROOT / "installer" / "JARVIS.Installer.wixproj").read_text(encoding="utf-8")
        self.assertIn("WixToolset.Sdk/6.0.2", wixproj)
        self.assertNotIn("<SuppressIces>", wixproj)
        self.assertIn('Scope="perMachine"', WIX)
        self.assertIn('Version="3.3.3"', WIX)
        self.assertIn('StandardDirectory Id="ProgramFiles64Folder"', WIX)
        self.assertIn('StandardDirectory Id="ProgramMenuFolder"', WIX)
        self.assertIn('Name="J.A.R.V.I.S."', WIX)
        self.assertIn('KeyPath="yes"', WIX)
        self.assertRegex(WIX, r'<File Id="JarvisExe"[^>]*>\s*<Shortcut')
        self.assertIn('Advertise="yes"', WIX)
        self.assertNotIn('Component Id="StartMenuShortcut"', WIX)
        self.assertNotIn('Target="[#JarvisExe]"', WIX)
        self.assertNotIn('LocalAppDataFolder', WIX)
        self.assertIn("dotnet build installer/JARVIS.Installer.wixproj", FINAL_WORKFLOW)
        self.assertIn("JARVIS_V3_3_CLASSIC_APP.msi", FINAL_WORKFLOW)
        self.assertIn("actions/upload-artifact@v7", FINAL_WORKFLOW)
        self.assertIn("archive: false", FINAL_WORKFLOW)

    def test_final_workflow_guards_the_exact_mcp_cli_failure(self) -> None:
        self.assertIn("Build JARVIS V3.3 Installed Windows App", FINAL_WORKFLOW)
        self.assertIn('mcp[cli]>=2.0.0,<3', FINAL_WORKFLOW)
        self.assertIn("import typer", FINAL_WORKFLOW)
        self.assertNotIn("--collect-all mcp", FINAL_WORKFLOW)
        self.assertNotIn("--collect-all httpx2", FINAL_WORKFLOW)
        self.assertIn("--hidden-import mcp.client.streamable_http", FINAL_WORKFLOW)
        self.assertIn("--hidden-import httpx2", FINAL_WORKFLOW)
        self.assertIn("--hidden-import jarvis_accounts", FINAL_WORKFLOW)

    def test_account_and_optional_azure_voice_are_wired(self) -> None:
        self.assertIn('data-settings-page="account"', HTML)
        self.assertIn('data-page="account"', HTML)
        self.assertIn('id="azureSpeechKey"', HTML)
        self.assertIn("/profile/voice/synthesize", HTML)
        self.assertIn("en-US-RyanMultilingualNeural", HTML)
        self.assertIn("mergeProfileData", HTML)
        self.assertIn("isPersonalProfileInfo", HTML)


if __name__ == "__main__":
    unittest.main()

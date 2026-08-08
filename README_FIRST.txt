JARVIS V3.3 CLASSIC APP
=======================

WHAT THIS VERSION IS
--------------------
This build deliberately restores the classic JARVIS interface:
- the old INITIALIZE SYSTEM startup screen and initialization sequence
- the old dark teal/black HUD, radar/core and panels
- the old compact rectangular Settings overlay (760 x 650 maximum)
- the existing lower-right controls and classic layout

The code underneath the interface is updated, but the interface was not
redesigned into the V3/V3.1 full-page layout.

IMPORTANT SAFETY/STARTUP CHANGE
-------------------------------
The JARVIS app does NOT add itself to Windows Startup and does NOT replace
Windows Explorer or the Windows shell. The startup restored here is the
on-screen JARVIS initialization sequence only.

WHAT "APP" MEANS IN THIS VERSION
--------------------------------
JARVIS now runs in its own native Windows window using Edge WebView2. It does
not open the main interface as an Edge browser tab/window. GitHub then wraps
that app in a real Windows Installer (.msi), which installs JARVIS under
Program Files and creates a Start-menu entry plus an Installed Apps/uninstall
entry. Windows will show the normal UAC installer prompt once during install.

BUILD AND INSTALL IT — NO VISUAL STUDIO OR LOCAL PYTHON
-------------------------------------------------------
1. Make a NEW empty GitHub repository.
2. Upload EVERYTHING inside this folder. Keep the folders exactly as provided,
   including .github/workflows, browser_extension, installer, and tests.
3. Commit all of it to the main branch.
4. Open the Actions tab.
5. Open "Build JARVIS V3.3 Installed Windows App".
6. Click "Run workflow", choose main, then click the green Run workflow button.
7. Wait for the build to turn green.
8. Open the finished run and download JARVIS_V3_3_CLASSIC_APP.msi.
9. Double-click the MSI and install it.
10. Open J.A.R.V.I.S. from the Windows Start menu like a normal installed app.

The PC that RUNS the installed app does not need Visual Studio or Python.
Ollama is only required on the host PC providing the local model. Other people
can use the AI-only sharing link/code without installing Ollama on their PC.

Windows 11 already includes the Evergreen WebView2 Runtime. Most Windows 10
systems also already have it. If an older Windows 10 PC reports that WebView2
is missing, install Microsoft's Evergreen WebView2 Runtime once.

SETTINGS FIX
------------
Colors and Fonts use the same 760 x 650 settings rectangle as before. The
scroll viewport is now constrained to the grid row below the settings header,
instead of incorrectly using 100% of the whole panel height. It uses plain
native overflow-y:auto, the same normal scrolling behavior as the other
Settings pages. The old special capture-phase wheel handler was removed. The
panel is NOT made taller to fake scrolling. Whole color/font cards are
clickable; hover never applies a selection.

ACCOUNT + OPTIONAL AZURE VOICE
------------------------------
- Settings now has an Account page, so login/create-account/sync/logout can be
  done without switching to Chat.
- Login is merge-first: existing local chats, memory and saved appearance/
  browsing choices are merged before the combined copy is synced.
- Personal learned information asks for account login only when signed out.
- The account service is hosted by the owner's JARVIS server on port 5005.
  Passwords are PBKDF2 hashed and sessions use random bearer tokens.
- Settings > Voice has an Azure section at the bottom. Azure is optional.
  Without it JARVIS keeps the built-in system voice.
- Azure keys are never saved in HTML, localStorage, GitHub source, or profile
  JSON. They are encrypted by Windows DPAPI in the JARVIS account database and
  the client can only see whether Azure voice is configured.
- Default Azure voice: en-US-RyanMultilingualNeural with British-English SSML.
  Andrew Multilingual and Brian Multilingual are selectable alternatives.
- If Azure synthesis fails, JARVIS automatically uses its built-in voice.

FILES / SETTINGS
----------------
The installed app contains the UI and support files. User-changing settings are stored in
the user's Windows profile so they survive app updates. The factory config in
this source package contains no personal API keys or passwords.

const baseInput = document.getElementById("baseUrl");
const codeInput = document.getElementById("pairingCode");
const status = document.getElementById("status");

function show(text, kind) {
  status.textContent = text;
  status.className = "status" + (kind ? " " + kind : "");
}

function settings() {
  return new Promise((resolve) => chrome.storage.local.get({ baseUrl: "http://127.0.0.1:5015", pairingCode: "" }, resolve));
}

async function localRequest(path, options = {}) {
  const values = await settings();
  const headers = Object.assign({}, options.headers || {}, { "X-Jarvis-Extension": values.pairingCode });
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(values.baseUrl.replace(/\/$/, "") + path, Object.assign({}, options, { headers }));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Bridge returned ${response.status}.`);
  return data;
}

settings().then((values) => {
  baseInput.value = values.baseUrl;
  codeInput.value = values.pairingCode;
});

document.getElementById("connect").addEventListener("click", async () => {
  const baseUrl = baseInput.value.trim().replace(/\/$/, "");
  const pairingCode = codeInput.value.trim();
  if (!/^http:\/\/(?:127\.0\.0\.1|localhost):\d+$/i.test(baseUrl)) {
    show("Use the local JARVIS address, for example http://127.0.0.1:5015.", "bad");
    return;
  }
  if (pairingCode.length < 12) {
    show("Paste the browser pairing code from JARVIS Settings → Server Sharing.", "bad");
    return;
  }
  await chrome.storage.local.set({ baseUrl, pairingCode });
  try {
    await localRequest("/extension/register", { method: "POST", body: JSON.stringify({ name: "JARVIS Local Browser Link" }) });
    chrome.runtime.sendMessage({ type: "jarvis-poll-now" });
    show("Connected to local JARVIS.", "ok");
  } catch (error) {
    show(error.message || "Could not connect.", "bad");
  }
});

document.getElementById("readPage").addEventListener("click", () => {
  chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
    if (!tabs || !tabs[0] || !tabs[0].id) { show("No active browser tab.", "bad"); return; }
    chrome.tabs.sendMessage(tabs[0].id, { type: "jarvis-command", command: { operation: "read_page" } }, (result) => {
      if (chrome.runtime.lastError) { show("This browser page cannot be read.", "bad"); return; }
      if (!result || !result.ok) { show((result && result.message) || "Could not read this page.", "bad"); return; }
      show((result.data && result.data.title) ? `Ready: ${result.data.title}` : "Page read.", "ok");
    });
  });
});

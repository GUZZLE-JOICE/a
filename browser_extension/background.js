const DEFAULTS = {
  baseUrl: "http://127.0.0.1:5015",
  pairingCode: ""
};

let polling = false;
let pollTimer = null;
const POLL_INTERVAL_MS = 2500;

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULTS, (settings) => resolve(settings));
  });
}

async function bridgeFetch(path, options = {}) {
  const settings = await getSettings();
  if (!settings.pairingCode) throw new Error("Paste the pairing code first.");
  const headers = Object.assign({}, options.headers || {}, {
    "X-Jarvis-Extension": settings.pairingCode
  });
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(settings.baseUrl.replace(/\/$/, "") + path, Object.assign({}, options, { headers }));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Bridge returned ${response.status}.`);
  return data;
}

function activeTab() {
  return new Promise((resolve, reject) => {
    chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => {
      if (chrome.runtime.lastError || !tabs || !tabs[0] || !tabs[0].id) {
        reject(new Error("No active browser tab is available."));
        return;
      }
      resolve(tabs[0]);
    });
  });
}

function sendToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (result) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(result || { ok: false, message: "The page did not respond." });
    });
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function waitForTabComplete(tabId, timeoutMs = 14000) {
  return new Promise((resolve) => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      clearTimeout(timer);
      resolve();
    };
    const onUpdated = (updatedId, changeInfo) => {
      if (updatedId === tabId && changeInfo.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.get(tabId, (current) => {
      if (!chrome.runtime.lastError && current && current.status === "complete") finish();
    });
  });
}

async function executeCommand(command) {
  const tab = await activeTab();
  if (command.operation === "research_model") {
    const url = "https://makerworld.com/en/search/models?keyword=" + encodeURIComponent(command.query || "3d model");
    const researchTab = await chrome.tabs.create({ url, active: true });
    await waitForTabComplete(researchTab.id);
    await delay(1800);
    let result = await sendToTab(researchTab.id, { type: "jarvis-command", command: { operation: "click_best_model", query: command.query || "3d model" } });
    if (!result || !result.ok) {
      await delay(1800);
      result = await sendToTab(researchTab.id, { type: "jarvis-command", command: { operation: "click_best_model", query: command.query || "3d model" } });
    }
    return result;
  }
  if (command.operation === "research_web") {
    const url = "https://www.google.com/search?q=" + encodeURIComponent(command.query || "");
    const researchTab = await chrome.tabs.create({ url, active: true });
    await waitForTabComplete(researchTab.id);
    await delay(700);
    let result = await sendToTab(researchTab.id, { type: "jarvis-command", command: { operation: "click_best_search_result", query: command.query || "" } });
    if (!result || !result.ok) {
      await delay(900);
      result = await sendToTab(researchTab.id, { type: "jarvis-command", command: { operation: "click_best_search_result", query: command.query || "" } });
    }
    return result;
  }
  if (command.operation === "open_url") {
    await chrome.tabs.update(tab.id, { url: command.url });
    return { ok: true, message: "Opened the requested page.", data: { url: command.url } };
  }
  return sendToTab(tab.id, { type: "jarvis-command", command });
}

async function reportResult(command, result) {
  try {
    await bridgeFetch("/extension/result", {
      method: "POST",
      body: JSON.stringify({
        command_id: command.command_id,
        ok: !!(result && result.ok),
        message: (result && result.message) || "Browser command finished.",
        data: (result && result.data) || {}
      })
    });
  } catch (error) {
    // The next poll reconnects after the local app is started again.
  }
}

async function pollOnce() {
  if (polling) return;
  polling = true;
  try {
    const packet = await bridgeFetch("/extension/next", { method: "GET" });
    if (packet.command) {
      let result;
      try {
        result = await executeCommand(packet.command);
      } catch (error) {
        result = { ok: false, message: error.message || "Browser command failed." };
      }
      await reportResult(packet.command, result);
    }
  } catch (error) {
    // The extension stays quiet while JARVIS is not running or is not paired.
  } finally {
    polling = false;
  }
}

async function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  const settings = await getSettings();
  if (!settings.pairingCode) return;
  pollOnce();
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("jarvis-poll-backup", { periodInMinutes: 1 });
  startPolling();
});
chrome.runtime.onStartup.addListener(startPolling);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "jarvis-poll-backup") {
    pollOnce();
    startPolling();
  }
});
chrome.storage.onChanged.addListener(startPolling);
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "jarvis-poll-now") {
    pollOnce().finally(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

startPolling();

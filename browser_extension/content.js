function visible(element) {
  if (!element || !(element instanceof HTMLElement)) return false;
  const style = window.getComputedStyle(element);
  const box = element.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && box.width > 1 && box.height > 1;
}

function normalise(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function pageSnapshot() {
  const selection = normalise(window.getSelection && window.getSelection().toString());
  const text = normalise(document.body && document.body.innerText).slice(0, 12000);
  return {
    title: document.title || "Untitled page",
    url: location.href,
    selection: selection.slice(0, 4000),
    text
  };
}

function findClickable(text) {
  const wanted = normalise(text).toLowerCase();
  const candidates = Array.from(document.querySelectorAll('button, a, input[type="button"], input[type="submit"], [role="button"]'))
    .filter(visible)
    .map((element) => ({
      element,
      text: normalise(element.innerText || element.value || element.getAttribute("aria-label") || "").toLowerCase()
    }))
    .filter((candidate) => candidate.text);
  return candidates.find((candidate) => candidate.text === wanted) ||
    candidates.find((candidate) => candidate.text.includes(wanted)) ||
    candidates.find((candidate) => wanted.includes(candidate.text)) || null;
}

function queryTerms(value) {
  return normalise(value).toLowerCase().split(/[^a-z0-9]+/).filter((term) => term.length >= 2 && !["the", "best", "good", "for", "find", "model"].includes(term));
}

function scoreLink(element, terms, makerWorldOnly) {
  const text = normalise(element.innerText || element.getAttribute("aria-label") || "").toLowerCase();
  let url;
  try { url = new URL(element.href, location.href); } catch (error) { return -10000; }
  if (!/^https?:$/.test(url.protocol)) return -10000;
  if (makerWorldOnly) {
    if (!/(^|\.)makerworld\.com$/i.test(url.hostname) || !/\/models\//i.test(url.pathname) || /\/search\//i.test(url.pathname)) return -10000;
  } else {
    const blockedSearchHost = /(^|\.)(google\.|bing\.com$|duckduckgo\.com$)/i.test(url.hostname);
    if (blockedSearchHost || url.hostname === location.hostname) return -10000;
  }
  let score = text ? 5 : 0;
  terms.forEach((term) => {
    if (text.includes(term)) score += 12;
    if (url.pathname.toLowerCase().includes(term)) score += 4;
  });
  if (makerWorldOnly && /\/models\//i.test(url.pathname)) score += 30;
  return score;
}

function clickBestLink(query, makerWorldOnly) {
  const terms = queryTerms(query);
  const ranked = Array.from(document.querySelectorAll("a[href]"))
    .filter(visible)
    .map((element) => ({ element, score: scoreLink(element, terms, makerWorldOnly) }))
    .filter((item) => item.score > -1000)
    .sort((a, b) => b.score - a.score);
  if (!ranked.length) throw new Error(makerWorldOnly ? "MakerWorld loaded, but I could not find a visible model link to follow." : "The search page loaded, but I could not find a normal result link to follow.");
  const chosen = ranked[0].element;
  const label = normalise(chosen.innerText || chosen.getAttribute("aria-label") || "result");
  const href = chosen.href;
  chosen.scrollIntoView({ block: "center", behavior: "smooth" });
  chosen.click();
  return { ok: true, message: `Followed “${label.slice(0, 120) || "result"}”.`, data: { title: document.title, url: href, linkText: label.slice(0, 300) } };
}

function activeEditable() {
  const active = document.activeElement;
  if (active && (active.matches('input:not([type="password"]):not([type="hidden"]), textarea') || active.isContentEditable)) return active;
  return Array.from(document.querySelectorAll('textarea, input:not([type="password"]):not([type="hidden"]), [contenteditable="true"]')).find(visible) || null;
}

function setEditableText(element, text) {
  if (element.isContentEditable) {
    element.focus();
    document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, text);
  } else {
    element.focus();
    element.value = text;
  }
  element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

function pressKey(key) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  (document.activeElement || document.body).dispatchEvent(event);
  if (key === "pageup") window.scrollBy({ top: -Math.max(window.innerHeight * 0.8, 420), behavior: "smooth" });
  if (key === "pagedown" || key === "space") window.scrollBy({ top: Math.max(window.innerHeight * 0.8, 420), behavior: "smooth" });
  if (key === "home") window.scrollTo({ top: 0, behavior: "smooth" });
  if (key === "end") window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "jarvis-command" || !message.command) return false;
  const command = message.command;
  try {
    if (command.operation === "click_best_model") {
      sendResponse(clickBestLink(String(command.query || ""), true));
      return false;
    }
    if (command.operation === "click_best_search_result") {
      sendResponse(clickBestLink(String(command.query || ""), false));
      return false;
    }
    if (command.operation === "read_page") {
      sendResponse({ ok: true, message: "Read the current browser page.", data: pageSnapshot() });
      return false;
    }
    if (command.operation === "click_text") {
      const candidate = findClickable(command.text);
      if (!candidate) throw new Error(`I could not find a visible button or link named “${command.text}”.`);
      candidate.element.scrollIntoView({ block: "center", behavior: "smooth" });
      candidate.element.click();
      sendResponse({ ok: true, message: `Clicked “${normalise(candidate.element.innerText || candidate.element.value || candidate.element.getAttribute("aria-label"))}”.`, data: { title: document.title, url: location.href } });
      return false;
    }
    if (command.operation === "type_text") {
      const field = activeEditable();
      if (!field) throw new Error("Click a normal text field in the page first.");
      setEditableText(field, String(command.text || ""));
      sendResponse({ ok: true, message: "Typed into the page field.", data: { title: document.title, url: location.href } });
      return false;
    }
    if (command.operation === "scroll") {
      const amount = Math.max(1, Math.min(30, Number(command.amount) || 3));
      const top = (command.direction === "up" ? -1 : 1) * amount * 130;
      window.scrollBy({ top, behavior: "smooth" });
      sendResponse({ ok: true, message: `Scrolled ${command.direction}.`, data: { title: document.title, url: location.href } });
      return false;
    }
    if (command.operation === "press") {
      pressKey(String(command.key || ""));
      sendResponse({ ok: true, message: `Pressed ${command.key}.`, data: { title: document.title, url: location.href } });
      return false;
    }
    throw new Error("That browser command is not available.");
  } catch (error) {
    sendResponse({ ok: false, message: error.message || "Browser command failed." });
    return false;
  }
});

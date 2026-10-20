// Service worker: listens to the bot's alert feed, opens the right page,
// and tells the content script what to click. It never places an order.

const ALLOWED_HOSTS = [
  "www.gamestheshop.com", "www.amazon.in", "www.flipkart.com", "blinkit.com",
  "www.gyftr.com", "store.jiogames.com", "shop.rooter.gg", "offers.myhubble.money",
  "digistore.co.in", "www.woohoo.in",
];
const AUTO_STORES = ["gamestheshop", "amazon"];   // stores where it clicks through to checkout

const DEFAULTS = {
  topic: "", server: "https://ntfy.sh", enabled: true, autoClick: true, sound: true,
  stores: { gamestheshop: true, amazon: true, others: true },
  maxPrice: 0,               // 0 = no limit
};

async function settings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...s, stores: { ...DEFAULTS.stores, ...(s.stores || {}) } };
}

// one write at a time: several tabs finishing together must not overwrite each other's lines
let logChain = Promise.resolve();
function log(msg) {
  const line = `${new Date().toLocaleTimeString()}  ${msg}`;
  logChain = logChain.then(async () => {
    const { logLines = [] } = await chrome.storage.local.get("logLines");
    logLines.unshift(line);
    await chrome.storage.local.set({ logLines: logLines.slice(0, 30) });
  }).catch(() => {});
  return logChain;
}

// ---- live connection to the bot's private ntfy topic (WebSocket) ----
// Chrome (116+) keeps an extension service worker alive while a WebSocket is active; a 30 s
// alarm also wakes it, so the connection is re-opened if Chrome ever stopped the worker.
let ws = null, wsKey = "", lastMsg = 0;

let chain = Promise.resolve();
function ensureListener() {                 // one at a time, so an older call can't undo a newer one
  chain = chain.then(connect, connect);
  return chain;
}

async function connect() {
  const s = await settings();
  const key = s.enabled && s.topic ? `${s.server}|${s.topic}` : "";
  if (!key) { if (ws) ws.close(); ws = null; wsKey = ""; await setStatus("off"); return; }
  if (ws && wsKey === key && ws.readyState <= 1) return;           // CONNECTING or OPEN
  if (ws) ws.close();
  wsKey = key;
  const base = s.server.replace(/\/+$/, "").replace(/^http/, "ws");
  const since = lastMsg ? Math.max(lastMsg, Math.floor(Date.now() / 1000) - 120) : "2m";  // catch up after a drop
  ws = new WebSocket(`${base}/${encodeURIComponent(s.topic)}/ws?since=${since}`);
  ws.onopen = () => { setStatus("connected"); syncControl(s).catch(() => {}); };
  ws.onclose = () => { setStatus("reconnecting"); setTimeout(ensureListener, 3000); };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.event === "keepalive" || ev.event === "open") return setStatus("connected");
      if (ev.event !== "message") return;
      lastMsg = Math.max(lastMsg, ev.time || 0);
      const data = JSON.parse(ev.message);
      if (data && data.v === 1 && data.type === "control") return applyControl(data, true);
      if (data && data.v === 1 && Date.now() / 1000 - (data.ts || 0) < 180) handleAlert(data);
    } catch { /* not one of ours */ }
  };
}

async function setStatus(status) {
  await chrome.storage.local.set({ conn: status, connAt: Date.now() });
}

chrome.runtime.onStartup.addListener(ensureListener);
chrome.runtime.onInstalled.addListener((d) => {
  chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
  ensureListener();
  if (d.reason === "install") chrome.runtime.openOptionsPage();     // straight to the setup screen
});
chrome.alarms.onAlarm.addListener(ensureListener);
chrome.storage.onChanged.addListener((c) => { if (c.topic || c.enabled || c.server) ensureListener(); });
ensureListener();

// ---- handle an alert ----
const seen = new Set();

function safeUrl(u) {
  try { const x = new URL(u); return x.protocol === "https:" && ALLOWED_HOSTS.includes(x.hostname) ? x.href : null; }
  catch { return null; }
}

async function handleAlert(data) {
  const s = await settings();
  if (!s.enabled) return;
  const { remotePaused } = await chrome.storage.local.get({ remotePaused: false });
  if (remotePaused && !data.test) { await log(`Paused from Telegram – ignored ${data.store_name} alert`); return; }
  const key = data.store + ":" + (data.items || []).map((i) => i.id).join(",") + ":" + Math.floor((data.ts || 0) / 60);
  if (seen.has(key)) return;
  seen.add(key);

  const auto = AUTO_STORES.includes(data.store);
  const storeOn = auto ? s.stores[data.store] : s.stores.others;
  if (!storeOn && !data.test) { await log(`Skipped ${data.store_name} (turned off)`); return; }

  let items = (data.items || []).filter((i) => safeUrl(i.url));
  if (s.maxPrice > 0) items = items.filter((i) => !i.price || i.price <= s.maxPrice);
  if (!items.length) { await log(`Skipped ${data.store_name}: no allowed link / over price limit`); return; }

  // Open EVERY card in the alert (1k, 2k, 3k, 4k …), one tab each, lowest value first.
  // GamesTheShop: every tab clicks through to checkout. Amazon: only the highest-value tab
  // does (each Amazon "Pay with UPI" creates its own order); the others just open.
  const win = await chrome.windows.getLastFocused().catch(() => null);
  const last = items.length - 1;
  const opened = [];
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const clickThrough = s.autoClick && auto && (data.store !== "amazon" || i === last);
    // Always the product page: Amazon's cart-add link (cart_url) often lands on an empty cart.
    const tab = await chrome.tabs.create({ url: safeUrl(it.url), active: i === last,
                                           ...(win ? { windowId: win.id } : {}) });
    if (clickThrough) await setJob(tab.id, { store: data.store, step: "start", started: Date.now(),
                                             title: it.title, storeName: data.store_name, test: !!data.test });
    opened.push(`${it.title}${clickThrough ? " (auto-click)" : ""}`);
  }
  if (win) chrome.windows.update(win.id, { focused: true, drawAttention: true, state: "normal" }).catch(() => {});
  const anyAuto = s.autoClick && auto;
  chrome.notifications.create({
    type: "basic", iconUrl: "icon.png", priority: 2, requireInteraction: true, silent: !s.sound,
    title: `🟢 ${data.test ? "TEST · " : ""}${data.store_name}: ${items.length > 1 ? items.length + " cards" : items[0].title}`,
    message: (items.length > 1 ? items.map((i) => i.title).join(", ") + "\n" : "") +
             (anyAuto ? "Opening checkout… pay in each tab." : "Opened the page – buy quickly!"),
  });
  await log(`${data.test ? "TEST " : ""}${data.store_name}: opened ${opened.join(" | ")}`);
}

// One storage key per tab, so several tabs can update their own job at the same time
// without overwriting each other.
const jobKey = (tabId) => "job_" + tabId;
const setJob = (tabId, j) => chrome.storage.session.set({ [jobKey(tabId)]: j });
const getJobFor = async (tabId) => (await chrome.storage.session.get(jobKey(tabId)))[jobKey(tabId)] || null;

// ---- messages from content scripts (what to click) and the options page (test) ----
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "job?" && sender.tab) {
    getJobFor(sender.tab.id).then((j) => {
      reply(j && Date.now() - j.started < 5 * 60 * 1000 ? j : null);   // only tabs we opened, for 5 min
    });
    return true;
  }
  if (msg.type === "job-step" && sender.tab) {
    (async () => {
      const id = sender.tab.id, j = await getJobFor(id);
      if (msg.done) await chrome.storage.session.remove(jobKey(id));
      else if (j) await setJob(id, { ...j, step: msg.step });
      reply(true);                         // the content script waits for this before clicking
      if (msg.note) {
        await log(msg.note);
        if (msg.done) chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 2,
          title: "PSN Stock Bot", message: msg.note });
      }
    })();
    return true;
  }
  if (msg.type === "qr" && sender.tab) { sendQr(msg, sender.tab).then((ok) => reply(ok)); return true; }
  if (msg.type === "watch?" && sender.tab) {
    chrome.storage.local.get(qrKey(sender.tab.id)).then((o) => {
      const q = o[qrKey(sender.tab.id)];
      reply(q && q.expires > Date.now() ? { expires: q.expires } : null);
    });
    return true;
  }
  if (msg.type === "qr-result" && sender.tab) { qrResult(sender.tab, msg).then((ok) => reply(ok)); return true; }
  if (msg.type === "connected-report") {
    (async () => {
      const s = await settings();
      await syncControl(s).catch(() => {});
      const { remotePaused } = await chrome.storage.local.get({ remotePaused: false });
      reply(await reportToOwner(remotePaused ? "✅ Extension connected – but PAUSED (send /extension on to start)"
                                             : "✅ Extension connected and ON"));
    })();
    return true;
  }
  if (msg.type === "settings-changed") { ensureListener(); return; }
  if (msg.type === "test") {
    const urls = (msg.urls || [msg.url]).filter(Boolean);
    handleAlert({ test: true, ts: Date.now() / 1000, store: msg.store, store_name: "Test",
      items: urls.map((u, i) => ({ id: "TEST" + i, title: urls.length > 1 ? `Test card ${i + 1}` : "Test alert", url: u })) });
  }
});

// ---- send the payment QR to Telegram (optional: bot token + chat ID in Options) ----
async function dataUrlToBlob(u) { return (await fetch(u)).blob(); }

async function screenshotCrop(tab, rect) {
  // fallback when the QR image can't be read directly: bring the tab to front and screenshot it
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  await new Promise((r) => setTimeout(r, 500));
  const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  const blob = await dataUrlToBlob(shot);
  if (!rect) return blob;
  const bmp = await createImageBitmap(blob);
  const pad = 16 * rect.dpr;
  const x = Math.max(0, rect.x * rect.dpr - pad), y = Math.max(0, rect.y * rect.dpr - pad);
  const w = Math.min(bmp.width - x, rect.w * rect.dpr + 2 * pad), h = Math.min(bmp.height - y, rect.h * rect.dpr + 2 * pad);
  if (w < 50 || h < 50) return blob;
  const c = new OffscreenCanvas(w, h);
  c.getContext("2d").drawImage(bmp, x, y, w, h, 0, 0, w, h);
  return c.convertToBlob({ type: "image/png" });
}

let qrChain = Promise.resolve();          // one at a time (screenshots need the tab in front)
function sendQr(msg, tab) {
  const run = qrChain.then(() => sendQrNow(msg, tab)).catch(async (e) => { await log(`QR → Telegram failed: ${e.message || e}`); return false; });
  qrChain = run;
  return run;
}

async function sendQrNow(msg, tab) {
  const s = await chrome.storage.local.get({ tgQr: true, tgToken: "", tgChatId: "", browserName: "",
                                             gtsMobile: "", gtsAccount: null, qrTimeoutSec: 120 });
  if (!s.tgQr || !s.tgToken || !s.tgChatId) return false;
  const timeoutMs = Math.max(30, Number(s.qrTimeoutSec) || 120) * 1000;
  const job = (await getJobFor(tab.id)) || {};
  let blob = null, how = "image";
  if (msg.dataUrl) blob = await dataUrlToBlob(msg.dataUrl).catch(() => null);
  if (!blob && msg.src) blob = await fetch(msg.src).then((r) => (r.ok ? r.blob() : null)).catch(() => null);
  if (!blob || blob.size < 200) {
    if (!(await chrome.permissions.contains({ origins: ["<all_urls>"] })))
      throw new Error("couldn't read the QR image – in Options press 'Allow screenshot fallback'");
    how = "screenshot"; blob = await screenshotCrop(tab, msg.rect);
  }
  const acc = s.gtsAccount || {};
  const mobile = s.gtsMobile || acc.mobile || "";
  const who = [acc.name, acc.email].filter(Boolean).join(" · ");
  const lines = [
    `${job.test ? "TEST · " : ""}💳 ${job.storeName || "GamesTheShop"}: ${job.title || "PSN card"}${msg.amount ? ` – ₹${msg.amount}` : ""}`,
    `👤 Account: ${s.browserName || "(name this browser in Options)"}`,
    `📱 Mobile: ${mobile ? "+91 " + mobile : "not found – open gamestheshop.com once while signed in, or type it in Options"}`,
    ...(who ? [`🪪 ${who}`] : []),
    ...(msg.order ? [`🧾 Order: ${msg.order}`] : []),
    `🕒 ${new Date().toLocaleTimeString()}`,
    "",
    `⏳ PAY WITHIN ${fmtLeft(timeoutMs)} – this QR is deleted at ${new Date(Date.now() + timeoutMs).toLocaleTimeString()} if not paid.`,
    "Scan with any UPI app and enter your PIN.",
    "Same phone: save the image → UPI app → Scan QR → gallery.",
  ];
  const caption = lines.join("\n").slice(0, 1024);      // Telegram caption limit
  const fd = new FormData();
  fd.append("chat_id", s.tgChatId);
  fd.append("caption", caption);
  fd.append("photo", blob, "qr.png");
  const r = await fetch(`https://api.telegram.org/bot${s.tgToken}/sendPhoto`, { method: "POST", body: fd });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.description || `HTTP ${r.status}`);
  await startQrTimer(tab.id, {
    msgId: j.result.message_id, chatId: s.tgChatId, caption, expires: Date.now() + timeoutMs,
    title: job.title || "PSN card", storeName: job.storeName || "GamesTheShop", amount: msg.amount || "",
    order: msg.order || "", account: s.browserName || "", mobile, test: !!job.test,
  });
  await log(`QR sent to Telegram (${how}): ${job.title || ""} · ${s.browserName || "this browser"}${mobile ? " · +91 " + mobile : ""}`);
  return true;
}

// ---- QR timer + payment result (owner only: goes to the chat ID set in Options) ----
// qr_<tabId> = { msgId, chatId, caption, expires, title, … }  (storage.local: survives a worker restart)
const qrKey = (tabId) => "qr_" + tabId;
const fmtLeft = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

async function tg(method, params, file) {
  const { tgToken } = await chrome.storage.local.get({ tgToken: "" });
  let body;
  if (file) { body = new FormData(); for (const [k, v] of Object.entries(params)) body.append(k, v); body.append(file.field, file.blob, file.name); }
  else body = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
  const r = await fetch(`https://api.telegram.org/bot${tgToken}/${method}`, { method: "POST", body });
  return r.json().catch(() => ({ ok: false }));
}

async function startQrTimer(tabId, q) {
  await chrome.storage.local.set({ [qrKey(tabId)]: q });
  chrome.alarms.create(qrKey(tabId), { when: q.expires });
}

async function allQrs() {
  const all = await chrome.storage.local.get(null);
  return Object.entries(all).filter(([k]) => /^qr_\d+$/.test(k)).map(([k, v]) => [Number(k.slice(3)), v]);
}

function resultLines(q) {
  return [`${q.storeName}: ${q.title}${q.amount ? ` – ₹${q.amount}` : ""}`,
          `👤 Account: ${q.account || "this browser"}`, ...(q.mobile ? [`📱 Mobile: +91 ${q.mobile}`] : []),
          ...(q.order ? [`🧾 Order: ${q.order}`] : []), `🕒 ${new Date().toLocaleTimeString()}`];
}

// Time is up and nothing was paid: delete the QR from Telegram.
async function expireQr(tabId) {
  const q = (await chrome.storage.local.get(qrKey(tabId)))[qrKey(tabId)];
  if (!q) return;
  await chrome.storage.local.remove(qrKey(tabId));
  await tg("deleteMessage", { chat_id: q.chatId, message_id: q.msgId });
  await tg("sendMessage", { chat_id: q.chatId, disable_notification: true,
    text: [`⌛ ${q.test ? "TEST · " : ""}QR expired – not paid in time, deleted.`, ...resultLines(q)].join("\n") });
  await log(`QR expired (deleted from Telegram): ${q.title} · ${q.account || "this browser"}`);
}

// Live countdown in the caption (runs on the 30 s keep-alive alarm).
async function tickQrs() {
  for (const [tabId, q] of await allQrs()) {
    const left = q.expires - Date.now();
    if (left <= 0) { await expireQr(tabId); continue; }
    const cap = q.caption.replace(/^⏳ .*$/m, `⏳ ${fmtLeft(left)} LEFT – deleted at ${new Date(q.expires).toLocaleTimeString()} if not paid.`);
    await tg("editMessageCaption", { chat_id: q.chatId, message_id: q.msgId, caption: cap });
  }
}

chrome.alarms.onAlarm.addListener((a) => {
  if (/^qr_\d+$/.test(a.name)) expireQr(Number(a.name.slice(3)));
  else if (a.name === "keepalive") tickQrs();
});

// The payment tab reported the result (content script watches the page text).
async function qrResult(tab, msg) {
  const q = (await chrome.storage.local.get(qrKey(tab.id)))[qrKey(tab.id)];
  if (!q) return false;
  await chrome.storage.local.remove(qrKey(tab.id));
  chrome.alarms.clear(qrKey(tab.id));
  await tg("deleteMessage", { chat_id: q.chatId, message_id: q.msgId });   // QR no longer needed
  const ok = msg.result === "success";
  const text = [`${ok ? "✅" : "❌"} ${q.test ? "TEST · " : ""}${ok ? "PAYMENT SUCCESSFUL" : "Payment failed / cancelled"}`,
                ...resultLines(q), ...(msg.order && !q.order ? [`🧾 Order: ${msg.order}`] : []),
                ...(msg.line ? [`“${msg.line.slice(0, 120)}”`] : [])].join("\n");
  let shot = null;
  if (await chrome.permissions.contains({ origins: ["<all_urls>"] })) {
    await new Promise((r) => setTimeout(r, 1200));             // let the success page finish drawing
    shot = await screenshotCrop(tab, null).catch(() => null);
  }
  if (shot) await tg("sendPhoto", { chat_id: q.chatId, caption: text.slice(0, 1024) }, { field: "photo", blob: shot, name: "result.png" });
  else await tg("sendMessage", { chat_id: q.chatId, text: text + (ok ? "\n(no screenshot: press 'Allow screenshot fallback' in Options)" : "") });
  await log(`${ok ? "Payment successful" : "Payment failed"}: ${q.title} · ${q.account || "this browser"}`);
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 2,
    title: ok ? "✅ Payment successful" : "❌ Payment failed", message: `${q.title} · ${q.account || "this browser"}` });
  return true;
}


// ---- pause / resume from Telegram (/extension off|on or the button) ----
// The latest control message is read from the feed on every (re)connect, so a browser that was
// offline still picks up the current state.
async function syncControl(s) {
  const base = s.server.replace(/\/+$/, "");
  const r = await fetch(`${base}/${encodeURIComponent(s.topic)}/json?poll=1&since=24h&tags=psnctl`);
  const lines = (await r.text()).trim().split("\n").filter(Boolean);
  for (const l of lines.reverse()) {
    try { const d = JSON.parse(JSON.parse(l).message); if (d.type === "control") return applyControl(d, false); } catch { /* skip */ }
  }
}

async function applyControl(d, live) {
  const { remotePaused = false, setupDone = false } = await chrome.storage.local.get(["remotePaused", "setupDone"]);
  const paused = !!d.paused;
  if (paused === remotePaused) return;
  await chrome.storage.local.set({ remotePaused: paused });
  await log(paused ? "⏸ Paused from Telegram – Telegram alerts only" : "▶ Turned on from Telegram");
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 1,
    title: "PSN Stock Bot", message: paused ? "Paused from Telegram – this browser won't open or click anything." : "Turned on from Telegram – ready for restocks." });
  if (live && setupDone) await reportToOwner(paused ? "⏸ Extension paused" : "▶ Extension ON and connected");
}

// short status message to the owner's chat, describing this browser
async function reportToOwner(head) {
  const s = await chrome.storage.local.get({ tgToken: "", tgChatId: "", browserType: "", profileName: "", accountName: "",
                                             gtsMobile: "", gtsAccount: null, browserName: "" });
  if (!s.tgToken || !s.tgChatId) return false;
  const mobile = s.gtsMobile || (s.gtsAccount || {}).mobile || "";
  const text = [head, `🌐 Browser: ${s.browserType || "?"}`, `🗂 Profile: ${s.profileName || "?"}`,
                `👤 Account: ${s.accountName || (s.gtsAccount || {}).name || "?"}`,
                `📱 Mobile: ${mobile ? "+91 " + mobile : "?"}`,
                `v${chrome.runtime.getManifest().version} · ${new Date().toLocaleTimeString()}`].join("\n");
  const j = await tg("sendMessage", { chat_id: s.tgChatId, text }).catch(() => ({ ok: false }));
  return !!j.ok;
}

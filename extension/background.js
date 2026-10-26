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
let ws = null, wsKey = "", lastMsg = 0, wsOpenedAt = Date.now();

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
  ws.onopen = () => { wsOpenedAt = Date.now(); setStatus("connected");
                      syncControl(s).catch(() => {}).then(() => heartbeat(true)).catch(() => {}); };
  ws.onclose = () => { setStatus("reconnecting"); setTimeout(ensureListener, 3000); };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.event === "keepalive" || ev.event === "open") return setStatus("connected");
      if (ev.event !== "message") return;
      lastMsg = Math.max(lastMsg, ev.time || 0);
      const data = JSON.parse(ev.message);
      // control: only fresh ones live; older ones from the 2-min catch-up are covered by syncControl
      if (data && data.v === 1 && data.type === "control")
        return (data.ts || 0) * 1000 >= wsOpenedAt - 1000 ? applyControl(data, true) : undefined;
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
  let win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null);
  if (!win) win = await chrome.windows.create({ focused: true, state: "normal" }).catch(() => null);   // no window open
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
  if (msg.type === "claimed?" && sender.tab) {     // has any frame in this tab claimed it?
    const k = `claim_${sender.tab.id}_${msg.what || "x"}`;
    chrome.storage.session.get(k).then((o) => reply(!!(o[k] && Date.now() - o[k] < 120000)));
    return true;
  }
  if (msg.type === "claim" && sender.tab) {        // first frame to ask wins, for 2 minutes
    const k = `claim_${sender.tab.id}_${msg.what || "x"}`;
    chrome.storage.session.get(k).then(async (o) => {
      if (o[k] && Date.now() - o[k] < 120000) return reply(false);
      await chrome.storage.session.set({ [k]: Date.now() });
      reply(true);
    });
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
  const s = await chrome.storage.local.get({ tgQr: true, qrMode: "dm", tgToken: "", tgChatId: "", browserName: "",
    browserType: "", profileName: "", accountName: "", gtsMobile: "", amzMobile: "", gtsAccount: null, amzAccount: null,
    qrTimeoutSec: 120 });
  if (!s.tgQr || !s.tgToken || !s.tgChatId) return false;
  const card = !!msg.card;
  const notify = s.qrMode === "notify" || card;           // card: never a QR photo
  const timeoutMs = Math.max(30, Number(s.qrTimeoutSec) || 120) * 1000;
  const job = (await getJobFor(tab.id)) || {};
  const amazon = job.store === "amazon" || /amazon\.in/.test(tab.url || "");
  const storeName = job.storeName && job.storeName !== "Test" ? job.storeName : amazon ? "Amazon" : "GamesTheShop";

  let blob = null, how = "notify";
  if (!notify) {
    how = "image";
    if (msg.dataUrl) blob = await dataUrlToBlob(msg.dataUrl).catch(() => null);
    if (!blob && msg.src) blob = await fetch(msg.src).then((r) => (r.ok ? r.blob() : null)).catch(() => null);
    if (!blob || blob.size < 200) {
      if (!(await chrome.permissions.contains({ origins: ["<all_urls>"] })))
        throw new Error("couldn't read the QR image – in Options press 'Allow screenshots'");
      how = "screenshot"; blob = await screenshotCrop(tab, msg.rect);
    }
  }
  // which account: GamesTheShop / Amazon details + this browser's profile
  const acc = amazon ? (s.amzAccount || {}) : (s.gtsAccount || {});
  const mobile = amazon ? s.amzMobile : (s.gtsMobile || acc.mobile || "");
  const accName = acc.name || s.accountName || "";
  const amount = msg.amount || (String(job.title || "").match(/[\d,]{3,}/) || [""])[0];
  const deadline = new Date(Date.now() + timeoutMs).toLocaleTimeString();
  const head = card
    ? (msg.cardReady === "otp"
       ? `🔐 ${job.test ? "TEST · " : ""}Card payment of ₹${amount || "?"} started – ENTER THE OTP in the browser`
       : `💳 ${job.test ? "TEST · " : ""}Debit card payment for ₹${amount || "?"} ready – go to the browser`)
    : notify
    ? `🔔 ${job.test ? "TEST · " : ""}QR for ₹${amount || "?"} generated – go to the browser and pay`
    : `💳 ${job.test ? "TEST · " : ""}${storeName}: ${job.title || "PSN card"}${amount && !String(job.title || "").includes(amount) ? ` – ₹${amount}` : ""}`;
  const lines = [
    head,
    ...(notify ? [`🛒 ${storeName}: ${job.title || "PSN card"}`] : []),
    `🌐 Browser: ${s.browserType || "?"}  ·  🗂 Profile: ${s.profileName || "?"}`,
    `👤 ${storeName} account: ${accName || "?"}`,
    `📱 Mobile: ${mobile ? "+91 " + mobile : amazon ? "– (add it in Options)" : "not found – set it in Options"}`,
    ...(msg.order ? [`🧾 Order: ${msg.order}`] : []),
    `🕒 ${new Date().toLocaleTimeString()}`,
    "",
    `⏳ PAY WITHIN ${fmtLeft(timeoutMs)} – ${notify ? "the payment page on that browser expires" : "this QR is deleted"} at ${deadline}.`,
    card ? ({ otp: "Card filled and Pay pressed – enter the bank OTP in that browser to finish. Nothing is charged until you do.",
               cvv: "Card details filled – type the CVV, press Pay, then enter the bank OTP.",
               pay: "Card details filled (CVV too) – press Pay, then enter the bank OTP.",
               form: "Enter your debit card details, press Pay, then the bank OTP." }[msg.cardReady]
             || "Finish the card payment in the browser.")
    : notify ? "Open that browser – the payment tab is in front with the QR highlighted." : "Scan with any UPI app and enter your PIN.",
    ...(notify ? [] : ["Same phone: save the image → UPI app → Scan QR → gallery."]),
  ];
  const text = lines.join("\n").slice(0, 1024);
  let j;
  if (notify) {
    j = await tg("sendMessage", { chat_id: s.tgChatId, text });
    // bring the payment tab to the front on the laptop
    chrome.tabs.update(tab.id, { active: true }).catch(() => {});
    chrome.windows.update(tab.windowId, { focused: true, drawAttention: true }).catch(() => {});
  } else {
    j = await tg("sendPhoto", { chat_id: s.tgChatId, caption: text }, { field: "photo", blob, name: "qr.png" });
  }
  if (!j.ok) throw new Error(j.description || "Telegram error");
  await startQrTimer(tab.id, {
    msgId: j.result.message_id, chatId: s.tgChatId, caption: text, kind: notify ? "text" : "photo",
    expires: Date.now() + timeoutMs, title: job.title || "PSN card", storeName, amount,
    order: msg.order || "", account: s.browserName || accName, mobile, test: !!job.test,
  });
  await log(`${notify ? "Telegram notified (pay in browser)" : `QR sent to Telegram (${how})`}: ${storeName} ${job.title || ""} · ${s.browserName || "this browser"}`);
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
  return [`${q.storeName}: ${q.title}${q.amount && !String(q.title).includes(q.amount) ? ` – ₹${q.amount}` : ""}`,
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
    text: [`⌛ ${q.test ? "TEST · " : ""}QR expired – not paid in time${q.kind === "text" ? "" : ", deleted"}.`, ...resultLines(q)].join("\n") });
  await log(`QR expired (deleted from Telegram): ${q.title} · ${q.account || "this browser"}`);
}

// Live countdown in the caption (runs on the 30 s keep-alive alarm).
async function tickQrs() {
  for (const [tabId, q] of await allQrs()) {
    const left = q.expires - Date.now();
    if (left <= 0) { await expireQr(tabId); continue; }
    const cap = q.caption.replace(/^⏳ .*$/m, `⏳ ${fmtLeft(left)} LEFT – ${q.kind === "text" ? "expires" : "deleted"} at ${new Date(q.expires).toLocaleTimeString()} if not paid.`);
    if (q.kind === "text") await tg("editMessageText", { chat_id: q.chatId, message_id: q.msgId, text: cap });
    else await tg("editMessageCaption", { chat_id: q.chatId, message_id: q.msgId, caption: cap });
  }
}

chrome.alarms.onAlarm.addListener((a) => {
  if (/^qr_\d+$/.test(a.name)) expireQr(Number(a.name.slice(3)));
  else if (a.name === "keepalive") { tickQrs(); heartbeat(false).catch(() => {}); }
});

// The payment tab reported the result (content script watches the page text).
async function qrResult(tab, msg) {
  const q = (await chrome.storage.local.get(qrKey(tab.id)))[qrKey(tab.id)];
  if (!q) return false;
  await chrome.storage.local.remove(qrKey(tab.id));
  chrome.alarms.clear(qrKey(tab.id));
  await tg("deleteMessage", { chat_id: q.chatId, message_id: q.msgId });   // QR no longer needed
  const ok = msg.result === "success";
  if (ok) reportPurchase(q, msg).catch(() => {});        // for the bot's weekly report
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
  const id = await deviceId();
  if (d.revoke_all || (Array.isArray(d.revoked) && d.revoked.includes(id))) return revokeMe(!!d.revoke_all);
  const st = await chrome.storage.local.get({ remotePaused: false, setupDone: false, ctlTs: 0, qrMode: "dm", tgQr: true });
  const notes = [];
  const paused = !!d.paused;
  if (paused !== st.remotePaused) {
    await chrome.storage.local.set({ remotePaused: paused });
    notes.push(paused ? "⏸ Extension paused" : "▶ Extension ON and connected");
    await log(paused ? "⏸ Paused from Telegram – Telegram alerts only" : "▶ Turned on from Telegram");
  }
  // QR mode from Telegram: applied once per control message, so a later local change in Options
  // isn't undone every time the browser reconnects
  const MODES = { dm: "📷 Send QR in DM", notify: "🔔 Pay manually", off: "⛔ QR off" };
  if (d.qr_mode && MODES[d.qr_mode] && (d.ts || 0) > st.ctlTs) {
    const cur = !st.tgQr ? "off" : st.qrMode;
    await chrome.storage.local.set({ ctlTs: d.ts || 0, ...(d.qr_mode === "off" ? { tgQr: false } : { tgQr: true, qrMode: d.qr_mode }) });
    if (cur !== d.qr_mode) { notes.push(`💳 QR mode: ${MODES[d.qr_mode]}`); await log(`QR mode set from Telegram: ${MODES[d.qr_mode]}`); }
  }
  if (!notes.length) return;
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 1, title: "PSN Stock Bot",
    message: notes.join("\n") + (paused && notes[0].startsWith("⏸") ? "\nThis browser won't open or click anything." : "") });
  if (live && st.setupDone) await reportToOwner(notes.join("\n"));
  heartbeat(true).catch(() => {});
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

// ---- this browser's identity + heartbeat, so the owner can list / revoke browsers in Telegram ----
async function deviceId() {
  let { deviceId: id } = await chrome.storage.local.get("deviceId");
  if (!id) { id = crypto.randomUUID().replace(/-/g, "").slice(0, 16); await chrome.storage.local.set({ deviceId: id }); }
  return id;
}

let lastHb = 0;
async function heartbeat(force) {
  if (!force && Date.now() - lastHb < 4 * 60 * 1000) return;          // every ~5 min
  const s = await chrome.storage.local.get({ topic: "", server: "https://ntfy.sh", setupDone: false, enabled: true,
    browserType: "", profileName: "", accountName: "", gtsMobile: "", gtsAccount: null, remotePaused: false,
    qrMode: "dm", tgQr: true });
  if (!s.topic || !s.setupDone) return;
  lastHb = Date.now();
  const acc = s.gtsAccount || {};
  const body = { v: 1, type: "hb", id: await deviceId(), ts: Date.now() / 1000, version: chrome.runtime.getManifest().version,
    browser: s.browserType, profile: s.profileName, account: s.accountName || acc.name || "",
    mobile: s.gtsMobile || acc.mobile || "", paused: s.remotePaused || !s.enabled, qr: !s.tgQr ? "off" : s.qrMode };
  await fetch(`${s.server.replace(/\/+$/, "")}/${encodeURIComponent(s.topic)}-hb`,
    { method: "POST", body: JSON.stringify(body), headers: { Tags: "psnhb" } });
}

// Revoked from Telegram: say so, then forget the feed and the bot token, and show the setup screen.
async function revokeMe(all) {
  const { setupDone } = await chrome.storage.local.get({ setupDone: false });
  if (setupDone) await reportToOwner(all ? "🚫 Disconnected (all browsers revoked)" : "🚫 This browser was revoked").catch(() => {});
  // revoked: also wipe the saved card details from this browser
  await chrome.storage.local.set({ cardNumber: "", cardExpiry: "", cardName: "", cardCvv: "",
    topic: "", tgToken: "", tgChatId: "", setupDone: false, remotePaused: false,
    revokedAt: Date.now(), deviceId: crypto.randomUUID().replace(/-/g, "").slice(0, 16) });   // a reconnect is a new browser
  for (const [tabId] of await allQrs()) await chrome.storage.local.remove(qrKey(tabId));
  if (ws) { try { ws.close(); } catch { /* ignore */ } }
  ws = null; wsKey = "";
  await setStatus("off");
  await log("🚫 Revoked from Telegram – disconnected. Paste a new setup code to connect again.");
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 2, title: "PSN Stock Bot",
    message: "This browser was disconnected from Telegram by the owner." });
}

// a successful payment → small event on the heartbeat feed; the bot keeps it for /week
async function reportPurchase(q, msg) {
  const s = await chrome.storage.local.get({ topic: "", server: "https://ntfy.sh", browserType: "", profileName: "", accountName: "" });
  if (!s.topic) return;
  const amount = Number(String(q.amount || "").replace(/[^\d.]/g, "")) || null;
  const body = { v: 1, type: "buy", dev: await deviceId(), ts: Date.now() / 1000, test: !!q.test,
    store: q.storeName, title: q.title, amount, order: msg.order || q.order || "",
    account: s.accountName || q.account || "", profile: s.profileName, browser: s.browserType };
  await fetch(`${s.server.replace(/\/+$/, "")}/${encodeURIComponent(s.topic)}-hb`,
    { method: "POST", body: JSON.stringify(body), headers: { Tags: "psnbuy" } });
}

// If one of our tabs lands on a payment page and the extension can't act there (not allowed on
// that address, or nothing recognised), say so in the log with the address – otherwise it just
// looks like nothing happened.
const watchedPages = new Set();
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url || !/^https:/.test(tab.url)) return;
  let host;
  try { host = new URL(tab.url).hostname; } catch { return; }
  if (/gamestheshop\.com|amazon\.in|ntfy\.sh/.test(host)) return;      // store pages, handled elsewhere
  if (!(await getJobFor(tabId))) return;                               // not a tab we opened
  const key = tabId + host;
  if (watchedPages.has(key)) return;
  watchedPages.add(key);
  setTimeout(async () => {
    const job = await getJobFor(tabId);
    if (!job || ["card", "choosing", "chosen", "paying", "otp", "qr", "upi", "paid"].includes(job.step)) return;
    const reached = await chrome.tabs.sendMessage(tabId, { type: "ping" }).catch(() => null);
    await log(reached
      ? `⚠ Nothing recognised on ${host} – open Options and send the developer this address.`
      : `⚠ The extension isn't allowed to run on ${host} – that page can't be filled. Send the developer this address.`);
  }, 15000);
});

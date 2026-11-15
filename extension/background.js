// Service worker: listens to the bot's alert feed, opens the right page,
// and tells the content script what to click. It never places an order.

const ALLOWED_HOSTS = [
  "www.gamestheshop.com", "www.amazon.in", "www.flipkart.com", "blinkit.com",
  "www.gyftr.com", "store.jiogames.com", "shop.rooter.gg", "offers.myhubble.money",
  "digistore.co.in", "www.woohoo.in",
  // small Shopify / WooCommerce shops (config.yaml): opened only, no auto-click
  "gameprex.in", "www.simplygaming.in", "hitechgamez.in", "gamekart.in", "e2zstore.com",
  "www.nistore.in", "sheenugamecenter.com", "hgworld.in", "www.stanshop.co", "krgkart.com",
  "emartgames.in", "venusgamesandelecs.com",
];
const AUTO_STORES = ["gamestheshop", "amazon", "flipkart"];   // stores where it clicks through to checkout

const DEFAULTS = {
  topic: "", server: "https://ntfy.sh", enabled: true, autoClick: true, sound: true,
  stores: { gamestheshop: true, amazon: true, flipkart: true, others: true },
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
      // one-off commands (🔄 refresh): only fresh ones, never replayed from the catch-up
      if (data && data.v === 1 && data.type === "action")
        return (data.ts || 0) * 1000 >= Date.now() - 60000 ? handleAction(data).catch(() => {}) : undefined;
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

  // This browser's own Amazon check and the bot's alert usually both report the same restock:
  // whichever comes second must not open the card again (within 10 min). Bot alerts only skip
  // cards the local check opened, so a real second restock from the bot still opens.
  if (!data.test) {
    const before = (data.items || []).length;
    data = { ...data, items: await claimNew(data) };
    if (!data.items.length) {
      if (before && !data.local) await log(`${data.store_name}: already opened by this browser's own check`);
      return;
    }
  }

  const auto = AUTO_STORES.includes(data.store);
  const storeOn = auto ? s.stores[data.store] : s.stores.others;
  if (!storeOn && !data.test) { await log(`Skipped ${data.store_name} (turned off)`); return; }

  let items = (data.items || []).filter((i) => safeUrl(i.url));
  const vals = await allowedValues();
  if (vals) {                                        // Options → "Card values": only those (unknown value = keep)
    const skip = items.filter((i) => { const v = i.value || amzValue(i.title); return v && !vals.has(v); });
    if (skip.length) await log(`${data.store_name}: ignored ${skip.map((i) => "₹" + (i.value || amzValue(i.title))).join(", ")} (not in your card values)`);
    items = items.filter((i) => !skip.includes(i));
    if (!items.length) return;
  }
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
    // Amazon / Flipkart: each checkout is its own order, so only the highest-value tab clicks through
    const clickThrough = s.autoClick && auto && (!["amazon", "flipkart"].includes(data.store) || i === last);
    // Always the product page: Amazon's cart-add link (cart_url) often lands on an empty cart.
    const tab = await chrome.tabs.create({ url: safeUrl(it.url), active: i === last,
                                           ...(win ? { windowId: win.id } : {}) });
    // every tab opened for a restock is watched for a finished order (reported to Telegram)
    await chrome.storage.session.set({ [openedKey(tab.id)]: { store: data.store, storeName: data.store_name,
      title: it.title, value: it.value || amzValue(it.title) || null, price: it.price || null, test: !!data.test, ts: Date.now() } });
    if (clickThrough) await setJob(tab.id, { store: data.store, step: "start", started: Date.now(),
                                             title: it.title, storeName: data.store_name, test: !!data.test,
                                             value: it.value || amzValue(it.title) || null });   // Amazon balance limit check
    // Two checkouts loading at once fight for bandwidth on a slow connection (and both can miss
    // the payment QR), so leave a moment between click-through tabs.
    if (clickThrough && i < last) await new Promise((r) => setTimeout(r, 1200));
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
  if (msg.type === "tclick" && sender.tab && sender.frameId === 0) {
    trustedClickAt(sender.tab.id, msg.x, msg.y).then((ok) => reply({ ok })); return true;
  }
  if (msg.type === "watch?" && sender.tab) {
    chrome.storage.local.get(qrKey(sender.tab.id)).then((o) => {
      const q = o[qrKey(sender.tab.id)];
      // while the tab is being refreshed at expiry, keep watching: you may have paid after all
      const live = q && (q.expires > Date.now() || refreshing.has(sender.tab.id));
      reply(live ? { expires: Math.max(q.expires, Date.now() + 30000) } : null);
    });
    return true;
  }
  if (msg.type === "qr-result" && sender.tab) { qrResult(sender.tab, msg).then((ok) => reply(ok)); return true; }
  if (msg.type === "purchase-watch?" && sender.tab) {        // a tab we opened, not reported yet?
    chrome.storage.session.get(openedKey(sender.tab.id)).then((o) => {
      const r = o[openedKey(sender.tab.id)];
      reply(r && !r.reported && Date.now() - r.ts < 60 * 60 * 1000 ? r : null);
    });
    return true;
  }
  if (msg.type === "purchase" && sender.tab) { purchaseDone(sender.tab, msg).then((ok) => reply(ok)); return true; }
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
  if (msg.type === "settings-changed") { ensureListener(); amzWatchKick(); gtsWatchKick(); return; }
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
  const gts = !amazon && (job.store === "gamestheshop" || /gamestheshop\.com/.test(tab.url || ""));
  // GamesTheShop's signed-in account only on GamesTheShop (not on a Flipkart QR)
  const acc = amazon ? (s.amzAccount || {}) : gts ? (s.gtsAccount || {}) : {};
  const mobile = amazon ? s.amzMobile : gts ? (s.gtsMobile || acc.mobile || "") : "";
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
    ...(gts || amazon || mobile ? [`📱 Mobile: ${mobile ? "+91 " + mobile : amazon ? "– (add it in Options)" : "not found – set it in Options"}`] : []),
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
  // 🔄 after paying on the phone: the bot tells this browser to reload the tab and screenshot it
  const reply_markup = JSON.stringify({ inline_keyboard: [[{ text: "🔄 I paid – refresh & screenshot",
                                                             callback_data: `xr:${await deviceId()}:${tab.id}` }]] });
  let j;
  if (notify) {
    j = await tg("sendMessage", { chat_id: s.tgChatId, text, reply_markup }, null, 3000);
    // bring the payment tab to the front on the laptop
    chrome.tabs.update(tab.id, { active: true }).catch(() => {});
    chrome.windows.update(tab.windowId, { focused: true, drawAttention: true }).catch(() => {});
  } else {
    j = await tg("sendPhoto", { chat_id: s.tgChatId, caption: text, reply_markup }, { field: "photo", blob, name: "qr.png" }, 3000);
  }
  if (!j.ok) throw new Error(j.description || "Telegram error");
  await startQrTimer(tab.id, {
    msgId: j.result.message_id, chatId: s.tgChatId, caption: text, kind: notify ? "text" : "photo",
    expires: Date.now() + timeoutMs, title: job.title || "PSN card", storeName, amount,
    order: msg.order || "", account: s.browserName || accName, mobile, test: !!job.test, markup: reply_markup,
  });
  await log(`${notify ? "Telegram notified (pay in browser)" : `QR sent to Telegram (${how})`}: ${storeName} ${job.title || ""} · ${s.browserName || "this browser"}`);
  return true;
}

// ---- QR timer + payment result (owner only: goes to the chat ID set in Options) ----
// qr_<tabId> = { msgId, chatId, caption, expires, title, … }  (storage.local: survives a worker restart)
const qrKey = (tabId) => "qr_" + tabId;
const fmtLeft = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

// Telegram limits how fast ONE BOT may send, however many browsers you run. With a fleet, a
// restock makes every browser send at once, so honour 429 (retry_after) and retry a few times.
// A short random wait before the first try spreads the burst out.
const TG_TRIES = 5;
async function tg(method, params, file, spread = 0) {
  const { tgToken } = await chrome.storage.local.get({ tgToken: "" });
  const build = () => {
    if (!file) return new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]));
    const fd = new FormData();
    for (const [k, v] of Object.entries(params)) fd.append(k, v);
    fd.append(file.field, file.blob, file.name);
    return fd;
  };
  if (spread) await new Promise((r) => setTimeout(r, Math.random() * spread));
  let last = { ok: false };
  for (let i = 0; i < TG_TRIES; i++) {
    let r;
    try {
      r = await fetch(`https://api.telegram.org/bot${tgToken}/${method}`, { method: "POST", body: build() });
    } catch {                                     // network blip: wait and try again
      await new Promise((res) => setTimeout(res, 1000 * (i + 1)));
      continue;
    }
    const j = await r.json().catch(() => ({ ok: false }));
    if (j.ok) return j;
    last = j;
    const wait = r.status === 429 ? Number((j.parameters || {}).retry_after || 1) + 1
      : r.status >= 500 ? 2 * (i + 1) : 0;        // 4xx other than 429: no point retrying
    if (!wait) return j;
    await new Promise((res) => setTimeout(res, wait * 1000));
  }
  return last;
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

// ---- a real mouse click (Chrome DevTools Protocol) for buttons that ignore script clicks ----
// Only in tabs the extension opened for a restock (job or opened_ record), only on store/Paytm
// pages. Chrome shows "started debugging this browser" for the moment it takes.
async function trustedClickAt(tabId, x, y) {
  const job = await getJobFor(tabId);
  const opened = (await chrome.storage.session.get(openedKey(tabId)))[openedKey(tabId)];
  if (!job && !opened) return false;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  let host = "";
  try { host = new URL(tab.url).hostname; } catch { return false; }
  if (!/(^|\.)(amazon\.in|flipkart\.com|gamestheshop\.com|paytm(payments)?\.(in|com))$/.test(host)) return false;
  if (!chrome.debugger) return false;
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e) {
    if (!/already attached/i.test(e.message || "")) { await log(`Real click not possible: ${e.message || e}`); return false; }
  }
  try {
    const ev = (type, extra = {}) => chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent",
      { type, x, y, button: "left", clickCount: 1, ...extra });
    await ev("mouseMoved", { button: "none", clickCount: 0 });
    await ev("mousePressed");
    await new Promise((r) => setTimeout(r, 40));
    await ev("mouseReleased");
    return true;
  } catch (e) {
    await log(`Real click failed: ${e.message || e}`);
    return false;
  } finally {
    chrome.debugger.detach(target).catch(() => {});
  }
}

// ---- reload a payment tab and screenshot it (🔄 button under the QR, and once at QR expiry) ----
// A payment made on the phone doesn't always make the laptop's Paytm page move on, so the page
// can still show the QR after you paid. A reload asks the shop again; the content script then
// spots "payment successful" on the fresh page and the usual ✅ report (with screenshot) goes
// out. If it doesn't, the screenshot below shows what the page says.
const refreshing = new Set();

function waitLoaded(tabId, ms) {
  return new Promise((resolve) => {
    const done = (ok) => { chrome.tabs.onUpdated.removeListener(on); clearTimeout(t); resolve(ok); };
    const on = (id, info) => { if (id === tabId && info.status === "complete") done(true); };
    const t = setTimeout(() => done(false), ms);
    chrome.tabs.onUpdated.addListener(on);
  });
}

// Returns "reported" (the ✅/❌ report went out after the reload), "shot" (screenshot sent),
// "text" (no screenshot permission), or "gone" (tab closed).
async function refreshTab(tabId, why) {
  if (refreshing.has(tabId)) return "busy";
  refreshing.add(tabId);
  try {
    const s = await chrome.storage.local.get({ tgChatId: "" });
    const before = (await chrome.storage.local.get(qrKey(tabId)))[qrKey(tabId)];
    const opened = (await chrome.storage.session.get(openedKey(tabId)))[openedKey(tabId)];
    const chatId = (before && before.chatId) || s.tgChatId;
    const lines = before ? resultLines(before) : opened ? [`${opened.storeName}: ${opened.title}`] : [];
    let tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) {
      if (why === "button" && chatId)
        await tg("sendMessage", { chat_id: chatId, text: ["⚠️ That payment tab was closed, nothing to refresh.", ...lines].join("\n") });
      return "gone";
    }
    await log(`Refreshing the payment tab (${why === "button" ? "🔄 from Telegram" : "QR expired"}): ${(before || opened || {}).title || tab.title}`);
    await chrome.tabs.reload(tabId);
    await waitLoaded(tabId, 20000);
    await new Promise((r) => setTimeout(r, 3000));     // the page's own "payment successful" check
    const after = (await chrome.storage.local.get(qrKey(tabId)))[qrKey(tabId)];
    if (before && !after) return "reported";            // the fresh page showed the result: ✅/❌ sent
    if (!chatId) return "text";
    tab = await chrome.tabs.get(tabId).catch(() => tab);
    let shot = null;
    if (await chrome.permissions.contains({ origins: ["<all_urls>"] })) shot = await screenshotCrop(tab, null).catch(() => null);
    const text = [why === "button" ? "🔄 Refreshed – this is what the payment page shows now"
                                   : "⌛ QR time is up – refreshed the page once, this is what it shows",
                  ...lines, "No “payment successful” on the page yet. If you paid, it can take a moment: tap 🔄 again."]
                 .join("\n").slice(0, 1024);
    if (shot) { await tg("sendPhoto", { chat_id: chatId, caption: text }, { field: "photo", blob: shot, name: "page.png" }); return "shot"; }
    await tg("sendMessage", { chat_id: chatId, text: text + "\n(no screenshot: press 'Allow screenshots' in Options)" });
    return "text";
  } catch (e) {
    await log(`Refreshing the payment tab failed: ${e.message || e}`);
    return "error";
  } finally {
    refreshing.delete(tabId);
  }
}

// A one-off command from the bot (🔄 button). Only for this browser.
async function handleAction(d) {
  if (d.device !== await deviceId()) return;
  if (d.action === "refresh" && Number.isInteger(d.tab)) await refreshTab(d.tab, "button");
}

// Time is up: reload the tab once (you may have paid and the page just didn't move on), then
// delete the QR from Telegram if it still isn't paid.
const expiring = new Set();
async function expireQr(tabId) {
  if (expiring.has(tabId)) return;                     // alarm + countdown tick can both fire
  const q0 = (await chrome.storage.local.get(qrKey(tabId)))[qrKey(tabId)];
  if (!q0) return;
  expiring.add(tabId);
  try {
    const res = await refreshTab(tabId, "expired");
    if (res === "reported") return;                     // it was paid after all: ✅ already sent
    const q = (await chrome.storage.local.get(qrKey(tabId)))[qrKey(tabId)];
    if (!q) return;
    await chrome.storage.local.remove(qrKey(tabId));
    await tg("deleteMessage", { chat_id: q.chatId, message_id: q.msgId });
    await tg("sendMessage", { chat_id: q.chatId, disable_notification: true,
      text: [`⌛ ${q.test ? "TEST · " : ""}QR expired – not paid in time${q.kind === "text" ? "" : ", deleted"}.`, ...resultLines(q)].join("\n") });
    await log(`QR expired (deleted from Telegram): ${q.title} · ${q.account || "this browser"}`);
  } finally {
    expiring.delete(tabId);
  }
}

// Live countdown in the caption (runs on the 30 s keep-alive alarm).
async function tickQrs() {
  for (const [tabId, q] of await allQrs()) {
    const left = q.expires - Date.now();
    if (left <= 0) { await expireQr(tabId); continue; }
    const cap = q.caption.replace(/^⏳ .*$/m, `⏳ ${fmtLeft(left)} LEFT – ${q.kind === "text" ? "expires" : "deleted"} at ${new Date(q.expires).toLocaleTimeString()} if not paid.`);
    const keep = q.markup ? { reply_markup: q.markup } : {};     // an edit without it drops the 🔄 button
    if (q.kind === "text") await tg("editMessageText", { chat_id: q.chatId, message_id: q.msgId, text: cap, ...keep });
    else await tg("editMessageCaption", { chat_id: q.chatId, message_id: q.msgId, caption: cap, ...keep });
  }
}

chrome.alarms.onAlarm.addListener((a) => {
  if (/^qr_\d+$/.test(a.name)) expireQr(Number(a.name.slice(3)));
  else if (a.name === "keepalive") { tickQrs(); heartbeat(false).catch(() => {}); amzWatchKick(); gtsWatchKick(); }
});

// The payment tab reported the result (content script watches the page text).
async function qrResult(tab, msg) {
  if (msg.result === "success" && !claimReport(tab.id)) return false;      // already reported
  const q = (await chrome.storage.local.get(qrKey(tab.id)))[qrKey(tab.id)];
  if (!q) { if (msg.result === "success") reportedTabs.delete(tab.id); return false; }
  if (msg.result === "success") await markReported(tab.id);
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


// ---- every finished purchase → Telegram (whatever the QR mode, also when you finished by hand) ----
// opened_<tabId> = { store, storeName, title, value, price, test, ts, reported } (session storage)
const openedKey = (tabId) => "opened_" + tabId;
const reportedTabs = new Set();          // checked + set before any await: one report per tab, ever
function claimReport(tabId) {
  if (reportedTabs.has(tabId)) return false;
  reportedTabs.add(tabId);
  return true;
}
async function markReported(tabId) {
  const k = openedKey(tabId), r = (await chrome.storage.session.get(k))[k];
  if (r) await chrome.storage.session.set({ [k]: { ...r, reported: true } });
}

// The content script saw an order-confirmation page in a tab we opened for a restock.
async function purchaseDone(tab, msg) {
  const k = openedKey(tab.id), rec = (await chrome.storage.session.get(k))[k];
  if (!rec || rec.reported) return false;
  // the QR flow is watching this tab: let it report (it also removes the QR from Telegram)
  if ((await chrome.storage.local.get(qrKey(tab.id)))[qrKey(tab.id)])
    return qrResult(tab, { result: "success", line: msg.line, order: msg.order });
  if (!claimReport(tab.id)) return false;
  await markReported(tab.id);
  const s = await chrome.storage.local.get({ tgToken: "", tgChatId: "", browserName: "", browserType: "", profileName: "",
    accountName: "", gtsMobile: "", amzMobile: "", gtsAccount: null, amzAccount: null });
  const amazon = rec.store === "amazon";
  const acc = amazon ? (s.amzAccount || {}) : (s.gtsAccount || {});
  const mobile = amazon ? s.amzMobile : (s.gtsMobile || acc.mobile || "");
  const amount = rec.value || msg.amount || rec.price || "";
  const q = { storeName: rec.storeName || rec.store, title: rec.title || "PSN card", amount: amount ? String(amount) : "",
              order: msg.order || "", account: s.browserName || acc.name || s.accountName || "", mobile, test: rec.test };
  reportPurchase(q, msg).catch(() => {});                   // for the bot's weekly report
  await log(`Purchase complete: ${q.storeName} ${q.title}${q.order ? " · order " + q.order : ""} · ${q.account || "this browser"}`);
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 2, title: "✅ Purchase complete",
    message: `${q.title} · ${q.account || "this browser"}` });
  if (!s.tgToken || !s.tgChatId) { await log("Purchase not sent to Telegram: connect the extension with a setup code from /extension"); return false; }
  const text = [`✅ ${q.test ? "TEST · " : ""}PURCHASE COMPLETE`, ...resultLines(q),
                `🌐 ${s.browserType || "?"} · 🗂 ${s.profileName || "?"}`,
                ...(msg.line ? [`"${msg.line.slice(0, 120)}"`] : [])].join("\n");
  let shot = null;
  if (await chrome.permissions.contains({ origins: ["<all_urls>"] })) {
    await new Promise((r) => setTimeout(r, 1200));             // let the page finish drawing
    shot = await screenshotCrop(tab, null).catch(() => null);
  }
  const j = shot ? await tg("sendPhoto", { chat_id: s.tgChatId, caption: text.slice(0, 1024) }, { field: "photo", blob: shot, name: "order.png" })
                 : await tg("sendMessage", { chat_id: s.tgChatId, text });
  if (!j.ok) await log(`Purchase report to Telegram failed: ${j.description || "error"}`);
  return !!j.ok;
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
  if (!force && Date.now() - lastHb < 14 * 60 * 1000) return;         // every ~15 min (fleet-friendly)
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

// ---- Amazon: check the cards from this browser too (Options → "Check Amazon from this browser") ----
// The bot checks Amazon from a server every 30–120 s and its alert then has to reach this browser.
// This checks the same product pages straight from your own internet connection every few seconds
// and opens Buy Now the moment a card comes back. It loads the pages WITHOUT your Amazon login
// (credentials: "omit"), so the checks have nothing to do with your account. Off by default.
const AMZ_WATCH_DEFAULTS = { amzWatch: false, amzWatchSec: 15,
  amzWatchList: "B093QF35KZ  ₹2000 card\nB07K6RYVJ5  ₹4000 card\nB0C1H3K2V4  PlayStation E-Gift Card\nB07K6PVR8B  PlayStation card" };
let amzTimer = null, amzBusy = false, amzPauseUntil = 0;

// ASINs from the Options list: one per line, a bare 10-character ID or any amazon.in product link
function amzAsins(text) {
  const out = [];
  for (const line of String(text || "").split("\n")) {
    const m = line.match(/\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})(?:[/?#\s]|$)/) || line.trim().match(/^([A-Z0-9]{10})(?:\s|$)/);
    if (m && !out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

const amzValue = (title) => {
  const m = String(title).match(/(?:rs\.?|₹|inr)\s*-?\s*([\d,]{3,6})(?!\d)|(?<!\d)([\d,]{3,6})\s*-?\s*(?:inr|rs)\b/i);
  return m ? Number((m[1] || m[2]).replace(/,/g, "")) || null : null;
};
const unescapeHtml = (s) => s.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&nbsp;/g, " ");

// Same rules as the bot (psnbot/stores/amazon.py). Returns {inStock, title}, "blocked", or null (couldn't tell).
async function amzCheck(asin) {
  let r, html;
  try {
    r = await fetch(`https://www.amazon.in/gp/aw/d/${asin}`, { credentials: "omit", cache: "no-store" });
    html = await r.text();
  } catch { return null; }
  if (r.status === 503 || /validateCaptcha|Type the characters you see/i.test(html)) return "blocked";
  if (!r.ok) return null;
  const title = unescapeHtml(((html.match(/id="productTitle"[^>]*>([\s\S]*?)</) || [])[1] || "").replace(/\s+/g, " ").trim());
  if (!title) return null;                                     // not a normal product page
  const avail = ((html.match(/id="availability"[\s\S]{0,1500}?<\/div>/) || [""])[0]).replace(/<[^>]+>/g, " ").toLowerCase();
  const buy = /id="(?:add-to-cart-button|buy-now-button|gc-buy-box-atc|gc-buy-box-bn)"/.test(html);
  let inStock;
  if (/currently unavailable|out of stock/.test(avail)) inStock = false;
  else if (buy) inStock = true;
  else if (avail.trim() || /id="outOfStock"/.test(html)) inStock = false;
  else return null;
  return { inStock, title };
}

// Options → "Card values" (e.g. "1000, 2000, 3000, 4000, 5000"): a Set, or null = every value
const DEFAULT_VALUES = "1000, 2000, 3000, 4000, 5000";     // keep in step with options.js
async function allowedValues() {
  const { onlyValues } = await chrome.storage.local.get({ onlyValues: DEFAULT_VALUES });
  const set = new Set(String(onlyValues || "").split(/[^\d]+/).filter(Boolean).map(Number).filter((v) => v >= 100));
  return set.size ? set : null;
}

// Cards opened recently: "store:id" -> { ts, by: "local" | "bot" } (session storage, so it
// survives the worker restarting). Check + mark run one alert at a time, so two alerts for
// the same card arriving together can't both open it.
let claimChain = Promise.resolve();
function claimNew(data) {
  const run = claimChain.then(async () => {
    const { opened = {} } = await chrome.storage.session.get("opened");
    const now = Date.now();
    for (const k of Object.keys(opened)) if (now - opened[k].ts > 30 * 60 * 1000) delete opened[k];
    const fresh = (data.items || []).filter((i) => {
      const o = opened[`${data.store}:${i.id}`];
      return !(o && now - o.ts < 10 * 60 * 1000 && (data.local || o.by === "local"));
    });
    for (const i of fresh) opened[`${data.store}:${i.id}`] = { ts: now, by: data.local ? "local" : "bot" };
    await chrome.storage.session.set({ opened });
    return fresh;
  });
  claimChain = run.catch(() => {});
  return run;
}

async function amzStatus(text) {                    // shown in Options; only written when it changes
  const { amzWatchInfo } = await chrome.storage.local.get({ amzWatchInfo: "" });
  if (amzWatchInfo !== text) await chrome.storage.local.set({ amzWatchInfo: text });
}

function amzWatchKick() {
  if (!amzTimer && !amzBusy) amzTimer = setTimeout(amzWatchRun, 500);
}

async function amzWatchRun() {
  amzTimer = null;
  const s = await chrome.storage.local.get({ ...AMZ_WATCH_DEFAULTS, enabled: true, remotePaused: false,
                                             stores: DEFAULTS.stores });
  const asins = amzAsins(s.amzWatchList);
  if (!s.amzWatch || !s.enabled || !asins.length) {
    await amzStatus(s.amzWatch && !asins.length ? "⚠ No Amazon product IDs in the list" : "");
    return;                                         // stopped; the 30 s alarm / a settings change starts it again
  }
  const every = Math.max(5, Number(s.amzWatchSec) || 15) * 1000;
  amzBusy = true;
  try {
    if (s.remotePaused || (s.stores && s.stores.amazon === false)) {
      await amzStatus(s.remotePaused ? "⏸ Paused from Telegram" : "Amazon is switched off above");
    } else if (Date.now() < amzPauseUntil) {
      await amzStatus(`⚠ Amazon showed a captcha – checking again at ${new Date(amzPauseUntil).toLocaleTimeString()}`);
    } else {
      // last seen stock per card, kept for the browser session (survives the worker restarting)
      const { amzLast = {} } = await chrome.storage.session.get("amzLast");
      const back = [];
      let changed = false, ok = 0;
      for (let i = 0; i < asins.length; i++) {
        if (i) await new Promise((r) => setTimeout(r, 400 + Math.random() * 600));
        const res = await amzCheck(asins[i]);
        if (res === "blocked") {
          amzPauseUntil = Date.now() + 5 * 60 * 1000;
          await log("Amazon check: Amazon showed a captcha – pausing for 5 min (raise the seconds in Options if it keeps happening)");
          break;
        }
        if (!res) continue;                         // couldn't tell this round: keep the last state
        ok++;
        const prev = amzLast[asins[i]];
        if (prev !== res.inStock) { amzLast[asins[i]] = res.inStock; changed = true; }
        // only a change from sold out to in stock opens the card (not what's in stock when Chrome starts)
        if (res.inStock && prev === false) back.push({ id: asins[i], title: res.title, value: amzValue(res.title) });
      }
      if (changed) await chrome.storage.session.set({ amzLast });
      if (Date.now() >= amzPauseUntil) {
        const inStock = asins.filter((a) => amzLast[a] === true).length;
        await amzStatus(ok ? `● Checking ${asins.length} card${asins.length > 1 ? "s" : ""} every ${every / 1000} s · ${inStock} in stock`
                           : "⚠ Couldn't read the Amazon pages – check the product IDs");
      }
      if (back.length) {
        back.sort((a, b) => (a.value || 0) - (b.value || 0));      // highest value last: that tab clicks through
        await log(`Amazon check: back in stock – ${back.map((b) => b.title.slice(0, 50)).join(" | ")}`);
        await handleAlert({ v: 1, local: true, ts: Date.now() / 1000, store: "amazon", store_name: "Amazon",
          items: back.map((b) => ({ id: b.id, title: b.title, url: `https://www.amazon.in/dp/${b.id}`, value: b.value, price: null })) });
      }
    }
  } catch (e) {
    await log(`Amazon check failed: ${e.message || e}`);
  } finally {
    amzBusy = false;
    if (!amzTimer) amzTimer = setTimeout(amzWatchRun, every + Math.random() * 2000);
  }
}

chrome.storage.onChanged.addListener((c, area) => {
  if (area === "local" && (c.amzWatch || c.amzWatchSec || c.amzWatchList || c.enabled)) amzWatchKick();
});
amzWatchKick();

// ---- GamesTheShop: check from this browser too (Options → "Check GamesTheShop from this browser") ----
// Same small stock API the bot uses (psnbot/stores/gamestheshop.py): one ~12 KB search request per
// check. The search list can say "In Stock" for minutes after a card sells out, so a card only
// counts as back once its product page's own request (live stock) agrees, like the bot does.
const GTS_API = "https://green-api.gamestheshop.com/storefront";
const GTS_WATCH_DEFAULTS = { gtsWatch: false, gtsWatchSec: 3 };
const GTS_VERIFY_PER_MIN = 30;                    // live-stock requests per minute, on top of the list
const GTS_MAX_SEC = 30;                           // slowest the automatic back-off will go
const GTS_CLEAN_MS = 5 * 60 * 1000;               // this long without a 429 before easing back up
let gtsTimer = null, gtsBusy = false, gtsPauseUntil = 0, gtsFails = 0;
// GamesTheShop rate-limits (HTTP 429). Retrying at the same speed just gets blocked again, so the
// interval is raised on every 429 and eased back down after a long clean run. Kept in session
// storage so restarting the worker doesn't go straight back to hammering the shop.
// gtsFloor = the fastest interval the shop has refused. The interval never eases back below a
// little above that, so it settles on a speed the shop accepts instead of bouncing in and out of 429.
let gtsEvery = 0, gtsRetryAfter = 0, gtsFloor = 0, gtsCleanSince = 0;
const gtsLiveCache = {};                          // variant id -> { ts, live }

// The API only answers requests that come from the shop's own site
chrome.declarativeNetRequest.updateSessionRules({
  removeRuleIds: [1],
  addRules: [{ id: 1, priority: 1,
    action: { type: "modifyHeaders", requestHeaders: [
      { header: "origin", operation: "set", value: "https://www.gamestheshop.com" },
      { header: "referer", operation: "set", value: "https://www.gamestheshop.com/" }] },
    condition: { requestDomains: ["green-api.gamestheshop.com"], initiatorDomains: [chrome.runtime.id],
                 resourceTypes: ["xmlhttprequest", "other"] } }],
}).catch(() => {});

const gtsIsPsn = (name) => /playstation|psn/i.test(name) &&
  !/playstation plus|ps plus|controller|console|memory card|xbox|steam/i.test(name);

// "limited" (429), null (couldn't tell) or the PSN rows: [{ vid, pid, name, price, listed }]
async function gtsList() {
  let r;
  try {
    r = await fetch(`${GTS_API}/products/filter?page=1&limit=50`, { method: "POST", credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json, text/plain, */*" },
      body: JSON.stringify({ searchQuery: "gift card" }) });
  } catch { return null; }
  if (r.status === 429) { gtsRetryAfter = Number(r.headers.get("retry-after")) || 0; return "limited"; }
  if (!r.ok) return null;
  const rows = (await r.json().catch(() => ({}))).data;
  if (!Array.isArray(rows)) return null;
  return rows.filter((p) => gtsIsPsn(p.name || "")).map((p) => {
    const status = String(p.stock_status || "").toLowerCase().replace(/\s/g, ""), avail = (p.inventory || {}).available_stock;
    const out = status.includes("out") || (avail != null && avail <= 0);
    return { vid: p.id, pid: p.product_id, name: String(p.name || "").replace(/\s+/g, " ").trim(),
             price: p.sale_price || null, listed: !out && status === "instock" };
  });
}

// What the product page shows: true / false, "limited", or null (couldn't tell)
async function gtsLive(pid, vid) {
  let r;
  try { r = await fetch(`${GTS_API}/products/${pid}/`, { credentials: "omit", cache: "no-store", headers: { Accept: "application/json" } }); }
  catch { return null; }
  if (r.status === 429) return "limited";
  if (!r.ok) return null;
  const d = (await r.json().catch(() => ({}))).data || {};
  const vs = d.variants || [], v = vs.find((x) => x.id === vid) || vs[0];
  const avail = ((v || {}).inventory || {}).available_stock;
  if (avail != null) return avail > 0;
  return !/out of stock/i.test(d.stock_status || "");
}

async function gtsStatus(text) {
  const { gtsWatchInfo } = await chrome.storage.local.get({ gtsWatchInfo: "" });
  if (gtsWatchInfo !== text) await chrome.storage.local.set({ gtsWatchInfo: text });
}

// HTTP 429: wait (Retry-After if the shop sent one), then check less often from now on.
async function gtsSlowDown(want, every) {
  const wait = Math.min(300, Math.max(30, gtsRetryAfter || 60));
  gtsRetryAfter = 0;
  gtsPauseUntil = Date.now() + wait * 1000;
  gtsCleanSince = 0;
  gtsFloor = Math.max(gtsFloor, every);           // this speed is known to be refused
  const next = Math.min(GTS_MAX_SEC * 1000, Math.max(want, every * 2));
  const changed = next !== gtsEvery;
  gtsEvery = next;
  await chrome.storage.session.set({ gtsEvery, gtsFloor });
  if (changed) {                                  // only when the interval really changes: no log spam
    await log(`GamesTheShop check: too many requests – waiting ${wait}s, then checking every `
              + `${Math.round(next / 1000)} s instead of ${want / 1000} s.`
              + (next >= GTS_MAX_SEC * 1000 ? " This is as slow as it goes; the bot checks this store too, "
                 + "so you can switch this off in Options." : ""));
  }
}

function gtsWatchKick() {
  if (!gtsTimer && !gtsBusy) gtsTimer = setTimeout(gtsWatchRun, 300);
}

async function gtsWatchRun() {
  gtsTimer = null;
  const s = await chrome.storage.local.get({ ...GTS_WATCH_DEFAULTS, enabled: true, remotePaused: false, stores: DEFAULTS.stores });
  if (!s.gtsWatch || !s.enabled) { await gtsStatus(""); return; }      // the 30 s alarm / a settings change starts it again
  const want = Math.max(1, Number(s.gtsWatchSec) || 3) * 1000;
  if (!gtsEvery) {
    const saved = await chrome.storage.session.get({ gtsEvery: 0, gtsFloor: 0 });
    gtsEvery = saved.gtsEvery || want;
    gtsFloor = saved.gtsFloor || 0;
  }
  const every = Math.max(want, gtsEvery);         // your setting, or slower while the shop is pushing back
  const slowed = every > want;
  gtsBusy = true;
  try {
    if (s.remotePaused || (s.stores && s.stores.gamestheshop === false)) {
      await gtsStatus(s.remotePaused ? "⏸ Paused from Telegram" : "GamesTheShop is switched off above");
    } else if (Date.now() < gtsPauseUntil) {
      await gtsStatus(`⚠ GamesTheShop asked to slow down – checking again at ${new Date(gtsPauseUntil).toLocaleTimeString()}`);
    } else {
      let rows = await gtsList();
      if (rows === "limited") {
        await gtsSlowDown(want, every);
      } else if (!rows) {
        if (++gtsFails >= 5) await gtsStatus("⚠ Couldn't read GamesTheShop's stock list");
      } else {
        gtsFails = 0;
        const vals = await allowedValues();          // only the card values you want (no requests for the rest)
        if (vals) rows = rows.filter((r) => { const v = amzValue(r.name); return !v || vals.has(v); });
        const { gtsLast = {} } = await chrome.storage.session.get("gtsLast");
        const now = Date.now();
        // live-check cards the list calls in stock: straight away when the list just changed,
        // otherwise spread out to stay within GTS_VERIFY_PER_MIN
        const listed = rows.filter((r) => r.listed);
        for (const r of rows) if (!r.listed) delete gtsLiveCache[r.vid];
        const gap = Math.max(3000, listed.length * 60000 / GTS_VERIFY_PER_MIN);
        const due = listed.filter((r) => !gtsLiveCache[r.vid] || now - gtsLiveCache[r.vid].ts >= gap);
        const res = await Promise.all(due.map((r) => gtsLive(r.pid, r.vid)));
        due.forEach((r, i) => {
          if (res[i] === "limited") gtsSlowDown(want, every).catch(() => {});
          else if (res[i] !== null) gtsLiveCache[r.vid] = { ts: now, live: res[i] };
        });
        const back = [];
        let changed = false;
        for (const r of rows) {
          const cur = !r.listed ? false : gtsLiveCache[r.vid] ? gtsLiveCache[r.vid].live : undefined;
          if (cur === undefined) continue;            // couldn't tell this round: keep the last state
          const prev = gtsLast[r.vid];
          if (prev !== cur) { gtsLast[r.vid] = cur; changed = true; }
          if (cur && prev === false) back.push(r);    // sold out -> in stock (not what's in stock at start)
        }
        if (changed) await chrome.storage.session.set({ gtsLast });
        const inStock = rows.filter((r) => gtsLast[r.vid] === true).length;
        // a long clean run: ease back up, but never below a little above a speed that was refused
        const target = Math.max(want, Math.round(gtsFloor * 1.25));
        if (!gtsCleanSince) gtsCleanSince = Date.now();
        if (every > target && Date.now() - gtsCleanSince >= GTS_CLEAN_MS) {
          gtsCleanSince = Date.now();
          gtsEvery = Math.max(target, Math.round(every * 0.8));
          await chrome.storage.session.set({ gtsEvery });
          await log(`GamesTheShop check: going faster again – every ${(gtsEvery / 1000).toFixed(1)} s.`);
        }
        await gtsStatus(`● Checking ${rows.length} PlayStation cards every ${(every / 1000).toFixed(every < 10000 ? 1 : 0)} s`
                        + (slowed ? ` (the shop asked me to slow down; you set ${want / 1000} s)` : "")
                        + ` · ${inStock} in stock`);
        if (back.length) {
          const val = (r) => amzValue(r.name) || 0;
          back.sort((a, b) => val(a) - val(b));
          await log(`GamesTheShop check: back in stock – ${back.map((r) => r.name.slice(0, 50)).join(" | ")}`);
          await handleAlert({ v: 1, local: true, ts: Date.now() / 1000, store: "gamestheshop", store_name: "GamesTheShop",
            items: back.map((r) => ({ id: r.vid, title: r.name, value: amzValue(r.name), price: r.price,
              url: `https://www.gamestheshop.com/product/${r.pid}?variant=${r.vid}` })) });
        }
      }
    }
  } catch (e) {
    await log(`GamesTheShop check failed: ${e.message || e}`);
  } finally {
    gtsBusy = false;
    if (!gtsTimer) gtsTimer = setTimeout(gtsWatchRun, every + Math.random() * 200);
  }
}

chrome.storage.onChanged.addListener((c, area) => {
  if (area === "local" && (c.gtsWatch || c.gtsWatchSec || c.enabled)) gtsWatchKick();
});
gtsWatchKick();

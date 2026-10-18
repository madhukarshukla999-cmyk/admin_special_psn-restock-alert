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

async function log(msg) {
  const { logLines = [] } = await chrome.storage.local.get("logLines");
  logLines.unshift(`${new Date().toLocaleTimeString()}  ${msg}`);
  await chrome.storage.local.set({ logLines: logLines.slice(0, 30) });
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
  ws.onopen = () => setStatus("connected");
  ws.onclose = () => { setStatus("reconnecting"); setTimeout(ensureListener, 3000); };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (ev.event === "keepalive" || ev.event === "open") return setStatus("connected");
      if (ev.event !== "message") return;
      lastMsg = Math.max(lastMsg, ev.time || 0);
      const data = JSON.parse(ev.message);
      if (data && data.v === 1 && Date.now() / 1000 - (data.ts || 0) < 180) handleAlert(data);
    } catch { /* not one of ours */ }
  };
}

async function setStatus(status) {
  await chrome.storage.local.set({ conn: status, connAt: Date.now() });
}

chrome.runtime.onStartup.addListener(ensureListener);
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }); ensureListener(); });
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
  const key = data.store + ":" + (data.items || []).map((i) => i.id).join(",") + ":" + Math.floor((data.ts || 0) / 60);
  if (seen.has(key)) return;
  seen.add(key);

  const auto = AUTO_STORES.includes(data.store);
  const storeOn = auto ? s.stores[data.store] : s.stores.others;
  if (!storeOn && !data.test) { await log(`Skipped ${data.store_name} (turned off)`); return; }

  let items = (data.items || []).filter((i) => safeUrl(i.url));
  if (s.maxPrice > 0) items = items.filter((i) => !i.price || i.price <= s.maxPrice);
  if (!items.length) { await log(`Skipped ${data.store_name}: no allowed link / over price limit`); return; }

  const it = items[items.length - 1];        // highest value first is usually what people want
  const clickThrough = s.autoClick && auto;
  const url = (clickThrough && it.cart_url && safeUrl(it.cart_url)) || safeUrl(it.url);

  const win = await chrome.windows.getLastFocused().catch(() => null);
  const tab = await chrome.tabs.create({ url, active: true, ...(win ? { windowId: win.id } : {}) });
  if (win) chrome.windows.update(win.id, { focused: true, drawAttention: true, state: "normal" }).catch(() => {});
  if (clickThrough) {
    const { jobs = {} } = await chrome.storage.session.get("jobs");
    jobs[tab.id] = { store: data.store, step: "start", started: Date.now() };
    await chrome.storage.session.set({ jobs });
  }
  chrome.notifications.create({
    type: "basic", iconUrl: "icon.png", priority: 2, requireInteraction: true, silent: !s.sound,
    title: `🟢 ${data.test ? "TEST · " : ""}${data.store_name}: ${it.title}`,
    message: clickThrough ? "Opening checkout… enter OTP/CVV to pay." : "Opened the page – buy quickly!",
  });
  await log(`${data.test ? "TEST " : ""}${data.store_name}: opened ${it.title}${clickThrough ? " (auto-click)" : ""}`);
}

// ---- messages from content scripts (what to click) and the options page (test) ----
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "job?" && sender.tab) {
    chrome.storage.session.get("jobs").then(({ jobs = {} }) => {
      const j = jobs[sender.tab.id];
      reply(j && Date.now() - j.started < 5 * 60 * 1000 ? j : null);   // only tabs we opened, for 5 min
    });
    return true;
  }
  if (msg.type === "job-step" && sender.tab) {
    chrome.storage.session.get("jobs").then(async ({ jobs = {} }) => {
      if (msg.done) delete jobs[sender.tab.id]; else if (jobs[sender.tab.id]) jobs[sender.tab.id].step = msg.step;
      await chrome.storage.session.set({ jobs });
      if (msg.note) {
        await log(msg.note);
        if (msg.done) chrome.notifications.create({ type: "basic", iconUrl: "icon.png", priority: 2,
          title: "PSN Stock Bot", message: msg.note });
      }
    });
    return;
  }
  if (msg.type === "settings-changed") { ensureListener(); return; }
  if (msg.type === "test") { handleAlert({ test: true, ts: Date.now() / 1000, store: msg.store, store_name: "Test",
    items: [{ id: "TEST", title: "Test alert", url: msg.url, cart_url: msg.cart_url || null }] }); }
});

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
    if (clickThrough) await setJob(tab.id, { store: data.store, step: "start", started: Date.now() });
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
  if (msg.type === "settings-changed") { ensureListener(); return; }
  if (msg.type === "test") {
    const urls = (msg.urls || [msg.url]).filter(Boolean);
    handleAlert({ test: true, ts: Date.now() / 1000, store: msg.store, store_name: "Test",
      items: urls.map((u, i) => ({ id: "TEST" + i, title: urls.length > 1 ? `Test card ${i + 1}` : "Test alert", url: u })) });
  }
});

const $ = (id) => document.getElementById(id);
const DEFAULTS = { topic: "", enabled: true, autoClick: true, sound: true, maxPrice: 0,
                   stores: { gamestheshop: true, amazon: true, others: true } };

async function load() {
  const s = { ...DEFAULTS, ...(await chrome.storage.local.get(null)) };
  s.stores = { ...DEFAULTS.stores, ...(s.stores || {}) };
  $("topic").value = s.topic; $("enabled").checked = s.enabled; $("autoClick").checked = s.autoClick;
  $("sound").checked = s.sound; $("maxPrice").value = s.maxPrice || 0;
  for (const k of ["gamestheshop", "amazon", "others"]) $("st_" + k).checked = s.stores[k];
  const fresh = s.connAt && Date.now() - s.connAt < 5 * 60 * 1000;
  $("status").textContent = !s.topic ? "not set up" : !s.enabled ? "off"
    : s.conn === "connected" && fresh ? "✅ connected" : s.conn ? "⏳ " + s.conn : "⏳ connecting…";
  $("log").textContent = (s.logLines || []).join("\n") || "No alerts yet.";
}

$("save").onclick = async () => {
  await chrome.storage.local.set({
    topic: $("topic").value.trim(), enabled: $("enabled").checked, autoClick: $("autoClick").checked,
    sound: $("sound").checked, maxPrice: Number($("maxPrice").value) || 0,
    stores: { gamestheshop: $("st_gamestheshop").checked, amazon: $("st_amazon").checked, others: $("st_others").checked },
  });
  chrome.runtime.sendMessage({ type: "settings-changed" }).catch(() => {});
  $("save").textContent = "Saved ✓"; setTimeout(() => ($("save").textContent = "Save"), 1500);
  setTimeout(load, 1500);
};

$("test").onclick = () => {
  const url = $("testUrl").value.trim() || "https://www.gamestheshop.com/";
  const store = url.includes("amazon.in") ? "amazon" : url.includes("gamestheshop.com") ? "gamestheshop" : "test";
  const asin = (url.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1];
  chrome.runtime.sendMessage({ type: "test", url, store,
    cart_url: asin ? `https://www.amazon.in/gp/aws/cart/add.html?ASIN.1=${asin}&Quantity.1=1` : null });
};

chrome.storage.onChanged.addListener(load);
load();

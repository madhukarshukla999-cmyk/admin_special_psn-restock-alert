const $ = (id) => document.getElementById(id);
const DEFAULTS = { topic: "", enabled: true, autoClick: true, sound: true, maxPrice: 0, clickPayNow: true, amazonUpiQr: true, upiId: "", tgQr: true, tgToken: "", tgChatId: "",
                   stores: { gamestheshop: true, amazon: true, others: true } };

async function load() {
  const s = { ...DEFAULTS, ...(await chrome.storage.local.get(null)) };
  s.stores = { ...DEFAULTS.stores, ...(s.stores || {}) };
  $("topic").value = s.topic; $("enabled").checked = s.enabled; $("autoClick").checked = s.autoClick;
  $("sound").checked = s.sound; $("maxPrice").value = s.maxPrice || 0;
  $("clickPayNow").checked = s.clickPayNow; $("amazonUpiQr").checked = s.amazonUpiQr; $("tgQr").checked = s.tgQr; $("tgToken").value = s.tgToken || ""; $("tgChatId").value = s.tgChatId || ""; $("upiId").value = s.upiId || "";
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
    clickPayNow: $("clickPayNow").checked, amazonUpiQr: $("amazonUpiQr").checked, tgQr: $("tgQr").checked, tgToken: $("tgToken").value.trim(), tgChatId: $("tgChatId").value.trim(), upiId: $("upiId").value.trim(),
    stores: { gamestheshop: $("st_gamestheshop").checked, amazon: $("st_amazon").checked, others: $("st_others").checked },
  });
  chrome.runtime.sendMessage({ type: "settings-changed" }).catch(() => {});
  $("save").textContent = "Saved ✓"; setTimeout(() => ($("save").textContent = "Save"), 1500);
  setTimeout(load, 1500);
};

$("test").onclick = () => {
  // one or more links (separate with spaces or new lines) – several = a multi-card restock
  const urls = $("testUrl").value.split(/\s+/).filter((u) => u.startsWith("https://"));
  if (!urls.length) urls.push("https://www.gamestheshop.com/");
  const url = urls[0];
  const store = url.includes("amazon.in") ? "amazon" : url.includes("gamestheshop.com") ? "gamestheshop" : "test";
  chrome.runtime.sendMessage({ type: "test", url, urls, store });
};

$("allowShots").onclick = async () => {
  const ok = await chrome.permissions.request({ origins: ["<all_urls>"] }).catch(() => false);
  $("allowShots").textContent = ok ? "Screenshot fallback allowed ✓" : "Not allowed";
};
chrome.permissions.contains({ origins: ["<all_urls>"] }).then((ok) => { if (ok) $("allowShots").textContent = "Screenshot fallback allowed ✓"; });

chrome.storage.onChanged.addListener(load);
load();

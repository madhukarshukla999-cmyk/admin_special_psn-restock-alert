const $ = (id) => document.getElementById(id);
const DEFAULTS = { topic: "", enabled: true, autoClick: true, sound: true, maxPrice: 0, clickPayNow: true, amazonUpiQr: true, upiId: "", tgQr: true, tgToken: "", tgChatId: "", browserName: "", gtsMobile: "", qrTimeoutSec: 120,
                   stores: { gamestheshop: true, amazon: true, others: true } };

async function load() {
  const s = { ...DEFAULTS, ...(await chrome.storage.local.get(null)) };
  s.stores = { ...DEFAULTS.stores, ...(s.stores || {}) };
  $("topic").value = s.topic; $("enabled").checked = s.enabled; $("autoClick").checked = s.autoClick;
  $("sound").checked = s.sound; $("maxPrice").value = s.maxPrice || 0;
  $("clickPayNow").checked = s.clickPayNow; $("amazonUpiQr").checked = s.amazonUpiQr; $("tgQr").checked = s.tgQr; $("tgToken").value = s.tgToken || ""; $("tgChatId").value = s.tgChatId || "";
  $("browserName").value = s.browserName || ""; $("qrTimeoutSec").value = s.qrTimeoutSec || 120;
  $("chatWarn").textContent = /^-/.test(s.tgChatId || "") ? "⚠ This chat ID is a group – QRs and payment results would be seen by everyone in it. Use your own chat ID from /extension." : ""; $("gtsMobile").value = s.gtsMobile || "";
  const a = s.gtsAccount;
  $("gtsDetected").textContent = a ? `Detected GamesTheShop account: ${a.mobile ? "+91 " + a.mobile : "no mobile in profile"}${a.name ? " · " + a.name : ""}${a.email ? " · " + a.email : ""}`
                                   : "No GamesTheShop account detected yet – open gamestheshop.com once while signed in."; $("upiId").value = s.upiId || "";
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
    clickPayNow: $("clickPayNow").checked, amazonUpiQr: $("amazonUpiQr").checked, tgQr: $("tgQr").checked, tgToken: $("tgToken").value.trim(), tgChatId: $("tgChatId").value.trim(), browserName: $("browserName").value.trim(), qrTimeoutSec: Math.max(30, Number($("qrTimeoutSec").value) || 120), gtsMobile: $("gtsMobile").value.replace(/\D/g, "").slice(-10), upiId: $("upiId").value.trim(),
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

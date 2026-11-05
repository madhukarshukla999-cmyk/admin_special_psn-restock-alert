const $ = (id) => document.getElementById(id);
const DEFAULTS = {
  topic: "", server: "https://ntfy.sh", enabled: true, autoClick: true, sound: true, maxPrice: 0, clickPayNow: true,
  amazonUpiQr: true, upiId: "", tgQr: true, tgToken: "", tgChatId: "", browserName: "", gtsMobile: "", qrTimeoutSec: 120, qrMode: "dm", amzMobile: "", gtsPayWith: "upi", cardLast4: "", cardNumber: "", cardExpiry: "", cardName: "", cardCvv: "", cardAutoPay: true,
  browserType: "", profileName: "", accountName: "", setupDone: false, remotePaused: false,
  stores: { gamestheshop: true, amazon: true, others: true },
  // keep in step with AMZ_WATCH_DEFAULTS in background.js
  amzWatch: false, amzWatchSec: 15, amzWatchInfo: "",
  amzWatchList: "B093QF35KZ  ₹2000 card\nB07K6RYVJ5  ₹4000 card\nB0C1H3K2V4  PlayStation E-Gift Card",
};
let editing = false, typing = false;
$("version").textContent = "v" + chrome.runtime.getManifest().version + " · fast checkout";

// ---------- helpers ----------
async function detectBrowser() {
  try { if (navigator.brave && (await navigator.brave.isBrave())) return "Brave"; } catch { /* no */ }
  const brands = ((navigator.userAgentData && navigator.userAgentData.brands) || []).map((b) => b.brand).join(" ");
  const ua = navigator.userAgent;
  if (/Edge/i.test(brands) || / Edg\//.test(ua)) return "Edge";
  if (/Opera/i.test(brands) || / OPR\//.test(ua)) return "Opera";
  if (/Vivaldi/i.test(brands + ua)) return "Vivaldi";
  if (/Chrom/i.test(brands + ua)) return "Chrome";
  return "Other";
}
function parseCode(code) {
  code = (code || "").trim();
  if (code.startsWith("psnbot1:")) {
    try {
      const b = code.slice(8).replace(/-/g, "+").replace(/_/g, "/");
      const j = JSON.parse(decodeURIComponent(escape(atob(b + "===".slice((b.length + 3) % 4)))));
      return { topic: j.t, server: j.s || "https://ntfy.sh", tgChatId: String(j.c || ""), tgToken: j.k || "" };
    } catch { return null; }
  }
  if (/^psnbot-[\w-]{10,}$/.test(code)) return { topic: code };      // old-style connection code
  return code ? null : {};
}
const mobileOf = (v) => { const d = String(v || "").replace(/\D/g, "").replace(/^0+/, ""); const m = d.match(/^(?:91)?([6-9]\d{9})$/); return m ? m[1] : ""; };
const flag = (id, bad) => $(id).classList.toggle("missing", !!bad);

const qrModeVal = () => (document.querySelector('input[name="qrMode"]:checked') || {}).value || "dm";

// ---------- render ----------
async function load() {
  if (typing) return;
  const s = { ...DEFAULTS, ...(await chrome.storage.local.get(null)) };
  s.stores = { ...DEFAULTS.stores, ...(s.stores || {}) };
  const acc = s.gtsAccount || null;

  // status pill
  const fresh = s.connAt && Date.now() - s.connAt < 5 * 60 * 1000;
  const pill = $("status");
  const [txt, cls] = !s.setupDone && !s.topic ? ["Not connected", "bad"]
    : !s.enabled ? ["Off", "bad"]
    : s.remotePaused ? ["⏸ Paused", "warn"]
    : s.conn === "connected" && fresh ? ["● Connected", "ok"]
    : ["⏳ " + (s.conn || "connecting"), "warn"];
  pill.textContent = txt; pill.className = "pill " + cls;
  $("pausedBanner").classList.toggle("hidden", !s.remotePaused);
  $("revokedBanner").classList.toggle("hidden", !(s.revokedAt && !s.setupDone));

  // setup screen
  const showSetup = editing || !s.setupDone;
  $("setup").classList.toggle("hidden", !showSetup);
  $("main").classList.toggle("hidden", !s.setupDone);
  if (showSetup) {
    if (!$("browserType").dataset.touched) {
      const b = s.browserType || (await detectBrowser());
      $("browserType").value = b;
      $("browserHint").textContent = s.browserType ? "" : `Detected: ${b}`;
    }
    if (!$("profileName").value) $("profileName").value = s.profileName || "";
    if (!$("accountName").value) $("accountName").value = s.accountName || (acc && acc.name) || "";
    if (!$("setupMobile").value) $("setupMobile").value = s.gtsMobile || (acc && acc.mobile) || "";
    if (!$("topic").value) $("topic").value = s.topic || "";
    if (!$("tgToken").value) $("tgToken").value = s.tgToken || "";
    if (!$("tgChatId").value) $("tgChatId").value = s.tgChatId || "";
    const box = $("detectBox");
    if (acc) {
      box.classList.add("ok");
      $("detectText").innerHTML = `✅ GamesTheShop account found: <b>${acc.mobile ? "+91 " + acc.mobile : "no mobile in profile"}</b>` +
        (acc.name ? ` · ${acc.name}` : "") + (acc.email ? ` · ${acc.email}` : "") + (acc.mobile ? "" : " – type the mobile below");
      $("openGts").classList.add("hidden");
    } else {
      box.classList.remove("ok");
      $("detectText").textContent = "GamesTheShop account not detected yet. Open the site while signed in – it fills in by itself – or type the details below.";
      $("openGts").classList.remove("hidden");
    }
  }

  // identity card
  const mobile = s.gtsMobile || (acc && acc.mobile) || "";
  const name = s.accountName || (acc && acc.name) || "";
  $("idName").textContent = name || s.browserName || "This browser";
  $("idDetails").textContent = [s.browserType, s.profileName && "profile " + s.profileName, mobile && "+91 " + mobile].filter(Boolean).join(" · ") || "Not set up";
  $("avatar").textContent = (name || s.browserType || "?").trim().charAt(0).toUpperCase();

  // settings
  $("enabled").checked = s.enabled; $("autoClick").checked = s.autoClick; $("sound").checked = s.sound;
  $("maxPrice").value = s.maxPrice || 0; $("clickPayNow").checked = s.clickPayNow; $("amazonUpiQr").checked = s.amazonUpiQr;
  $("tgQr").checked = s.tgQr;
  const mode = !s.tgQr ? "off" : s.qrMode || "dm";
  for (const r of document.querySelectorAll('input[name="qrMode"]')) r.checked = r.value === mode;
  $("amzMobile").value = s.amzMobile || ""; $("gtsPayWith").value = s.gtsPayWith || "upi"; $("cardLast4").value = s.cardLast4 || "";
  $("cardAutoPay").checked = s.cardAutoPay; $("cardNumber").value = s.cardNumber || ""; $("cardName").value = s.cardName || ""; $("cardCvv").value = s.cardCvv || "";
  $("cardExpiry").value = s.cardExpiry ? s.cardExpiry.slice(0, 2) + "/" + s.cardExpiry.slice(2) : "";
  $("cardSaved").textContent = s.cardNumber ? `– saved ••${s.cardNumber.slice(-4)}${s.cardExpiry ? " · " + s.cardExpiry.slice(0, 2) + "/" + s.cardExpiry.slice(2) : ""}${s.cardCvv ? " · CVV stored" : ""}`
                                            : "– not set"; $("amzName").value = (s.amzAccount && s.amzAccount.name) || ""; $("qrTimeoutSec").value = s.qrTimeoutSec || 120; $("gtsMobile").value = s.gtsMobile || "";
  $("gtsMobile").placeholder = acc && acc.mobile ? `auto: +91 ${acc.mobile}` : "auto-detected";
  $("upiId").value = s.upiId || "";
  for (const k of ["gamestheshop", "amazon", "others"]) $("st_" + k).checked = s.stores[k];
  $("amzWatch").checked = s.amzWatch; $("amzWatchSec").value = s.amzWatchSec || 15;
  $("amzWatchList").value = s.amzWatchList;
  $("amzWatchInfo").textContent = s.amzWatch ? s.amzWatchInfo || "Starting…" : "";
  const group = /^-/.test(s.tgChatId || "");
  $("chatWarn").classList.toggle("hidden", !group);
  $("chatWarn").textContent = group ? "⚠ The chat ID is a group – everyone in it would see QRs and payment results. Use your own chat ID." : "";
  $("gtsDetected").textContent = !s.tgToken || !s.tgChatId ? "⚠ Not connected to Telegram – reconnect with a setup code." : "";
  $("log").textContent = (s.logLines || []).join("\n") || "No activity yet.";
}

// ---------- setup / connect ----------
$("setupCode").oninput = () => {
  const c = parseCode($("setupCode").value);
  const h = $("codeHint");
  if (c === null) { h.textContent = "⚠ That doesn't look like a setup code."; h.className = "hint"; return; }
  if (!c.topic) { h.textContent = ""; return; }
  $("topic").value = c.topic;
  if (c.tgToken) $("tgToken").value = c.tgToken;
  if (c.tgChatId) $("tgChatId").value = c.tgChatId;
  h.textContent = c.tgToken ? "✓ Setup code OK – feed, Telegram chat and bot token filled in." : "✓ Connection code OK – also enter the bot token and chat ID below (Manual).";
  h.className = "hint ok";
};
$("browserType").onchange = () => { $("browserType").dataset.touched = "1"; $("browserHint").textContent = ""; };
for (const id of ["profileName", "accountName", "setupMobile", "setupCode", "topic", "tgToken", "tgChatId"]) {
  $(id).addEventListener("focus", () => (typing = true));
  $(id).addEventListener("blur", () => { typing = false; setTimeout(load, 50); });
}

$("connect").onclick = async () => {
  const err = $("setupError"), btn = $("connect");
  err.classList.add("hidden");
  const { gtsAccount: acc } = await chrome.storage.local.get("gtsAccount");
  const v = {
    topic: $("topic").value.trim(), tgToken: $("tgToken").value.trim(), tgChatId: $("tgChatId").value.trim(),
    browserType: $("browserType").value, profileName: $("profileName").value.trim(),
    accountName: $("accountName").value.trim() || (acc && acc.name) || "",
    mobile: mobileOf($("setupMobile").value) || (acc && acc.mobile) || "",
  };
  const code = parseCode($("setupCode").value);
  if (code && code.server) v.server = code.server;
  const missing = [];
  if (!v.topic) missing.push(["setupCode", "setup code"]);
  if (v.topic && (!v.tgToken || !v.tgChatId)) missing.push(["setupCode", "bot token / chat ID (use the new setup code from /extension)"]);
  if (!v.profileName) missing.push(["profileName", "profile name"]);
  if (!v.accountName) missing.push(["accountName", "account name"]);
  if (!v.mobile) missing.push(["setupMobile", "account mobile number (10 digits)"]);
  for (const id of ["setupCode", "profileName", "accountName", "setupMobile"]) flag(id, missing.some((m) => m[0] === id));
  if (missing.length) { err.textContent = "Please fill in: " + [...new Set(missing.map((m) => m[1]))].join(", ") + "."; err.classList.remove("hidden"); return; }

  btn.disabled = true; btn.textContent = "Checking the bot…";
  const me = await fetch(`https://api.telegram.org/bot${v.tgToken}/getMe`).then((r) => r.json()).catch(() => ({}));
  if (!me.ok) {
    err.textContent = "Telegram rejected the bot token – copy a fresh setup code from /extension.";
    err.classList.remove("hidden"); btn.disabled = false; btn.textContent = "Connect"; return;
  }
  const detected = acc && acc.mobile;
  await chrome.storage.local.set({
    topic: v.topic, ...(v.server ? { server: v.server } : {}), tgToken: v.tgToken, tgChatId: v.tgChatId,
    browserType: v.browserType, profileName: v.profileName, accountName: v.accountName,
    gtsMobile: detected && detected === v.mobile ? "" : v.mobile,                  // typed = override
    browserName: `${v.browserType} · ${v.profileName} · ${v.accountName}`, setupDone: true, enabled: true, revokedAt: 0,
  });
  chrome.runtime.sendMessage({ type: "settings-changed" }).catch(() => {});
  btn.textContent = "Connecting to the feed…";
  for (let i = 0; i < 20; i++) {
    const { conn } = await chrome.storage.local.get("conn");
    if (conn === "connected") break;
    await new Promise((r) => setTimeout(r, 500));
  }
  const sent = await chrome.runtime.sendMessage({ type: "connected-report" }).catch(() => false);
  btn.disabled = false; btn.textContent = "Connect";
  editing = false;
  $("setupCode").value = ""; $("codeHint").textContent = "";
  await load();
  if (!sent) { $("gtsDetected").textContent = "Connected, but the ✅ message to Telegram failed – check the chat ID."; }
};
$("editSetup").onclick = () => { editing = true; load(); $("setup").scrollIntoView({ behavior: "smooth" }); };

// ---------- save other settings ----------
$("save").onclick = async () => {
  await chrome.storage.local.set({
    enabled: $("enabled").checked, autoClick: $("autoClick").checked, sound: $("sound").checked,
    maxPrice: Number($("maxPrice").value) || 0, clickPayNow: $("clickPayNow").checked, amazonUpiQr: $("amazonUpiQr").checked,
    tgQr: qrModeVal() !== "off", qrMode: qrModeVal() === "off" ? "dm" : qrModeVal(), amzMobile: mobileOf($("amzMobile").value), gtsPayWith: $("gtsPayWith").value,
    cardNumber: $("cardNumber").value.replace(/\D/g, ""), cardExpiry: $("cardExpiry").value.replace(/\D/g, "").slice(0, 4),
    cardName: $("cardName").value.trim(), cardCvv: $("cardCvv").value.replace(/\D/g, ""), cardAutoPay: $("cardAutoPay").checked,
    cardLast4: ($("cardNumber").value.replace(/\D/g, "") || $("cardLast4").value).slice(-4), qrTimeoutSec: Math.max(30, Number($("qrTimeoutSec").value) || 120),
    gtsMobile: mobileOf($("gtsMobile").value), upiId: $("upiId").value.trim(),
    stores: { gamestheshop: $("st_gamestheshop").checked, amazon: $("st_amazon").checked, others: $("st_others").checked },
    amzWatch: $("amzWatch").checked, amzWatchSec: Math.max(5, Number($("amzWatchSec").value) || 15),
    amzWatchList: $("amzWatchList").value.trim(),
  });
  chrome.runtime.sendMessage({ type: "settings-changed" }).catch(() => {});
  $("save").textContent = "Saved ✓"; setTimeout(() => ($("save").textContent = "Save"), 1500);
};

// ---------- test ----------
$("test").onclick = () => {
  const urls = $("testUrl").value.split(/\s+/).filter((u) => u.startsWith("https://"));
  if (!urls.length) urls.push("https://www.gamestheshop.com/");
  const url = urls[0];
  const store = url.includes("amazon.in") ? "amazon" : url.includes("gamestheshop.com") ? "gamestheshop" : "test";
  chrome.runtime.sendMessage({ type: "test", url, urls, store });
};

// ---------- screenshots permission ----------
$("allowShots").onclick = async () => {
  const ok = await chrome.permissions.request({ origins: ["<all_urls>"] }).catch(() => false);
  $("allowShots").textContent = ok ? "Screenshots allowed ✓" : "Not allowed";
};
chrome.permissions.contains({ origins: ["<all_urls>"] }).then((ok) => { if (ok) $("allowShots").textContent = "Screenshots allowed ✓"; });

$("cardClear").onclick = async () => {
  await chrome.storage.local.set({ cardNumber: "", cardExpiry: "", cardName: "", cardCvv: "" });
  for (const id of ["cardNumber", "cardExpiry", "cardName", "cardCvv"]) $(id).value = "";
  $("cardClear").textContent = "Cleared ✓"; setTimeout(() => ($("cardClear").textContent = "Clear card details"), 1500);
};
$("cardExpiry").addEventListener("input", (e) => {       // 1227 -> 12/27 as you type
  const d = e.target.value.replace(/\D/g, "").slice(0, 4);
  e.target.value = d.length > 2 ? d.slice(0, 2) + "/" + d.slice(2) : d;
});
for (const id of ["cardNumber", "cardExpiry", "cardName", "cardCvv", "cardLast4", "amzWatchList", "amzWatchSec"]) {
  $(id).addEventListener("focus", () => (typing = true));
  $(id).addEventListener("blur", () => { typing = false; });
}

chrome.storage.onChanged.addListener(() => load());
load();

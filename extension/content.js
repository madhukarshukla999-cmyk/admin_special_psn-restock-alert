// Runs on GamesTheShop and Amazon, but only acts on tabs the extension itself opened
// for an alert (asks the background first). Clicks up to the checkout page, then stops.
// It never clicks "Place order" / "Pay".

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// GamesTheShop is a Next.js app: BUY NOW / Pay Now change the URL without a page load, so this
// script isn't re-injected. We watch the URL ourselves and stop any wait once the page moved on.
let href = location.href;
const moved = () => location.href !== href;

async function find(test, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end && !moved()) {
    const el = test();
    if (el) return el;
    await sleep(250);
  }
  return null;
}

const byText = (re, sel = "button, input[type=submit], a") => () =>
  [...document.querySelectorAll(sel)].find((e) => {
    const t = (e.innerText || e.value || "").trim();
    return re.test(t) && !e.disabled && e.offsetParent !== null;
  });

// (if the page already moved on while we were looking, say nothing: the next page takes over)
const step = (s, note, done = false) => moved() ? Promise.resolve()
  : chrome.runtime.sendMessage({ type: "job-step", step: s, note, done }).catch(() => {});

async function opts() {
  return chrome.storage.local.get({ clickPayNow: true, upiId: "" });
}

async function gamestheshop(job) {
  const path = location.pathname;
  if (path.startsWith("/checkout")) {
    const o = await opts();
    if (!o.clickPayNow || job.step === "paynow")
      return step("checkout", "GamesTheShop: checkout open – tap Pay Now and pay.", true);
    const btn = await find(byText(/^pay now$/i));
    if (!btn) return step("checkout", "GamesTheShop: checkout open but 'Pay Now' not found – tap it yourself.", true);
    await step("paynow");
    btn.click();                     // only opens Paytm's payment page; nothing is paid until you approve
    return;
  }
  if (path.startsWith("/login")) return step("login", "GamesTheShop: you're not logged in in this browser. Log in once, then it goes straight to checkout.", true);
  if (path.startsWith("/product/") && job.step === "start") {
    const btn = await find(byText(/^buy now$/i));
    if (!btn) {
      const oos = /out of stock|sold out|notify me/i.test(document.body.innerText);
      return step("none", `GamesTheShop: ${oos ? "already sold out" : "BUY NOW button not found"} – check the tab.`, true);
    }
    await step("clicked");
    btn.click();
    return;
  }
  if (job.step === "start")
    return step("none", "GamesTheShop: opened a page that isn't a product – nothing to click. " +
                        "To test BUY NOW use /extension test <product link>.", true);
}

// Amazon: product page → Buy Now (straight to checkout); fallback Add to Cart → Proceed to Buy.
const visible = (e) => e && !e.disabled && (e.offsetParent !== null || e.getClientRects().length > 0);
const q = (...sels) => () => sels.map((s) => document.querySelector(s)).find(visible) || null;
const AMZ_BUY_NOW = q("#buy-now-button", 'input[name="submit.buy-now"]', "#buyNow");
const AMZ_ADD = q("#add-to-cart-button", 'input[name="submit.add-to-cart"]', "#gc-buy-box-atc");
const AMZ_PROCEED = () => q('input[name="proceedToRetailCheckout"]', "#sc-buy-box-ptc-button input",
  "#attach-sidesheet-checkout-button input", "#hlb-ptc-btn-native")() || byText(/^proceed to (buy|checkout)/i)();
const AMZ_TURBO = q("#turbo-checkout-iframe", "#turbo-checkout-panel-container", "#turbo-checkout-modal");

function amazonUnavailable() {
  const a = (document.querySelector("#availability, #outOfStock") || {}).innerText || "";
  return /currently unavailable|out of stock|unavailable/i.test(a);
}

// Amazon buttons are <span class="a-button"><input type=submit> + <span>text</span></span>:
// find the visible text, then click the real input/button of that a-button.
function clickable(el) {
  const box = el.closest(".a-button, button, a, label, [role=button], [role=radio]");
  if (box && box.matches(".a-button")) return box.querySelector("input, button") || el;
  return box || el;
}
function textEl(re, sel = "span, a, button, input[type=submit], label, h1, h2, h3, h4, h5, div") {
  return [...document.querySelectorAll(sel)].filter((e) => visible(e) &&
    re.test(((e.innerText || e.value || e.getAttribute("aria-label") || "") + "").trim()))
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length)[0] || null;  // innermost
}

// The UPI QR option on Amazon's payment page ("Scan and Pay with UPI")
function amzUpiQrOption() {
  const t = textEl(/^scan and pay with/i);
  if (!t) return null;
  // nearest ancestor holding exactly one radio button = this option's row
  for (let el = t, i = 0; el && i < 8; el = el.parentElement, i++) {
    const radios = el.querySelectorAll('input[type="radio"]');
    if (radios.length === 1) return radios[0];
    if (radios.length > 1) break;
  }
  return t;
}
// small box around an element (first ancestor with a bit of text), to tell what it belongs to
function boxText(el) {
  for (let p = el.parentElement, i = 0; p && i < 6; p = p.parentElement, i++)
    if ((p.innerText || "").trim().length > 25) return p.innerText;
  return "";
}
const enabled = (e) => visible(e) && e.getAttribute("aria-disabled") !== "true" &&
  !(e.closest && e.closest(".a-button-disabled"));
const amzUseThisPayment = () => {
  const t = [...document.querySelectorAll("span, button, input[type=submit], a")].filter((e) => visible(e) &&
    /^use this payment method$/i.test(((e.innerText || e.value || e.getAttribute("aria-label") || "") + "").trim()));
  return t.map(clickable).find(enabled) || null;     // the grey (disabled) one doesn't count
};

// one line describing the page, logged when a step fails, so we can see what Amazon changed
function amzDiag() {
  const frames = [...document.querySelectorAll("iframe")].filter(visible)
    .map((f) => { try { return new URL(f.src).hostname + new URL(f.src).pathname.slice(0, 20); } catch { return f.name || "?"; } });
  const btns = [...document.querySelectorAll("button, input[type=submit], .a-button-text")].filter(visible)
    .map((b) => (b.innerText || b.value || b.getAttribute("aria-label") || "").trim()).filter(Boolean).slice(0, 8);
  return `[debug ${location.pathname.slice(0, 40)} | scanText:${/scan and pay/i.test(document.body.innerText)} ` +
         `radios:${document.querySelectorAll('input[type=radio]').length} frames:${JSON.stringify(frames)} btns:${JSON.stringify(btns)}]`;
}
// On the order review page: is the chosen payment the UPI QR, and is the button "Pay with UPI"?
const amzQrChosen = () => /pay by scanning the qr code|scan and pay with upi/i.test(document.body.innerText);
const amzPayWithUpi = () => {
  const t = textEl(/^pay with upi$/i);
  return t ? clickable(t) : null;
};
const amzChangePayment = () => {
  // the "Change" link in the payment box (the one that mentions paying / payment method)
  const links = [...document.querySelectorAll("a, span.a-button-text, input[type=submit]")]
    .filter((e) => visible(e) && /^change$/i.test((e.innerText || e.value || "").trim()));
  return links.find((l) => { const t = boxText(l); return /pay(ment| by| with|ing with)/i.test(t) && !/deliver/i.test(t); }) || null;
};

// a square image/canvas of QR size, near text that mentions scanning / QR / UPI
function findQrEl() {
  return [...document.querySelectorAll("img, canvas, svg")].find((e) => {
    const r = e.getBoundingClientRect();
    return r.width >= 120 && Math.abs(r.width - r.height) < r.width * 0.15 && /scan|qr|upi/i.test(boxText(e));
  }) || null;
}
function highlightQr() {
  const qr = findQrEl();
  if (qr) highlight(qr.parentElement || qr);
  return !!qr;
}
// Amazon: QR is on screen → send it to Telegram (DM / notify mode) and watch for the result
async function amazonQrReady() {
  const el = findQrEl();
  const sent = el ? await sendQrToTelegram(el.parentElement || el) : false;
  if (sent) watchPayment(false);
  return step("qr", "Amazon: QR is ready – scan it with your UPI app and enter your PIN." + (sent ? " (Telegram notified)" : ""), true);
}

// A click like a real mouse: some pages only react to pointer/mouse down+up, not a bare click().
function realClick(el) {
  if (!el) return;
  try { el.scrollIntoView({ block: "center" }); } catch { /* ignore */ }
  const r = el.getBoundingClientRect();
  const o = { bubbles: true, cancelable: true, composed: true, view: window, button: 0,
              clientX: r.left + Math.min(10, r.width / 2), clientY: r.top + r.height / 2 };
  for (const t of ["pointerover", "pointerdown", "mousedown", "pointerup", "mouseup"])
    el.dispatchEvent(t.startsWith("pointer") ? new PointerEvent(t, { ...o, pointerType: "mouse", isPrimary: true }) : new MouseEvent(t, o));
  el.click();
}

// Selects "Scan and Pay with UPI" in this document and returns the enabled
// "Use this payment method" button (or null). Used by the top page and by iframes.
async function amzChooseUpi() {
  const ready = () => {
    const r = amzUpiQrOption();
    return (!r || r.type !== "radio" || r.checked) && amzUseThisPayment();
  };
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = amzUpiQrOption(), t = textEl(/^scan and pay with/i);
    if (!r && !t) return null;
    // radio first; then the row / label / text, in case Amazon listens on those
    const targets = attempt === 0 ? [r]
      : [t && t.closest("label, [role=radio], [data-pmts-component-id], .a-radio, li") , t, r];
    for (const el of targets) {
      if (!el || (r && r.type === "radio" && r.checked)) break;
      realClick(el);
      await sleep(150);
    }
    const use = await find(ready, attempt === 0 ? 2500 : 3500);
    if (use) return use;
  }
  return null;
}

// iframes tell the top page what they did (cross-origin safe: window.postMessage)
const frameState = { chosen: false, pressed: false };
window.addEventListener("message", (e) => {
  let okOrigin = false;
  try { okOrigin = /(^|\.)amazon\.in$/.test(new URL(e.origin).hostname); } catch { /* ignore */ }
  if (!okOrigin || !e.data || e.data.psnbot !== 1) return;
  if (e.data.chosen) frameState.chosen = true;
  if (e.data.pressed) frameState.pressed = true;
});

// Runs in Amazon iframes during checkout: only picks "Scan and Pay with UPI" (never pays).
async function amazonFrame(job) {
  if (job.step === "paid") return;
  const o = await chrome.storage.local.get({ amazonUpiQr: true });
  if (!o.amazonUpiQr) return;
  const found = await find(() => amzUpiQrOption() || textEl(/^scan and pay with/i), 25000);
  if (!found) return;
  const use = await amzChooseUpi();
  const r = amzUpiQrOption();
  if (!use && r && r.type === "radio" && !r.checked) return;     // couldn't select it
  window.top.postMessage({ psnbot: 1, chosen: true }, "*");
  if (use) { realClick(use); window.top.postMessage({ psnbot: 1, pressed: true }, "*"); }
}

async function amazonCheckout(job) {
  const o = await chrome.storage.local.get({ amazonUpiQr: true });
  const manual = "Amazon: checkout open – pick how to pay and finish it yourself.";
  if (!o.amazonUpiQr) return step("checkout", manual, true);
  if (job.step === "paid") {                            // after "Pay with UPI": the QR page
    const ok = await find(() => highlightQr(), 15000);
    if (ok) return amazonQrReady();
    return step("qr", "Amazon: UPI payment page open – scan the QR and enter your PIN.", true);
  }

  // 1) payment-method page: choose "Scan and Pay with UPI", then "Use this payment method"
  // 2) review page: press "Pay with UPI" (only then – never "Place your order" for card/COD)
  const what = await find(() => {
    if (amzPayWithUpi() && amzQrChosen()) return "pay";
    if (amzUpiQrOption()) return "choose";
    if (frameState.chosen) return "frame";
    if (job.step !== "changepay" && amzChangePayment() && textEl(/^(place your order|pay with|pay now)/i)) return "change";
    return null;
  }, 25000);

  if (what === "pay") {
    await step("paid", "Amazon: review page – clicking 'Pay with UPI'…");
    realClick(amzPayWithUpi());    // order waits for your UPI payment; nothing is paid until you scan + PIN
    if (await find(() => !amzPayWithUpi() && highlightQr(), 15000))      // QR shown on the same page
      return amazonQrReady();
    return;
  }
  if (what === "frame") {
    // the list is in an iframe; that frame's copy of this script picked UPI. Press the button
    // here (top-right "Use this payment method") unless the frame already pressed its own.
    await step("chosen", "Amazon: UPI QR chosen – clicking 'Use this payment method'…");
    await sleep(400);
    if (!frameState.pressed && !moved()) { const use = amzUseThisPayment(); if (use) realClick(use); }
    if (await find(() => amzPayWithUpi() && amzQrChosen(), 15000)) return amazonCheckout({ ...job, step: "chosen" });
    return;
  }
  if (what === "choose") {
    await step("choosing", "Amazon: payment page – choosing 'Scan and Pay with UPI'…");
    const use = await amzChooseUpi();
    if (!use) return step("checkout", "Amazon: couldn't select 'Scan and Pay with UPI' – tap it, then 'Use this payment method'. " + amzDiag(), true);
    await step("chosen", "Amazon: UPI QR chosen – clicking 'Use this payment method'…");
    realClick(use);
    // review page: usually a new URL (handled by the main loop); if it updates in place, go on here
    if (await find(() => amzPayWithUpi() && amzQrChosen(), 15000)) return amazonCheckout({ ...job, step: "chosen" });
    return;
  }
  if (what === "change") {
    await step("changepay", "Amazon: review page has another payment method – clicking 'Change'…");
    realClick(amzChangePayment());
    if (await find(amzUpiQrOption, 10000)) return amazonCheckout({ ...job, step: "changepay" });
    return;
  }
  return step("checkout", manual + " " + amzDiag(), true);
}

async function amazon(job) {
  const path = location.pathname;
  if (/\/(checkout|gp\/buy)\//.test(path) || /\/spc\//.test(path)) return amazonCheckout(job);
  if (/\/ap\/signin/.test(path)) return step("login", "Amazon: please log in in this browser once.", true);

  if (/\/(dp|gp\/product|gp\/aw\/d)\//.test(path) && job.step === "start") {
    const btn = await find(() => AMZ_BUY_NOW() || AMZ_ADD() || (amazonUnavailable() && "na"), 12000);
    if (!btn || btn === "na")
      return step("none", `Amazon: ${btn ? "this card is currently unavailable (sold out)" : "no Buy Now / Add to Cart button " + amzDiag()} – check the tab.`, true);
    const buyNow = btn === AMZ_BUY_NOW();
    await step(buyNow ? "buynow" : "added", `Amazon: product page – clicking '${buyNow ? "Buy Now" : "Add to Cart"}'…`);
    btn.click();
    if (buyNow) {
      // Buy Now either goes to checkout (new page) or opens Amazon's quick-checkout box on this page
      const turbo = await find(AMZ_TURBO, 8000);
      if (turbo) return step("checkout", "Amazon: quick checkout pop-up opened instead of the checkout page – finish it yourself. " + amzDiag(), true);
      return;
    }
    // Add to Cart may show a side panel on the same page with "Proceed to checkout"
    const go = await find(AMZ_PROCEED, 8000);
    if (go) { await step("proceed"); go.click(); }
    return;
  }

  if (/\/(gp\/cart|cart)/.test(path) || job.step === "added") {
    const btn = await find(() => AMZ_PROCEED() ||
      (/your amazon cart is empty/i.test(document.body.innerText) && "empty"), 10000);
    if (btn === "empty")
      return step("none", "Amazon: the cart is empty – the card sold out before it could be added.", true);
    if (btn) { await step("proceed"); btn.click(); return; }
    return step("none", "Amazon: couldn't find 'Proceed to Buy' – finish in the tab.", true);
  }
}

// ---- Paytm payment page (GamesTheShop's gateway) ----
function setValue(input, value) {
  const proto = Object.getPrototypeOf(input);
  Object.getOwnPropertyDescriptor(proto, "value").set.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function highlight(el) {
  el.scrollIntoView({ block: "center" });
  el.style.outline = "6px solid #22c55e";
  el.style.outlineOffset = "6px";
  el.style.borderRadius = "8px";
}

async function paytm(job) {
  const o = await opts();
  if (o.upiId) {
    // UPI ID: a payment request goes to your UPI app; you approve it there with your PIN.
    const upiRow = await find(() => [...document.querySelectorAll("div, li, a, button, span")]
      .find((e) => /^upi$/i.test((e.innerText || "").trim()) && e.offsetParent !== null), 15000);
    if (upiRow) {
      (upiRow.closest("li, a, button, [role=button]") || upiRow).click();
      const input = await find(() => [...document.querySelectorAll("input")]
        .find((i) => i.offsetParent !== null && /upi|vpa|@/i.test(i.placeholder + " " + i.name + " " + i.id + " " + (i.getAttribute("aria-label") || ""))), 10000);
      if (input) {
        input.focus(); setValue(input, o.upiId);
        const btn = await find(byText(/^(verify( (and|&) pay)?|pay( now| ₹.*)?|proceed)$/i), 8000);
        if (btn) {
          await sleep(300); btn.click();
          return step("upi", `Paytm: payment request sent to ${o.upiId} – approve it in your UPI app now.`, true);
        }
      }
    }
    // fall through to the QR if the UPI form wasn't found
  }
  // smallest visible box containing both the "Scan QR" text and an image/canvas (the QR itself)
  const qr = await find(() => {
    const boxes = [...document.querySelectorAll("div, section, li")].filter((e) =>
      e.offsetParent !== null && /scan qr/i.test(e.innerText || "") && e.querySelector("img, canvas, svg"));
    return boxes.sort((a, b) => a.innerText.length - b.innerText.length)[0] || null;
  }, 15000);
  if (qr) {
    highlight(qr);
    const sent = await sendQrToTelegram(qr);
    await step("qr", "Paytm: QR is ready – scan it with your UPI app and enter your PIN." +
                     (sent ? " (also sent to Telegram)" : ""), true);
    if (sent) watchPayment(false);          // this page shows the QR: only NEW success text counts
    return;
  }
  return step("pay", "Paytm: payment page open – choose how to pay.", true);
}

// The QR as a PNG data URL if we can read it here; otherwise its address or its position on
// screen (the background then fetches it or takes a screenshot).
async function qrImage(box) {
  const els = [...box.querySelectorAll("canvas, img, svg")].filter((e) => {
    const r = e.getBoundingClientRect(); return r.width >= 80 && r.height >= 80;
  }).sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
  const el = els[0];
  if (!el) return {};
  const r = el.getBoundingClientRect();
  const out = { rect: window === window.top ? { x: r.left, y: r.top, w: r.width, h: r.height, dpr: devicePixelRatio } : null };
  try {
    if (el.tagName === "CANVAS") return { ...out, dataUrl: el.toDataURL("image/png") };
    let src = el.tagName === "IMG" ? el.currentSrc || el.src : null;
    if (el.tagName.toLowerCase() === "svg")
      src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(el));
    if (src && src.startsWith("data:image/png")) return { ...out, dataUrl: src };
    if (src) {
      // redraw on a white canvas → PNG (fails for cross-origin images; then the background fetches it)
      const img = new Image(); img.crossOrigin = "anonymous";
      await new Promise((ok, bad) => { img.onload = ok; img.onerror = bad; img.src = src; });
      const size = Math.max(img.naturalWidth || 0, 400);
      const c = document.createElement("canvas"); c.width = c.height = size + 40;
      const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
      g.drawImage(img, 20, 20, size, size);
      return { ...out, dataUrl: c.toDataURL("image/png") };
    }
  } catch { /* tainted canvas / blocked image */ }
  return { ...out, src: el.tagName === "IMG" && /^https:/.test(el.src) ? el.src : null };
}

async function sendQrToTelegram(box) {
  const o = await chrome.storage.local.get({ tgQr: true, tgToken: "", tgChatId: "" });
  if (!o.tgQr || !o.tgToken || !o.tgChatId) return false;
  const { qrMode = "dm" } = await chrome.storage.local.get("qrMode");
  await sleep(800);                        // let the QR finish drawing
  const img = qrMode === "notify" ? {} : await qrImage(box);
  return chrome.runtime.sendMessage({ type: "qr", ...img, ...paytmDetails() }).catch(() => false);
}

// ---- which GamesTheShop account is signed in (read from the site's own localStorage) ----
function jwtPayload(tok) {
  try { return JSON.parse(atob(tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return null; }
}
function findMobile(obj, depth = 0) {
  if (!obj || depth > 4) return "";
  const norm = (v) => { const d = String(v).replace(/\D/g, "").replace(/^0+/, ""); const m = d.match(/^(?:91)?([6-9]\d{9})$/); return m ? m[1] : ""; };
  for (const [k, v] of Object.entries(obj))
    if (/phone|mobile|contact_?no|msisdn/i.test(k) && (typeof v === "string" || typeof v === "number") && norm(v)) return norm(v);
  for (const v of Object.values(obj))
    if (v && typeof v === "object") { const m = findMobile(v, depth + 1); if (m) return m; }
  return "";
}
async function detectGtsAccount() {
  if (location.hostname !== "www.gamestheshop.com" || window !== window.top) return;
  let user = null;
  try { user = JSON.parse(localStorage.getItem("user") || "null"); } catch { /* ignore */ }
  const tok = localStorage.getItem("accessToken");
  const jwt = tok ? jwtPayload(tok) : null;
  if (!user && !jwt) return;                       // not signed in here
  const acc = {
    mobile: findMobile(user) || findMobile(jwt),
    name: (user && (user.display_name || user.name || [user.first_name, user.last_name].filter(Boolean).join(" "))) || (jwt && jwt.name) || "",
    email: (user && (user.email || user.contact_email)) || (jwt && (jwt.email || jwt.contact_email)) || "",
    id: String((user && user.id) || (jwt && (jwt.user_id || jwt.id || jwt.sub)) || ""),
  };
  const { gtsAccount } = await chrome.storage.local.get("gtsAccount");
  if (JSON.stringify(gtsAccount || {}) !== JSON.stringify(acc)) await chrome.storage.local.set({ gtsAccount: acc });
}

async function detectAmazonAccount() {
  if (location.hostname !== "www.amazon.in" || window !== window.top) return;
  const el = document.querySelector("#nav-link-accountList-nav-line-1, #nav-greeting-name, .nav-line-1-container .nav-line-1");
  const m = ((el && el.innerText) || "").match(/hello,?\s*(.+)/i);
  const name = m ? m[1].trim() : "";
  if (!name || /sign in/i.test(name)) return;
  const { amzAccount } = await chrome.storage.local.get("amzAccount");
  if (!amzAccount || amzAccount.name !== name) await chrome.storage.local.set({ amzAccount: { name } });
}

// order id / amount shown on the Paytm page, for the Telegram caption
function paytmDetails() {
  const txt = document.body.innerText || "";
  const order = (txt.match(/order\s*(?:id|no\.?|number)\s*[:#]?\s*([A-Za-z0-9_\-]{6,})/i) || [])[1] || "";
  const amount = (txt.match(/₹\s?([\d,]+(?:\.\d{1,2})?)/) || [])[1] || "";
  return { order, amount };
}

// ---- after the QR: watch this tab for "payment successful" / "failed" (Paytm page or the shop's
// order page it redirects to) and tell the background, which updates Telegram.
const PAY_OK = /payment (was |is |has been )?success(ful)?|transaction success(ful)?|paid successfully|payment (received|completed|done)|order (has been |was )?(placed|confirmed|successful)|thank you for (your )?(order|purchase|shopping)/i;
const PAY_FAIL = /payment (has |was )?(failed|declined|cancell?ed|unsuccessful)|transaction (failed|declined|cancell?ed)|order (failed|cancell?ed)/i;
let watching = false;
// short visible texts on the page that match (a heading / message, not a whole paragraph of FAQ)
function payLines(re) {
  return [...document.querySelectorAll("h1, h2, h3, h4, h5, p, div, span, strong, b")]
    .filter((e) => e.offsetParent !== null && e.children.length < 4)
    .map((e) => (e.innerText || "").trim()).filter((x) => x.length < 140 && re.test(x));
}
async function watchPayment(baselineOk) {
  if (watching) return;
  watching = true;
  // on the QR page itself, ignore texts already there (e.g. an FAQ "what if my payment failed?")
  const base = baselineOk ? new Set() : new Set([...payLines(PAY_OK), ...payLines(PAY_FAIL)]);
  const end = Date.now() + 10 * 60 * 1000;
  while (Date.now() < end) {
    const ok = payLines(PAY_OK).filter((x) => !base.has(x)), fail = payLines(PAY_FAIL).filter((x) => !base.has(x));
    if (ok.length || fail.length) {
      await sleep(800);
      const txt = document.body.innerText || "";
      const line = (ok.length ? ok : fail).sort((a, b) => a.length - b.length)[0];
      const order = (txt.match(/order\s*(?:id|no\.?|number)\s*[:#]?\s*([A-Za-z0-9_\-]{6,})/i) || [])[1] || "";
      await chrome.runtime.sendMessage({ type: "qr-result", result: ok.length ? "success" : "failed", line, order }).catch(() => {});
      return;
    }
    const still = await chrome.runtime.sendMessage({ type: "watch?" }).catch(() => null);
    if (!still) return;                    // expired or already reported
    await sleep(1000);
  }
}

async function getJob() {
  // the background saves the job right after opening the tab; on a fast page we can ask first
  for (let i = 0; i < 6; i++) {
    const job = await chrome.runtime.sendMessage({ type: "job?" }).catch(() => null);
    if (job) return job;
    await sleep(500);
  }
  return null;
}

(async () => {
  const host = location.hostname;
  detectGtsAccount().catch(() => {});
  detectAmazonAccount().catch(() => {});
  if (window === window.top) {             // a tab whose QR is still waiting: watch it for the result
    chrome.runtime.sendMessage({ type: "watch?" }).then((w) => { if (w) watchPayment(true); }).catch(() => {});
  }
  const isPaytm = host.endsWith("paytmpayments.com") || host.endsWith("paytm.in");
  const isAmazon = host === "amazon.in" || host.endsWith(".amazon.in");
  if (window !== window.top) {
    // iframes: Paytm runs fully; Amazon's payment-list frame only selects UPI; others do nothing
    if (!isPaytm && !isAmazon) return;
    if (isAmazon) { const job = await getJob(); if (job) await amazonFrame(job); return; }
  } else if (isAmazon && host !== "www.amazon.in") return;
  let job = await getJob();
  if (!job) return;                                  // not a tab we opened: do nothing
  for (;;) {
    href = location.href;
    if (host === "www.gamestheshop.com") await gamestheshop(job);
    else if (host === "www.amazon.in") await amazon(job);
    else if (isPaytm) await paytm(job);
    // in-page navigation (Next.js / SPA): wait for the URL to change, then handle the new page
    const end = Date.now() + 60000;
    while (!moved() && Date.now() < end) await sleep(200);
    if (!moved()) return;
    await sleep(300);                                // let the new page render
    detectGtsAccount().catch(() => {});
    job = await chrome.runtime.sendMessage({ type: "job?" }).catch(() => null);
    if (!job) return;                                // finished (or expired)
  }
})();

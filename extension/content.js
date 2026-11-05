// Runs on GamesTheShop and Amazon, but only acts on tabs the extension itself opened
// for an alert (asks the background first). Clicks up to the checkout page, then stops.
// It never clicks "Place order" / "Pay".

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// lets the background check which pages/frames the extension can actually reach
chrome.runtime.onMessage.addListener((m, s, reply) => {
  if (m && m.type === "ping") { reply({ host: location.hostname, top: window === window.top }); return true; }
});

// GamesTheShop is a Next.js app: BUY NOW / Pay Now change the URL without a page load, so this
// script isn't re-injected. We watch the URL ourselves and stop any wait once the page moved on.
let href = location.href;
const moved = () => location.href !== href;

// Wait for test() to return something: re-checked on every page change (MutationObserver) and
// every 100 ms, so a button is clicked the moment it appears.
function find(test, timeoutMs = 15000) {
  const safe = () => { try { return test(); } catch { return null; } };
  const first = safe();
  if (first) return Promise.resolve(first);
  return new Promise((resolve) => {
    let done = false, obs = null, timer = null;
    const finish = (v) => { if (done) return; done = true; if (obs) obs.disconnect(); clearInterval(timer); resolve(v); };
    const end = Date.now() + timeoutMs;
    const check = () => {
      if (done) return;
      if (moved() || Date.now() > end) return finish(null);
      const v = safe(); if (v) finish(v);
    };
    try { obs = new MutationObserver(check); obs.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true }); } catch { /* ignore */ }
    timer = setInterval(check, 100);
  });
}

// Click, and click again if the page wasn't ready yet (React not hydrated → nothing happened).
// (never clicks again once the page has started leaving – no double orders)
let leaving = false;
addEventListener("beforeunload", () => { leaving = true; });
addEventListener("pagehide", () => { leaving = true; });
async function clickUntil(getBtn, left, tries = 8) {
  for (let i = 0; i < tries; i++) {
    if (leaving || moved()) return true;
    const b = getBtn();
    if (!b) return true;                       // button gone = it worked
    b.click();
    if (await find(() => leaving || left(), 250)) return true;   // page changed / moved on
  }
  return leaving || moved();
}

const byText = (re, sel = "button, input[type=submit], a") => () =>
  document.body && [...document.querySelectorAll(sel)].find((e) => {
    const t = (e.innerText || e.value || "").trim();
    return re.test(t) && !e.disabled && e.offsetParent !== null;
  });

// (if the page already moved on while we were looking, say nothing: the next page takes over)
const step = (s, note, done = false) => moved() ? Promise.resolve()
  : chrome.runtime.sendMessage({ type: "job-step", step: s, note, done }).catch(() => {});

async function opts() {
  return chrome.storage.local.get({ clickPayNow: true, upiId: "" });
}

const scriptStart = Date.now();
// the product's own buy button reads "Out of stock" / "Notify me" (ignores the site-wide fixed form)
function gtsMainOos() {
  if (!document.body) return false;
  return [...document.querySelectorAll("button")].some((e) => {
    if (!/^(out of stock|sold out|notify me)$/i.test((e.innerText || "").trim()) || !e.getClientRects().length) return false;
    for (let p = e; p; p = p.parentElement) if (getComputedStyle(p).position === "fixed") return false;
    return !e.closest("[role=dialog]");
  });
}

async function gamestheshop(job) {
  const path = location.pathname;
  if (path.startsWith("/checkout")) {
    const o = await opts();
    if (!o.clickPayNow || job.step === "paynow")
      return step("checkout", "GamesTheShop: checkout open – tap Pay Now and pay.", true);
    const btn = await find(byText(/^pay now$/i));
    if (!btn) return step("checkout", "GamesTheShop: checkout open but 'Pay Now' not found – tap it yourself.", true);
    step("paynow");                  // recorded in parallel – click right away
    // only opens Paytm's payment page; nothing is paid until you approve
    await clickUntil(byText(/^pay now$/i), () => !byText(/^pay now$/i)() || moved());
    return;
  }
  if (path.startsWith("/login")) return step("login", "GamesTheShop: you're not logged in in this browser. Log in once, then it goes straight to checkout.", true);
  if (path.startsWith("/product/") && (job.step === "start" || job.step === "reloaded")) {
    // The product's stock arrives a few seconds after the page appears, and the site has a fixed
    // "Notify Me" form on EVERY page – so wait for BUY NOW, and only call it sold out when the
    // product's own button says so after the page had time to load.
    const buy = byText(/^buy now$/i);
    const btn = await find(() => buy() || (Date.now() - scriptStart > 4000 && gtsMainOos() && "oos"), 25000);
    if (btn && btn !== "oos") {
      step("clicked");               // recorded in parallel – click right away
      await clickUntil(buy, () => moved());
      return;
    }
    if (job.step === "start") {      // stock can lag a moment behind the bot's check: reload once
      await step("reloaded", "GamesTheShop: page shows sold out – reloading once…");
      location.reload();
      return;
    }
    return step("none", `GamesTheShop: ${btn ? "sold out by the time the page loaded" : "BUY NOW button not found"} – check the tab.`, true);
  }
  if (job.step === "start")
    return step("none", "GamesTheShop: opened a page that isn't a product – nothing to click. " +
                        "To test BUY NOW use /extension test <product link>.", true);
}

// Amazon: product page → Buy Now (straight to checkout); fallback Add to Cart → Proceed to Buy.
const visible = (e) => e && !e.disabled && (e.offsetParent !== null || e.getClientRects().length > 0);
const q = (...sels) => () => sels.map((s) => document.querySelector(s)).find(visible) || null;
const AMZ_BUY_NOW = q("#buy-now-button", 'input[name="submit.buy-now"]', "#buyNow", "#gc-buy-box-bn");
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
  const hits = [...document.querySelectorAll(sel)].filter((e) => visible(e) &&
    re.test(((e.innerText || e.value || e.getAttribute("aria-label") || "") + "").trim()));
  // innermost: wrappers often have exactly the same text as the element inside them
  return hits.filter((e) => !hits.some((o) => o !== e && e.contains(o)))
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length)[0] || null;
}

// The UPI QR option on Amazon's payment page ("Scan and Pay with UPI").
// Amazon nests the payment rows deeply, so start from each radio button and grow it to its row
// (the largest box holding only that radio): the radio whose row says "Scan and Pay with" is it.
const RADIO_SELS = ['input[type="radio"]', '[role="radio"]'];
const isRadio = (e) => !!(e && e.matches && RADIO_SELS.some((s) => e.matches(s)));
const radioChecked = (e) => (e.type === "radio" ? e.checked : e.getAttribute("aria-checked") === "true");
function amzUpiQrOption() {
  for (const sel of RADIO_SELS) {
    for (const r of document.querySelectorAll(sel)) {
      let row = r;
      for (let i = 0; i < 15 && row.parentElement && row.parentElement !== document.body; i++) {
        if (row.parentElement.querySelectorAll(sel).length > 1) break;
        row = row.parentElement;
      }
      if (/scan and pay with/i.test(row.innerText || "")) return r;
    }
  }
  return textEl(/^scan and pay with/i);      // no radio found: the text itself (clicked as a fallback)
}
// small box around an element (first ancestor with a bit of text), to tell what it belongs to
function boxText(el) {
  for (let p = el.parentElement, i = 0; p && i < 6; p = p.parentElement, i++)
    if ((p.innerText || "").trim().length > 25) return p.innerText;
  return "";
}
// greyed-out buttons: disabled on the button itself or on any box around it
const enabled = (e) => visible(e) &&
  !(e.closest && e.closest('[aria-disabled="true"], .a-button-disabled, [disabled]'));
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
  const upi = amzUpiQrOption(), use = [...document.querySelectorAll("span, button, input[type=submit], a")]
    .find((e) => visible(e) && /^use this payment method$/i.test(((e.innerText || e.value || "") + "").trim()));
  return `[debug ${location.pathname.slice(0, 40)} | ${window === window.top ? "page" : "frame"} | ` +
         `upi:${!upi ? "none" : isRadio(upi) ? (radioChecked(upi) ? "radio,checked" : "radio,unchecked") : "text-only"} ` +
         `useBtn:${!use ? "none" : enabled(clickable(use)) ? "on" : "grey"} | scanText:${/scan and pay/i.test(document.body.innerText)} ` +
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
  // If "Use this payment method" already looks usable before anything is picked, it can't tell
  // us whether UPI got selected; then only the UPI radio itself counts.
  const usableBefore = !!amzUseThisPayment();
  const ready = () => {
    const r = amzUpiQrOption(), use = amzUseThisPayment();
    if (!use) return null;
    if (isRadio(r)) return radioChecked(r) ? use : null;
    return usableBefore ? null : use;
  };
  // Amazon's script can undo a selection a moment later, so it has to still hold after 300 ms
  const settled = async () => { if (!ready()) return null; await sleep(300); return ready(); };
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = amzUpiQrOption(), t = textEl(/^scan and pay with/i);
    if (!r && !t) return null;
    // radio first; then the row / label / text, in case Amazon listens on those
    const targets = attempt === 0 ? [r]
      : [t && t.closest("label, [role=radio], [data-pmts-component-id], .a-radio, li"), t, r];
    for (const el of targets) {
      if (!el || (isRadio(r) && radioChecked(r))) break;
      realClick(el);
      await sleep(150);
    }
    if (await find(ready, attempt === 0 ? 2500 : 3500)) {
      const use = await settled();
      if (use) return use;
    }
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
  if (!use && !(isRadio(r) && radioChecked(r))) return;          // couldn't select it
  window.top.postMessage({ psnbot: 1, chosen: true }, "*");
  if (use) { realClick(use); window.top.postMessage({ psnbot: 1, pressed: true }, "*"); }
}

const AMZ_STUCK = "Amazon: pressed 'Use this payment method' but the page didn't move on – " +
  "pick 'Scan and Pay with UPI' and press it yourself. ";

// true as soon as test() is true (checked every 100 ms), false after ms
async function waitFor(test, ms) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) {
    try { if (test()) return true; } catch { /* ignore */ }
  }
  return false;
}

// The decline button of a Prime / offers page shown during checkout ("No Thanks"), or null
function amzSkipOffer() {
  if (!/\/(checkout|gp\/buy)\//.test(location.pathname)) return null;
  const no = textEl(/^(no,? thanks?|no thank you|not now|skip( this)?|maybe later|continue without (prime|offers?|subscribing))$/i,
                    "a, button, input[type=submit], span, label");
  if (!no || !(/\/offers/.test(location.pathname) || /prime/i.test(document.body.innerText))) return null;
  const el = clickable(no);
  return enabled(el) ? el : null;
}

// Amazon's own error / notice text on the page (e.g. why Add to Cart didn't work)
function amzAlertText() {
  const el = [...document.querySelectorAll(".a-alert-content, [role=alert], #attach-warning-display, .a-color-error")]
    .find((e) => visible(e) && (e.innerText || "").trim().length > 3);
  return el ? el.innerText.trim().replace(/\s+/g, " ").slice(0, 160) : "";
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
    if (amzSkipOffer()) return "offer";
    if (amzPayWithUpi() && amzQrChosen()) return "pay";
    if (amzUpiQrOption()) return "choose";
    if (frameState.chosen) return "frame";
    if (job.step !== "changepay" && amzChangePayment() && textEl(/^(place your order|pay with|pay now)/i)) return "change";
    return null;
  }, 25000);

  if (what === "offer") {
    // Prime / offers page in the middle of checkout: always decline (never "Continue with Prime")
    const mine = await chrome.runtime.sendMessage({ type: "claim", what: "amzoffer" + location.pathname }).catch(() => true);
    if (!mine) return;
    await step("offer", "Amazon: Prime/offer page – clicking 'No Thanks'…");
    realClick(amzSkipOffer());
    if (await waitFor(() => moved() || !amzSkipOffer(), 5000)) return moved() ? undefined : amazonCheckout({ ...job, step: "offer" });
    // still there: its plain "Next step" button (nothing that joins / starts / tries anything)
    const next = textEl(/^(next step|continue)$/i, "a, button, input[type=submit], span");
    if (next) {
      await step("offer", "Amazon: offer page – clicking 'Next step'…");
      realClick(clickable(next));
      if (await waitFor(() => moved() || !amzSkipOffer(), 5000)) return moved() ? undefined : amazonCheckout({ ...job, step: "offer" });
    }
    return step("checkout", "Amazon: stuck on the Prime/offer page – press 'No Thanks' yourself, then pay. " + amzDiag(), true);
  }

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
    return step("checkout", AMZ_STUCK + amzDiag(), true);
  }
  if (what === "choose") {
    // one copy of this script per page does the choosing (two clicking at once undo each other)
    const mine = await chrome.runtime.sendMessage({ type: "claim", what: "amzpay" + location.pathname }).catch(() => true);
    if (!mine) return;
    await step("choosing", "Amazon: payment page – choosing 'Scan and Pay with UPI'…");
    const use = await amzChooseUpi();
    if (!use) return step("checkout", "Amazon: couldn't select 'Scan and Pay with UPI' – tap it, then 'Use this payment method'. " + amzDiag(), true);
    await step("chosen", "Amazon: UPI QR chosen – clicking 'Use this payment method'…");
    realClick(use);
    // review page: usually a new URL (handled by the main loop); if it updates in place, go on here
    if (await find(() => amzPayWithUpi() && amzQrChosen(), 15000)) return amazonCheckout({ ...job, step: "chosen" });
    return step("checkout", AMZ_STUCK + amzDiag(), true);   // (says nothing if the page moved on)
  }
  if (what === "change") {
    await step("changepay", "Amazon: review page has another payment method – clicking 'Change'…");
    realClick(amzChangePayment());
    if (await find(amzUpiQrOption, 10000)) return amazonCheckout({ ...job, step: "changepay" });
    return;
  }
  return step("checkout", manual + " " + amzDiag(), true);
}

// ---- Amazon e-gift cards (e.g. "PlayStation E-Gift Card"): pick an amount + recipient email ----
const AMZ_DISABLED = '[aria-disabled="true"], .a-button-disabled, [disabled], .a-button-unavailable';

// The recipient email box of a gift card order form (not the site search or sign-in boxes)
function amzGiftEmailBox() {
  return [...document.querySelectorAll('input[type="email"], textarea, input[type="text"], input:not([type])')].find((i) => {
    if (!visible(i) || i.closest("#nav-belt, #navbar, header, form[role=search]")) return false;
    const t = fieldText(i, true);
    return /recipient|e-?mail/i.test(t) && !/search|password|sign.?in|phone|mobile/i.test(t);
  }) || null;
}

// Amount options ("₹1,000", "₹2,000", …): clickable boxes in the gift card form, not prices
function amzGiftAmounts(near) {
  const re = /^(?:₹|rs\.?|inr)?\s*([\d,]{3,7})(?:\.00)?$/i;
  const hits = [...document.querySelectorAll("button, [role=button], [role=radio], label, li, span, a, input[type=submit], input[type=button]")]
    .filter((e) => visible(e) && re.test(((e.innerText || e.value || "") + "").trim()) &&
      e.closest("button, [role=button], [role=radio], label, li, .a-button, input") && !e.closest(AMZ_DISABLED));
  const inner = hits.filter((e) => !hits.some((o) => o !== e && e.contains(o)));
  let opts = inner.map((e) => ({ el: e, value: Number(((e.innerText || e.value) + "").trim().match(re)[1].replace(/,/g, "")) }))
    .filter((a) => a.value >= 100 && a.value <= 100000);
  // only the ones in the same form as the email box (the closest box holding 2+ of them)
  for (let box = near && near.parentElement, i = 0; box && i < 12; box = box.parentElement, i++) {
    const mine = opts.filter((a) => box.contains(a.el));
    if (mine.length >= 2) { opts = mine; break; }
  }
  return opts;
}

function amzGiftForm() {
  const email = amzGiftEmailBox();
  return email ? { email, amounts: amzGiftAmounts(email) } : null;
}

async function amzFillGiftCard(gc) {
  const o = await chrome.storage.local.get({ amzGiftEmail: "", accountName: "" });
  if (!o.amzGiftEmail) {
    await step("none", "Amazon: this e-gift card needs a recipient email – set 'Amazon e-gift card email' in Options. Finish this one in the tab.", true);
    return false;
  }
  let note = "the page's own amount";
  if (gc.amounts.length) {
    const top = gc.amounts.reduce((a, b) => (b.value > a.value ? b : a));
    realClick(clickable(top.el));
    note = `₹${top.value.toLocaleString("en-IN")} (highest of ${gc.amounts.map((a) => a.value).join(", ")})`;
    await sleep(300);
  }
  const box = amzGiftEmailBox() || gc.email;          // the form may have re-drawn after the click
  box.focus();
  setValue(box, o.amzGiftEmail);
  box.dispatchEvent(new Event("blur", { bubbles: true }));
  if (box.value.trim() !== o.amzGiftEmail) {
    await step("none", `Amazon: e-gift card – couldn't type the email into the form. Finish in the tab. ${amzDiag()}`, true);
    return false;
  }
  // "From" name, if the form wants one and it's empty
  const from = [...document.querySelectorAll('input[type="text"], input:not([type])')].find((i) =>
    visible(i) && !i.value && /\bfrom\b|sender/i.test(fieldText(i, false)));
  if (from && o.accountName) setValue(from, o.accountName);
  await step("giftfilled", `Amazon: e-gift card – chose ${note}, recipient ${o.amzGiftEmail}`);
  return true;
}

async function amazon(job) {
  const path = location.pathname;
  if (/\/(checkout|gp\/buy)\//.test(path) || /\/spc\//.test(path)) return amazonCheckout(job);
  if (/\/ap\/signin/.test(path)) return step("login", "Amazon: please log in in this browser once.", true);

  if (/\/(dp|gp\/product|gp\/aw\/d)\//.test(path) && (job.step === "start" || job.step === "giftfilled")) {
    let btn = await find(() => AMZ_BUY_NOW() || AMZ_ADD() || (amazonUnavailable() && "na"), 12000);
    if (!btn || btn === "na")
      return step("none", `Amazon: ${btn ? "this card is currently unavailable (sold out)" : "no Buy Now / Add to Cart button " + amzDiag()} – check the tab.`, true);
    // E-gift cards (amount + recipient email) must be filled in before the buttons work
    const gc = amzGiftForm();
    if (gc) {
      if (!(await amzFillGiftCard(gc))) return;            // says why in the log
      btn = await find(() => AMZ_BUY_NOW() || AMZ_ADD(), 5000);
      if (!btn) return step("none", "Amazon: e-gift card filled in, but no Buy Now / Add to Cart button – finish in the tab. " + amzDiag(), true);
    }
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
    if (go) { await step("proceed"); go.click(); return; }
    // still on the product page and Amazon shows an error: say what it said (without an error the
    // cart page may just be slow to load, and it carries on from there)
    const msg = !leaving && !moved() && amzAlertText();
    if (msg) return step("none", `Amazon: Add to Cart didn't go through – Amazon says: "${msg}". Finish in the tab. ` + amzDiag(), true);
    return;
  }

  if (/\/(gp\/cart|cart)/.test(path) || job.step === "added") {
    const btn = await find(() => AMZ_PROCEED() ||
      (/your amazon cart is empty/i.test(document.body.innerText) && "empty"), 10000);
    if (btn === "empty") {
      const msg = amzAlertText();
      return step("none", "Amazon: the cart is empty – Amazon didn't add the card (sold out, a per-customer limit, " +
                          "or an option like the amount wasn't picked)" + (msg ? `. Amazon says: "${msg}"` : "") + ".", true);
    }
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

// ---- Paytm: pay by debit card (GamesTheShop) ----
// Opens Paytm's card option, picks the saved card ending in your last 4 digits and puts the cursor in
// the CVV box. You type the CVV, press Pay and enter the bank OTP. Card number / CVV are never stored.
const vis = (e) => e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
const clickTarget = (e) => e.closest("li, a, button, label, [role=button], [role=tab], [role=radio]") || e;
function textBox(re, maxLen = 60) {
  return [...document.querySelectorAll("li, a, button, label, span, div, p, h3, h4, [role=tab]")]
    .filter((e) => vis(e) && (e.innerText || "").trim().length <= maxLen && re.test((e.innerText || "").trim()))
    .sort((a, b) => a.innerText.length - b.innerText.length)[0] || null;
}
// What a field is called. Paytm uses floating labels, not placeholders, so look at the label
// elements and the text right next to the box as well.
function fieldText(i, loose) {
  const bits = [i.placeholder, i.name, i.id, i.getAttribute("aria-label"), i.autocomplete, i.getAttribute("data-testid")];
  if (i.id) { try { const l = document.querySelector(`label[for="${CSS.escape(i.id)}"]`); if (l) bits.push(l.innerText); } catch { /* ignore */ } }
  const wrap = i.closest("label"); if (wrap) bits.push(wrap.innerText);
  const by = i.getAttribute("aria-labelledby");
  if (by) by.split(/\s+/).forEach((id) => { const e = document.getElementById(id); if (e) bits.push(e.innerText); });
  if (loose) {                                  // floating label / text in the same little box
    const p = i.parentElement;
    if (p) {
      bits.push([...p.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" "));
      bits.push(...[...p.querySelectorAll("label, span, div, p")].slice(0, 4).map((e) => e.innerText || ""));
    }
    for (let s = i.previousElementSibling, k = 0; s && k < 2; s = s.previousElementSibling, k++) bits.push(s.innerText || "");
  }
  return bits.filter(Boolean).join(" ").slice(0, 300);
}
// tight match first (the box's own name), then loose (nearby label text)
const inputLike = (re) => {
  const boxes = [...document.querySelectorAll("input")].filter((i) => vis(i) && !i.disabled && i.type !== "hidden");
  return boxes.find((i) => re.test(fieldText(i, false))) || boxes.find((i) => re.test(fieldText(i, true)));
};
function frameInfo() {
  const boxes = [...document.querySelectorAll("input")].filter((i) => vis(i) && i.type !== "hidden")
    .map((i) => ({ as: (fieldText(i, true).match(/card ?number|expiry|validity|cvv|cvc|name/i) || ["?"])[0],
                   max: i.maxLength > 0 ? i.maxLength : undefined, type: i.type }));
  const frames = [...document.querySelectorAll("iframe")].filter(vis).length;
  return `[frame ${window === window.top ? "main" : "inner"} ${location.hostname}${location.pathname.slice(0, 18)}`
       + ` | options: ${JSON.stringify(payOptions())} | boxes: ${JSON.stringify(boxes)} | iframes: ${frames}]`;
}

function payOptions() {
  return [...document.querySelectorAll("li, button, [role=tab], a")].filter(vis)
    .map((e) => (e.innerText || "").trim().split("\n")[0]).filter((t) => t && t.length < 30).slice(0, 10);
}

// Type into a field the way a person would, one character at a time, so Paytm's formatted
// boxes (spaces after every 4 digits, the "/" in MM/YY) accept it. Returns true if the digits landed.
async function typeInto(el, text) {
  const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value").set;
  el.focus();
  set.call(el, "");
  el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
  for (const ch of text) {
    el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, bubbles: true }));
    set.call(el, el.value + ch);                 // read back: the box may reformat as it goes
    el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, bubbles: true }));
    await sleep(10);
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
  const got = (el.value || "").replace(/\D/g, "");
  if (got === text) return true;
  setValue(el, text);                            // last resort: set it in one go
  return (el.value || "").replace(/\D/g, "") === text;
}

async function pickSelect(el, value) {
  const want = String(value);
  const opt = [...el.options].find((o) => {
    const v = (o.value || "").replace(/\D/g, ""), t = (o.text || "").replace(/\D/g, "");
    return v === want || t === want || v.slice(-2) === want.slice(-2) || t.slice(-2) === want.slice(-2);
  });
  if (!opt) return false;
  el.value = opt.value;
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}

// No usable label text? Work it out from the shape of the form: a 16-19 character box is the
// card number, 5-7 is the expiry, 3-4 (or a password box) is the CVV; otherwise document order.
function guessCardFields() {
  const ins = [...document.querySelectorAll("input")].filter((i) => vis(i) && !i.disabled && i.type !== "hidden"
    && !/search|coupon|promo|otp|email|mobile|phone/i.test(fieldText(i, true)));
  if (ins.length < 2 || ins.length > 4) return {};
  const out = {}, left = [...ins];
  const take = (test) => { const i = left.findIndex(test); return i < 0 ? null : left.splice(i, 1)[0]; };
  out.cvv = take((i) => i.type === "password" || [3, 4].includes(i.maxLength));
  out.num = take((i) => i.maxLength >= 16 && i.maxLength <= 23);
  out.exp = take((i) => [5, 6, 7].includes(i.maxLength));
  if (!out.num) out.num = take(() => true);                 // first of what's left
  if (!out.exp) out.exp = take(() => true);
  if (!out.cvv) out.cvv = take(() => true);
  return out;
}

// Fill the card form from the details saved in Options. Never presses Pay.
async function fillCardForm(card) {
  const filled = [];
  const g = guessCardFields();
  const num = inputLike(/card ?number|cardnum|cc-number|enter card|^number$/i) || g.num;
  if (num && card.number && await typeInto(num, card.number)) filled.push("number");
  const name = inputLike(/name on card|card ?holder|holder ?name|cardname/i) || g.name;
  if (name && card.name) { await typeInto(name, card.name); filled.push("name"); }
  if (card.expiry && /^\d{4}$/.test(card.expiry)) {
    const mm = card.expiry.slice(0, 2), yy = card.expiry.slice(2);
    const one = inputLike(/expiry|validity|exp ?date|valid ?thru|mm\s*\/?\s*yy/i) || g.exp;
    const sels = [...document.querySelectorAll("select")].filter(vis);
    const mSel = sels.find((s) => /month|mm|exp/i.test(s.name + s.id + (s.getAttribute("aria-label") || "")));
    const ySel = sels.find((s) => /year|yy|yyyy/i.test(s.name + s.id + (s.getAttribute("aria-label") || "")));
    const mIn = inputLike(/^(exp)?m{2}$|month/i), yIn = inputLike(/^(exp)?y{2,4}$|year/i);
    if (one && await typeInto(one, mm + yy)) filled.push("expiry");
    else if (mSel && ySel && await pickSelect(mSel, mm) && await pickSelect(ySel, yy)) filled.push("expiry");
    else if (mIn && yIn) { await typeInto(mIn, mm); await typeInto(yIn, yy); filled.push("expiry"); }
  }
  const cvv = inputLike(/cvv|cvc|security code/i) || g.cvv;
  if (cvv && card.cvv) { await typeInto(cvv, card.cvv); filled.push("CVV"); }
  else if (cvv) { cvv.focus(); highlight(cvv.closest("form, li, section, div") || cvv); }
  else if (num) { num.focus(); }
  return { filled, cvvLeft: !!(cvv && !card.cvv) };
}

let paidPressed = false;

// Paytm card payment. Order matters: if a card is saved in Options we always try to FILL the form
// first (text-matched boxes, else worked out from the form's shape) – a page where only a CVV box
// can be identified must not be mistaken for "a saved card is selected".
async function paytmCard(job, last4) {
  // e.g. "Prepaid, Debit and Credit Cards", "Credit / Debit Card", "Cards"
  const CARD_OPT = /(?:prepaid|debit|credit)[\s,/&a-z]{0,24}cards?\b|^cards?$/i;
  const tab = await find(() => [...document.querySelectorAll("div, li, button, a, span, p, h3, h4, [role=button], [role=tab]")]
    .filter((e) => vis(e) && CARD_OPT.test((e.innerText || "").trim()) && (e.innerText || "").trim().length < 70
                   && !/upi|net ?banking|emi|wallet/i.test((e.innerText || "").trim()))
    .sort((a, b) => a.innerText.length - b.innerText.length)[0] || null, 8000);
  if (!tab) return false;
  await step("card", "Paytm: choosing Debit Card…");
  chrome.runtime.sendMessage({ type: "claim", what: "cardflow" }).catch(() => {});
  realClick(clickTarget(tab));

  const card = await chrome.storage.local.get({ cardNumber: "", cardExpiry: "", cardName: "", cardCvv: "", cardAutoPay: true });
  const shown = card.cardNumber.slice(-4) || last4;

  // a saved card at Paytm ending in your last 4 digits -> clicking it shows just a CVV box
  if (last4 && !card.cardNumber) {
    const saved = await find(() => [...document.querySelectorAll("li, label, div, button")]
      .filter((e) => vis(e) && new RegExp(`(?:[x•*\\s-]|^)${last4}\\b`, "i").test(e.innerText || "") && (e.innerText || "").length < 160)
      .sort((a, b) => a.innerText.length - b.innerText.length)[0] || null, 5000);
    if (saved) realClick(clickTarget(saved));
  }
  // some pages need "Add new card" before the form appears
  if (card.cardNumber) {
    const add = textBox(/^(\+ ?)?(add (a )?new card|use (another|a different) card|enter card details|new card)$/i, 40);
    if (add) { realClick(clickTarget(add)); await sleep(200); }
  }

  // wait until this frame actually has a card form (named box, or one we can work out by shape)
  const ready = await find(() => inputLike(/card ?number|cardnum|cc-number|enter card|^number$/i)
    || guessCardFields().num || inputLike(/cvv|cvc|security code/i), 8000);
  if (!ready) return false;

  let note, readyState;
  const r = card.cardNumber ? await fillCardForm({ number: card.cardNumber, expiry: card.cardExpiry,
                                                   name: card.cardName, cvv: card.cardCvv })
                            : { filled: [], cvvLeft: false };
  if (r.filled.includes("number")) {
    readyState = r.cvvLeft ? "cvv" : "pay";
    note = `Paytm: card ••${shown} filled (${r.filled.join(", ")}) – `
         + (r.cvvLeft ? "type the CVV, press Pay, then the OTP." : "press Pay, then enter the OTP.");
    // everything in, CVV included -> press Pay. The bank's OTP page comes next; nothing is
    // charged until you enter that OTP. One frame only, once only.
    if (!r.cvvLeft && card.cardAutoPay && r.filled.includes("CVV") && !paidPressed
        && await chrome.runtime.sendMessage({ type: "claim", what: "pay" }).catch(() => false)) {
      const payBtn = await find(() => [...document.querySelectorAll("button, input[type=submit], [role=button]")]
        .find((e) => vis(e) && !e.disabled && /^(pay securely|pay now|pay)\b/i.test((e.innerText || e.value || "").trim())
                     && !/upi|wallet|later/i.test((e.innerText || e.value || "").trim())), 6000);
      if (payBtn) {
        paidPressed = true;
        const label = (payBtn.innerText || payBtn.value || "Pay").trim().slice(0, 24);
        const sentP = await chrome.runtime.sendMessage({ type: "qr", card: true, cardReady: "otp", ...paytmDetails() }).catch(() => false);
        await step("otp", `Paytm: card ••${shown} filled, pressed "${label}" – enter the bank OTP in the browser now.`
                          + (sentP ? " (Telegram notified)" : ""), true);
        watchPayment(false);                 // the result page is reported to Telegram
        realClick(payBtn);                   // -> bank OTP page; nothing is charged until you enter it
        return true;
      }
    }
  } else {
    // nothing could be filled: focus whatever box is there so you can type
    const cvvBox = inputLike(/cvv|cvc|security code/i) || guessCardFields().cvv;
    const numBox = inputLike(/card ?number|cardnum|cc-number|enter card|^number$/i) || guessCardFields().num;
    const box = (card.cardNumber ? numBox || cvvBox : cvvBox || numBox);
    if (box) { box.focus(); highlight(box.closest("form, li, section, div") || box); }
    const onlyCvv = box && box === cvvBox && !card.cardNumber;
    readyState = onlyCvv ? "cvv" : "form";
    note = onlyCvv ? `Paytm: card ending ${shown || "?"} selected – type the CVV, press Pay, then enter the OTP.`
                   : `Paytm: card form open but the details couldn't be filled – enter them yourself. ${frameInfo()}`;
  }
  const sent = await chrome.runtime.sendMessage({ type: "qr", card: true, cardReady: readyState, ...paytmDetails() }).catch(() => false);
  await step("card", note + (sent ? " (Telegram notified)" : ""), true);
  watchPayment(false);
  return true;
}

const otpPage = () => document.body && /one[- ]time password|\botp\b|3d ?secure|verified by|authenticat/i.test(document.body.innerText || "")
  && [...document.querySelectorAll("input")].some((i) => vis(i) && /otp|code|password/i.test(fieldText(i, true)));

async function paytm(job) {
  const o = await opts();
  if (otpPage()) {                             // payment already started: just watch for the result
    watchPayment(true);
    return step("otp", "Paytm: OTP page open – enter the OTP from your bank to finish.", true);
  }
  const pay = await chrome.storage.local.get({ gtsPayWith: "upi", cardLast4: "" });
  if (pay.gtsPayWith === "card") {             // runs in the top page and in Paytm's iframes
    if (await paytmCard(job, (pay.cardLast4 || "").replace(/\D/g, "").slice(-4))) return;
    if (await chrome.runtime.sendMessage({ type: "claimed?", what: "cardflow" }).catch(() => false))
      return;                                  // another frame is handling the card: stay quiet
    if (window !== window.top) return;          // an iframe with nothing to fill: stay quiet
    await log0(`Paytm: couldn't open the debit card option – showing the QR instead. ${frameInfo()}`);
  }
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
    if (!document.body) return null;
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

// true once the biggest image in the QR box has loaded / the canvas has something drawn on it
function qrDrawn(box, maxMs = 1500) {
  const ready = () => {
    const el = [...box.querySelectorAll("canvas, img, svg")].sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
    if (!el) return false;
    if (el.tagName === "IMG") return el.complete && el.naturalWidth > 0;
    if (el.tagName === "CANVAS") {
      try { const d = el.getContext("2d").getImageData(0, 0, el.width, el.height).data;
            for (let i = 3; i < d.length; i += 400) if (d[i]) return true; return false; } catch { return true; }
    }
    return true;
  };
  return find(() => ready() || null, maxMs).then(() => sleep(120));    // one frame of margin
}

async function sendQrToTelegram(box) {
  const o = await chrome.storage.local.get({ tgQr: true, tgToken: "", tgChatId: "" });
  if (!o.tgQr || !o.tgToken || !o.tgChatId) return false;
  const { qrMode = "dm" } = await chrome.storage.local.get("qrMode");
  await qrDrawn(box);                      // as soon as the QR image is actually there
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

function log0(note) { return chrome.runtime.sendMessage({ type: "job-step", step: "card-fallback", note }).catch(() => {}); }

async function getJob() {
  // the background saves the job right after opening the tab; on a fast page we can ask first
  for (let i = 0; i < 30; i++) {
    const job = await chrome.runtime.sendMessage({ type: "job?" }).catch(() => null);
    if (job) return job;
    await sleep(100);
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
  const isPaytm = /(^|\.)paytm(payments)?\.(in|com)$/.test(host);
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
    while (!moved() && Date.now() < end) await sleep(50);
    if (!moved()) return;
    detectGtsAccount().catch(() => {});
    job = await chrome.runtime.sendMessage({ type: "job?" }).catch(() => null);
    if (!job) return;                                // finished (or expired)
  }
})();

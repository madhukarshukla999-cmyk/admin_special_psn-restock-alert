// Runs on GamesTheShop and Amazon, but only acts on tabs the extension itself opened
// for an alert (asks the background first). Clicks up to the checkout page, then stops.
// It never clicks "Place order" / "Pay".

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function find(test, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
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

const step = (s, note, done = false) => chrome.runtime.sendMessage({ type: "job-step", step: s, note, done });

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
  }
}

async function amazon(job) {
  const path = location.pathname;
  if (/\/(checkout|gp\/buy)\//.test(path) || /\/spc\//.test(path))
    return step("checkout", "Amazon: checkout open – pick your card and enter OTP.", true);
  if (/\/ap\/signin/.test(path)) return step("login", "Amazon: please log in in this browser once.", true);
  if (path.includes("/gp/aws/cart/add")) {
    const btn = await find(() => document.querySelector('input[name="add"]') || byText(/^(continue|add to cart)$/i)());
    if (btn) { await step("added"); btn.click(); return; }
  }
  if (/\/(gp\/cart|cart)/.test(path) || job.step === "added") {
    const btn = await find(() => document.querySelector('input[name="proceedToRetailCheckout"]') ||
                                 byText(/proceed to (buy|checkout)/i)(), 10000);
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
    return step("qr", "Paytm: QR is ready – scan it with your UPI app and enter your PIN.", true);
  }
  return step("pay", "Paytm: payment page open – choose how to pay.", true);
}

async function handle() {
  const job = await chrome.runtime.sendMessage({ type: "job?" });
  if (!job) return false;                            // not a tab we opened (or job finished)
  const host = location.hostname;
  if (host === "www.gamestheshop.com") await gamestheshop(job);
  else if (host === "www.amazon.in") await amazon(job);
  else if (host.endsWith("paytmpayments.com") || host.endsWith("paytm.in")) await paytm(job);
  return true;
}

// Sites like GamesTheShop are single-page apps: BUY NOW switches to /checkout without
// reloading, so this script would otherwise never see the checkout page. Watch the URL and
// run again whenever it changes (for up to 5 minutes, only in tabs we opened).
(async () => {
  let last = location.href, busy = false;     // record the URL *before* clicking: BUY NOW changes it
  if (!(await handle())) return;
  const until = Date.now() + 5 * 60 * 1000;
  const timer = setInterval(async () => {
    if (Date.now() > until) return clearInterval(timer);
    if (location.href === last || busy) return;
    last = location.href;
    busy = true;
    try { if (!(await handle())) clearInterval(timer); } finally { busy = false; }
  }, 300);
})();

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
  const isPaytm = host.endsWith("paytmpayments.com") || host.endsWith("paytm.in");
  if (window !== window.top && !isPaytm) return;    // store pages: top frame only (no double clicks)
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
    job = await chrome.runtime.sendMessage({ type: "job?" }).catch(() => null);
    if (!job) return;                                // finished (or expired)
  }
})();

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

async function gamestheshop(job) {
  const path = location.pathname;
  if (path.startsWith("/checkout")) return step("checkout", "GamesTheShop: checkout open – choose payment and enter OTP.", true);
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

(async () => {
  const job = await chrome.runtime.sendMessage({ type: "job?" });
  if (!job) return;                                  // not a tab we opened: do nothing
  if (location.hostname === "www.gamestheshop.com") await gamestheshop(job);
  else if (location.hostname === "www.amazon.in") await amazon(job);
})();

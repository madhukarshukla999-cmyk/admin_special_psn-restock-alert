// GTSbuy (server order) → Paytm's own payment page in this tab. A POST form, like Paytm's
// checkout does, so after paying Paytm sends the browser back to GamesTheShop, which records it.
(async () => {
  const id = new URLSearchParams(location.search).get("p") || "";
  const k = "gtspay_" + id;
  const d = (await chrome.storage.session.get(k))[k];
  await chrome.storage.session.remove(k);
  if (!d) { document.body.textContent = "This payment link has expired."; return; }
  const host = "https://secure.paytmpayments.com";
  const f = document.createElement("form");
  f.method = "POST";
  f.action = `${host}/theia/api/v1/showPaymentPage?mid=${encodeURIComponent(d.mid)}&orderId=${encodeURIComponent(d.orderId)}`;
  for (const [n, v] of Object.entries({ mid: d.mid, orderId: d.orderId, txnToken: d.txnToken })) {
    const i = document.createElement("input"); i.type = "hidden"; i.name = n; i.value = v; f.appendChild(i);
  }
  document.body.appendChild(f);
  f.submit();
})();

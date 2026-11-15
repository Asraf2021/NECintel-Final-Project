/* Ana səhifə: rol seçimi və sistem vəziyyəti */
(function () {
  const { ICON, t } = window.UCE;
  document.getElementById("brandMark").innerHTML = ICON.shield;
  document.getElementById("icoCitizen").innerHTML = ICON.signal;
  document.getElementById("icoDispatch").innerHTML = ICON.users;

  let last = null;
  function render() {
    const dot = document.getElementById("hDot");
    const text = document.getElementById("hText");
    const routing = document.getElementById("hRouting");
    if (!last) {
      dot.className = "dot is-off";
      text.textContent = t("conn.offline");
      routing.textContent = "";
      return;
    }
    dot.className = "dot is-live";
    text.textContent = t("conn.live");
    routing.textContent = last.routing.degraded ? t("d.routing.fallback") : t("d.routing.external");
  }
  async function check() {
    try {
      const res = await fetch("/health");
      last = res.ok ? await res.json() : null;
    } catch (e) { last = null; }
    render();
  }
  window.I18N.onChange(render);
  check();
  setInterval(check, 5000);
})();

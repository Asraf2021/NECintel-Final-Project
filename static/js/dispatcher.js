/* Modul 2 — Fövqəladə Hallar Dispetçer Paneli və Analitika */
(function () {
  const { fcText, nm, api, esc, connectStream, fmtDist, fmtNum, timeAgo, debounce, levelColor, LEVEL_COLOR, ICON, TYPE_ICON,
    createMap, shelterIcon, toast, t } = window.UCE;
  const $ = (id) => document.getElementById(id);
  const RISK_TAG = { low: "green", medium: "blue", high: "amber", critical: "red" };
  const BRIGADE_TAG = { available: "green", dispatched: "amber", on_scene: "violet", busy: "", offline: "" };

  const state = { verifs: [], token: null, user: null, data: null, filter: "active", conn: "reconnect", stream: null, roadsDrawn: false, fitted: false };
  try { state.token = sessionStorage.getItem("uce_token"); } catch (e) { /* ignore */ }
  let map, layers = {};

  // Rayon adı seçilmiş dildə (zonalar icmaldan gəlir)
  function zoneName(id, fallback) {
    const z = state.data && state.data.zones.find((x) => x.district_id === id);
    return z ? nm(z) : (fallback || id || "");
  }
  function brigadeById(id) { return state.data ? state.data.brigades.find((b) => b.id === id) : null; }
  function shelterById(id) { return state.data ? state.data.shelters.find((x) => x.id === id) : null; }

  const can = (perm) => !!state.user && (state.user.permissions.includes("*") || state.user.permissions.includes(perm));

  async function call(path, opts) {
    opts = Object.assign({ token: state.token }, opts || {});
    try {
      return await api(path, opts);
    } catch (e) {
      if (e.status === 401) { logout(); }
      else if (e.status === 403) toast(t("d.toast.forbidden"), "error");
      else if (e.status === 409 && e.message === "no_brigade_available") toast(t("d.toast.no_brigade"), "error");
      else toast(t("common.error"), "error");
      throw e;
    }
  }

  // ---------- Giriş ----------
  function showLogin() {
    $("login").hidden = false; $("main").hidden = true; $("simMenu").hidden = true; $("logoutBtn").hidden = true;
    $("alertStrip").innerHTML = "";
    setTimeout(() => $("lUser").focus(), 50);
  }

  async function doLogin(e) {
    e.preventDefault();
    $("loginErr").textContent = "";
    try {
      const res = await api("/auth/login", { method: "POST", body: { username: $("lUser").value.trim(), password: $("lPass").value } });
      state.token = res.token;
      try { sessionStorage.setItem("uce_token", res.token); } catch (err) { /* ignore */ }
      $("lPass").value = "";
      await enter();
    } catch (err) {
      $("loginErr").textContent = t(err.status === 429 ? "login.rate" : "login.fail");
    }
  }

  function logout() {
    state.token = null; state.user = null;
    try { sessionStorage.removeItem("uce_token"); } catch (e) { /* ignore */ }
    if (state.stream) { state.stream.close(); state.stream = null; }
    showLogin();
  }

  async function enter() {
    try { state.user = await api("/auth/me", { token: state.token }); } catch (e) { logout(); return; }
    $("login").hidden = true; $("main").hidden = false;
    $("logoutBtn").hidden = false;
    $("userName").textContent = state.user.display_name;
    $("simMenu").hidden = !can("demo:run");
    if (!map) initMap();
    setTimeout(() => map.invalidateSize(), 50);
    await refresh();
    if (state.stream) state.stream.close();
    state.stream = connectStream("/api/v1/dispatch/stream?token=" + encodeURIComponent(state.token), onEvent,
      (s) => { state.conn = s; renderConn(); if (s === "live") refreshSoon(); });
  }

  // ---------- Məlumat ----------
  let inflight = false, again = false;
  async function refresh() {
    if (!state.token) return;
    if (inflight) { again = true; return; }
    inflight = true;
    try {
      state.data = await call("/dispatch/overview");
      if (can("verification:manage")) {
        try { state.verifs = (await call("/dispatch/verifications")).verifications; } catch (e) { state.verifs = []; }
      }
      render();
    } catch (e) { /* call() xəbər verir */ } finally {
      inflight = false;
      if (again) { again = false; refresh(); }
    }
  }
  const refreshSoon = debounce(refresh, 250);

  function onEvent(name, data) {
    if (name === "alert_new") {
      beep();
      toast(t("d.alert.headline") + ": " + alertText(data.alert), "error");
    }
    if (name === "reroute_batch" && data.count) toast(t("d.toast.rerouted", { n: data.count }));
    if (name === "sos_priority" && data.sos) {
      beep(); setTimeout(beep, 500);
      toast(t("d.toast.priority", { type: t("sos.type." + data.sos.type), zone: zoneName(data.sos.district_id, data.sos.district_name) }), "error");
    }
    refreshSoon();
  }

  // Səs siqnalı (Web Audio, fayl tələb etmir)
  function beep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.22].forEach((offset) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = "square"; o.frequency.value = 880;
        g.gain.setValueAtTime(0.06, ctx.currentTime + offset);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.18);
        o.connect(g); g.connect(ctx.destination);
        o.start(ctx.currentTime + offset); o.stop(ctx.currentTime + offset + 0.2);
      });
    } catch (e) { /* səs bloklana bilər */ }
  }

  // ---------- Xəritə ----------
  function initMap() {
    map = createMap("map", [40.4, 49.875], 12, true);
    ["roads", "closures", "zones", "shelters", "brigades", "sos"].forEach((k) => { layers[k] = L.layerGroup().addTo(map); });
    window.addEventListener("resize", debounce(() => map.invalidateSize(), 150));
  }

  async function drawRoads() {
    if (state.roadsDrawn) return;
    try {
      const data = await api("/roads");
      layers.roads.clearLayers();
      data.edges.forEach((e) => {
        const line = L.polyline(e.points, { color: "#4C6A8C", weight: 4, opacity: .7 });
        line.bindTooltip(esc(e.name), { sticky: true });
        if (can("road:manage")) {
          line.on("click", () => {
            L.popup().setLatLng(line.getCenter()).setContent('<b>' + esc(e.name) + '</b><br><button class="btn btn-sm btn-danger" type="button" data-close-road="' + esc(e.id) + '">' + ICON.road + " " + esc(t("d.roads.close")) + "</button>").openOn(map);
          });
        }
        line.addTo(layers.roads);
      });
      state.roadsDrawn = true;
    } catch (e) { /* yenidən cəhd ediləcək */ }
  }

  function drawMap() {
    const d = state.data;
    drawRoads();
    layers.closures.clearLayers();
    d.closures.forEach((c) => {
      L.polyline(c.points, { color: "#FF5A5F", weight: 6, dashArray: "8 7" })
        .bindTooltip(esc(c.name) + " — " + esc(t("d.roads.source." + c.source)), { sticky: true }).addTo(layers.closures);
    });

    layers.zones.clearLayers();
    const pendingZones = new Set(d.alerts.filter((a) => a.status === "pending" && a.kind === "sos_cluster").map((a) => a.district_id));
    d.alerts.filter((a) => a.status === "pending").forEach((a) => {
      if (a.lat == null) return;
      L.circle([a.lat, a.lon], { radius: d.thresholds.cluster_radius_m, color: "#FF5A5F", weight: 2, fillColor: "#E0282E", fillOpacity: .15, dashArray: "6 6", interactive: false }).addTo(layers.zones);
    });
    d.zones.forEach((z) => {
      if (z.risk === "low" || pendingZones.has(z.district_id)) return;
      const color = z.risk === "critical" ? "#E0282E" : z.risk === "high" ? "#F2A20C" : "#2F6FED";
      L.circle([z.lat, z.lon], { radius: 700, color, weight: 1, fillColor: color, fillOpacity: .08, interactive: false }).addTo(layers.zones);
    });

    layers.shelters.clearLayers();
    d.shelters.forEach((s) => {
      const html = "<b>" + esc(nm(s)) + "</b><br>" + esc(t("type." + s.type)) + " · " + esc(t("d.shelter." + s.status)) +
        (s.accessible ? " · " + esc(t("legend.accessible")) : "") +
        "<br>" + esc(t("plan.occupancy")) + ": <b>" + s.occupancy_pct + "%</b> (" + fmtNum(s.occupancy) + " / " + fmtNum(s.capacity) + ")";
      L.marker([s.lat, s.lon], { icon: shelterIcon(s, false) }).bindPopup(html).addTo(layers.shelters);
    });

    layers.brigades.clearLayers();
    d.brigades.forEach((b) => {
      const icon = L.divIcon({ className: "", html: '<div class="mk-brigade is-' + esc(b.status) + '">' + ICON.brigade + "</div>", iconSize: [28, 28], iconAnchor: [14, 14] });
      L.marker([b.lat, b.lon], { icon, zIndexOffset: 300 }).bindTooltip(esc(nm(b)) + " · " + esc(t("d.brigade." + b.status))).addTo(layers.brigades);
      if (b.target_lat != null && b.status !== "available") {
        L.polyline([[b.lat, b.lon], [b.target_lat, b.target_lon]], { color: "#F2A20C", weight: 2, dashArray: "4 6", interactive: false }).addTo(layers.brigades);
      }
    });

    layers.sos.clearLayers();
    d.sos.filter((s) => s.status === "active" || s.status === "assigned").forEach((s) => {
      const prio = s.priority === 1;
      const icon = L.divIcon({ className: "", html: '<div class="mk-sos' + (s.status === "assigned" ? " is-assigned" : "") + (prio ? " is-priority" : "") + '">' +
        (prio ? ICON.wheelchair : ICON[s.type]) + "</div>", iconSize: [30, 30], iconAnchor: [15, 15] });
      L.marker([s.lat, s.lon], { icon, zIndexOffset: prio ? 900 : 800 })
        .bindPopup((prio ? '<span class="tag violet">' + esc(t("d.sos.priority")) + "</span><br>" : "") + "<b>SOS #" + s.id + " · " + esc(t("sos.type." + s.type)) + "</b><br>" + esc(zoneName(s.district_id, s.district_name)) + " · " + esc(timeAgo(s.created_at)) +
          (s.note ? "<br>" + esc(s.note) : "")).addTo(layers.sos);
    });

    if (!state.fitted) {
      const pts = d.shelters.map((s) => [s.lat, s.lon]);
      if (pts.length) map.fitBounds(pts, { padding: [30, 30] });
      state.fitted = true;
    }
  }

  // ---------- Render ----------
  function render() {
    if (!state.data) return;
    renderAlertStrip(); renderKpis(); renderSos(); renderAlerts(); renderBrigades(); renderZones(); renderShelters();
    renderCharts(); renderRoads(); renderVerifs(); renderRouting(); drawMap();
  }

  function alertText(a) {
    const det = a.details || {};
    if (a.kind === "shelter_overflow") return t("d.alert.overflow", { name: nm({ name: det.shelter_name, name_en: det.shelter_name_en }), pct: det.occupancy_pct });
    const n = det.trigger === "radius" ? det.radius_count : det.zone_count;
    return t(det.trigger === "radius" ? "d.alert.cluster_radius" : "d.alert.cluster", { zone: zoneName(a.district_id, a.district_name), n });
  }

  function renderAlertStrip() {
    const pending = state.data.alerts.filter((a) => a.status === "pending");
    if (!pending.length) { $("alertStrip").innerHTML = ""; return; }
    const a = pending[0];
    const more = pending.length > 1 ? ' <span class="tag red">+' + (pending.length - 1) + "</span>" : "";
    const decide = can("alert:decide");
    $("alertStrip").innerHTML = '<div class="alert-banner" role="alert"><span class="a-ico">' + ICON.alert + '</span><div><div class="a-head">' +
      esc(t("d.alert.headline")) + more + '</div><div class="a-act">' + esc(t("d.alert.action")) + '</div><div class="a-what">' + esc(alertText(a)) +
      '</div></div><div class="a-btns">' + (decide ? '<button class="btn btn-confirm" type="button" data-confirm="' + a.id + '">' + ICON.check + "<span>" + esc(t("d.alert.confirm")) +
      '</span></button><button class="btn btn-dismiss" type="button" data-dismiss="' + a.id + '">' + esc(t("d.alert.dismiss")) + "</button>" : "") + "</div></div>";
  }

  function kpi(icon, label, value, sub, color, alarm, bar) {
    return '<div class="kpi' + (alarm ? " is-alarm" : "") + '" style="--k:' + (color || "inherit") + '"><div class="kpi-top"><span>' + esc(label) + "</span>" + icon +
      '</div><div class="kpi-val">' + esc(value) + '</div><div class="kpi-sub">' + esc(sub) + "</div>" + (bar || "") + "</div>";
  }

  function renderKpis() {
    const k = state.data.kpi;
    const occColor = k.occupancy_pct >= 90 ? "#FF5A5F" : k.occupancy_pct >= 70 ? "#F2A20C" : "#6EE7B7";
    const occBar = '<div class="bar" style="--c:' + occColor + '"><i style="width:' + Math.min(100, k.occupancy_pct) + '%"></i></div>';
    $("kpis").innerHTML =
      kpi(ICON.signal, t("d.kpi.sos"), k.sos_active, t("d.kpi.sos_sub", { n: k.sos_window }), k.sos_active ? "#FF5A5F" : null) +
      kpi(ICON.wheelchair, t("d.kpi.priority"), k.sos_priority, t("d.kpi.priority_sub"), k.sos_priority ? "#C9B3FF" : null, k.sos_priority > 0) +
      kpi(ICON.alert, t("d.kpi.alerts"), k.alerts_pending, t("d.kpi.alerts_sub"), k.alerts_pending ? "#FF5A5F" : null, k.alerts_pending > 0) +
      kpi(ICON.users, t("d.kpi.occupancy"), k.occupancy_pct + "%", t("d.kpi.occupancy_sub", { occ: fmtNum(k.total_occupancy), cap: fmtNum(k.total_capacity) }), occColor, false, occBar) +
      kpi(ICON.shelter, t("d.kpi.shelters"), k.shelters_open, t("d.kpi.shelters_sub", { full: k.shelters_full, res: k.shelters_reserve }), "#9EC0FF") +
      kpi(ICON.brigade, t("d.kpi.brigades"), k.brigades_available, t("d.kpi.brigades_sub", { n: k.brigades_total }), k.brigades_available ? "#6EE7B7" : "#FF5A5F") +
      kpi(ICON.road, t("d.kpi.roads"), k.roads_closed, t("d.kpi.roads_sub"), k.roads_closed ? "#FFD08A" : null) +
      kpi(ICON.check, t("d.kpi.safe"), k.citizens_safe, t("d.kpi.safe_sub"), "#6EE7B7");
  }

  function renderSos() {
    let list = state.data.sos;
    if (state.filter === "active") list = list.filter((s) => s.status === "active" || s.status === "assigned");
    if (!list.length) { $("sosList").innerHTML = '<div class="d-empty">' + esc(t("d.sos.empty")) + "</div>"; return; }
    const manage = can("sos:manage");
    const brigades = Object.fromEntries(state.data.brigades.map((b) => [b.id, nm(b)]));
    $("sosList").innerHTML = list.slice(0, 80).map((s) => {
      const done = s.status === "resolved" || s.status === "safe";
      const prio = s.priority === 1;
      const cls = (done ? "is-done" : "is-" + s.status) + (prio && !done ? " is-priority" : "");
      const tag = s.status === "active" ? "red" : s.status === "assigned" ? "amber" : "green";
      let btns = "";
      if (manage && !done) {
        btns = '<div class="sos-btns">' + (s.status === "active" ? '<button class="btn-xs pri" type="button" data-assign="' + s.id + '">' + esc(t("d.sos.assign")) + "</button>" : "") +
          '<button class="btn-xs" type="button" data-resolve="' + s.id + '">' + esc(t("d.sos.resolve")) + "</button></div>";
      }
      const brig = s.brigade_id && brigades[s.brigade_id] ? " · " + esc(brigades[s.brigade_id]) : "";
      const ver = s.verification === "verified" ? '<span class="tag green">' + esc(t("d.ver.verified")) + "</span>"
        : (s.verification === "pending_review" || s.verification === "mygov_record") ? '<span class="tag amber">' + esc(t("d.ver.pending")) + "</span>"
          : '<span class="tag">' + esc(t("d.ver.unverified")) + "</span>";
      const prioRow = prio ? '<div class="sos-prio"><span class="tag violet">' + ICON.wheelchair + " " + esc(t("d.sos.priority")) + '</span><span class="tag">' + esc(t("d.sos.role." + s.citizen_role)) + "</span>" + ver + "</div>" : "";
      return '<div class="sos-item ' + cls + '"><span class="sos-ico">' + (prio ? ICON.wheelchair : ICON[s.type]) + '</span><div>' + prioRow + '<div class="sos-row"><span class="sos-type">#' + s.id + " " +
        esc(t("sos.type." + s.type)) + '</span><span class="sos-time">' + esc(timeAgo(s.created_at)) + '</span></div><div class="sos-meta">' + esc(zoneName(s.district_id, s.district_name)) +
        ' · <span class="tag ' + tag + '">' + esc(t("d.sos.status." + s.status)) + "</span>" + brig + "</div>" +
        (s.note ? '<div class="sos-note">' + esc(s.note) + "</div>" : "") + btns + "</div></div>";
    }).join("");
  }

  function renderAlerts() {
    const list = state.data.alerts.slice(0, 20);
    if (!list.length) { $("alertList").innerHTML = '<div class="d-empty">' + esc(t("d.alerts.empty")) + "</div>"; return; }
    const decide = can("alert:decide");
    $("alertList").innerHTML = list.map((a) => {
      const st = a.status === "pending" ? '<span class="tag red">' + esc(t("d.alert.pending")) + "</span>"
        : a.status === "confirmed" ? '<span class="tag green">' + esc(t("d.alert.confirmed")) + "</span>" : '<span class="tag">' + esc(t("d.alert.dismissed")) + "</span>";
      let body = "";
      if (a.status === "pending" && decide) {
        body = '<div class="sos-btns"><button class="btn-xs danger" type="button" data-confirm="' + a.id + '">' + esc(t("d.alert.confirm")) +
          '</button><button class="btn-xs" type="button" data-dismiss="' + a.id + '">' + esc(t("d.alert.dismiss")) + "</button></div>";
      } else if (a.status === "confirmed") {
        const ac = a.actions || {};
        body = '<ul class="al-acts">' +
          (ac.brigade ? "<li>" + ICON.brigade + "<span>" + esc(t("d.alert.done_brigade", { name: nm(ac.brigade), dist: fmtDist(ac.brigade.distance_m) })) + "</span></li>"
            : '<li class="warn">' + ICON.alert + "<span>" + esc(t("d.alert.no_brigade")) + "</span></li>") +
          (ac.new_shelter ? "<li>" + ICON.shelter + "<span>" + esc(t("d.alert.done_shelter", { name: nm(ac.new_shelter), cap: fmtNum(ac.new_shelter.capacity) })) + "</span></li>"
            : '<li class="warn">' + ICON.alert + "<span>" + esc(t("d.alert.no_shelter")) + "</span></li>") + "</ul>";
      }
      const foot = a.decided_by ? '<div class="al-foot">' + esc(t("d.alert.by", { user: a.decided_by })) + " · " + esc(timeAgo(a.decided_at)) + "</div>" : "";
      return '<div class="al-card' + (a.status === "pending" ? " is-pending" : "") + '"><div class="al-top"><span class="tag red">' + esc(t("d.alert.headline")) + "</span>" + st +
        '</div><div class="al-what">' + esc(alertText(a)) + '</div><div class="al-foot">' + esc(timeAgo(a.created_at)) + "</div>" + body + foot + "</div>";
    }).join("");
  }

  function renderBrigades() {
    const manage = can("brigade:manage");
    $("brigadeList").innerHTML = state.data.brigades.map((b) => {
      let btn = "";
      if (manage && b.status === "dispatched") btn = '<button class="btn-xs" type="button" data-brigade="' + esc(b.id) + '" data-bstatus="on_scene">' + esc(t("d.brigade.arrive")) + "</button>";
      else if (manage && b.status !== "available") btn = '<button class="btn-xs" type="button" data-brigade="' + esc(b.id) + '" data-bstatus="available">' + esc(t("d.brigade.free")) + "</button>";
      return '<div class="br-item"><span class="br-ico">' + ICON.brigade + '</span><div><div class="br-name">' + esc(nm(b)) +
        (b.accessible_transport ? ' <span class="acc-mini" title="' + esc(t("d.brigade.accessible")) + '">' + ICON.wheelchair + "</span>" : "") + '</div><div class="br-sub">' +
        esc(t("d.brigade.type." + b.type)) + " · " + esc(zoneName(b.district_id, b.district_name)) + '</div></div><div class="br-right"><span class="tag ' + (BRIGADE_TAG[b.status] || "") + '">' +
        esc(t("d.brigade." + b.status)) + "</span>" + btn + "</div></div>";
    }).join("");
  }

  function bar(pct, color) { return '<div class="bar" style="--c:' + color + '"><i style="width:' + Math.min(100, pct) + '%"></i></div>'; }

  function renderZones() {
    const zones = state.data.zones.slice().sort((a, b) => ["critical", "high", "medium", "low"].indexOf(a.risk) - ["critical", "high", "medium", "low"].indexOf(b.risk) || b.sos_active - a.sos_active);
    $("zoneTable").innerHTML = '<table class="d-table"><thead><tr><th>' + esc(t("d.zones.zone")) + '</th><th class="r">' + esc(t("d.zones.sos")) + '</th><th class="r">' +
      esc(t("d.zones.sos5")) + '</th><th class="r">' + esc(t("d.zones.prio")) + "</th><th>" + esc(t("d.zones.occ")) + '</th><th class="r">' + esc(t("d.zones.brig")) + "</th><th>" + esc(t("d.zones.risk")) + "</th></tr></thead><tbody>" +
      zones.map((z) => {
        const c = z.occupancy_pct >= 90 ? LEVEL_COLOR.full : z.occupancy_pct >= 70 ? LEVEL_COLOR.high : z.occupancy_pct >= 40 ? LEVEL_COLOR.medium : LEVEL_COLOR.low;
        return '<tr><td class="nm">' + esc(nm(z)) + '</td><td class="r">' + z.sos_active + '</td><td class="r">' + z.sos_window + '</td><td class="r">' +
          (z.sos_priority ? '<span class="tag violet">' + z.sos_priority + "</span>" : "0") + '</td><td><div class="occ-cell">' + bar(z.occupancy_pct, c) +
          "<b>" + Math.round(z.occupancy_pct) + '%</b></div></td><td class="r">' + z.brigades_available + '</td><td><span class="tag ' + RISK_TAG[z.risk] + '">' + esc(t("d.risk." + z.risk)) + "</span></td></tr>";
      }).join("") + "</tbody></table>";
  }

  function renderShelters() {
    const manage = can("shelter:manage");
    const order = { open: 0, reserve: 1, closed: 2 };
    const list = state.data.shelters.slice().sort((a, b) => order[a.status] - order[b.status] || b.occupancy_pct - a.occupancy_pct);
    $("shelterTable").innerHTML = '<table class="d-table"><thead><tr><th>' + esc(t("d.shelters.name")) + "</th><th>" + esc(t("d.shelters.occ")) + "</th><th>" +
      esc(t("d.shelters.status")) + "</th><th>" + esc(t("fc.title")) + "</th><th></th></tr></thead><tbody>" + list.map((s) => {
        const fc = s.status === "open" ? fcText(s.forecast) : { main: "", sub: "", level: "none" };
        const fcCell = fc.main ? '<div class="fc fc-' + fc.level + '">' + esc(fc.main) + "</div>" + (fc.sub ? '<div class="sub">' + esc(fc.sub) + "</div>" : "") : "";
        let ctl = "";
        if (manage) {
          ctl = s.status === "open"
            ? '<span class="occ-ctl"><button class="btn-xs" type="button" data-occ="' + esc(s.id) + '" data-d="-50" aria-label="-50">−</button><button class="btn-xs" type="button" data-occ="' + esc(s.id) + '" data-d="50" aria-label="+50">+</button></span>'
            : '<button class="btn-xs" type="button" data-sstatus="' + esc(s.id) + '" data-v="open">' + esc(t("d.shelter.open_btn")) + "</button>";
        }
        const tag = s.status === "open" ? (s.accepting ? "green" : "red") : "";
        return '<tr><td><div class="nm">' + esc(nm(s)) + (s.accessible ? ' <span class="acc-mini" title="' + esc(t("legend.accessible")) + '">' + ICON.wheelchair + "</span>" : "") +
          '</div><div class="sub">' + esc(t("type." + s.type)) + " · " + fmtNum(s.occupancy) + " / " + fmtNum(s.capacity) +
          '</div></td><td><div class="occ-cell">' + bar(s.occupancy_pct, levelColor(s)) + "<b>" + Math.round(s.occupancy_pct) + '%</b></div></td><td><span class="tag ' + tag + '">' +
          esc(s.status === "open" && !s.accepting ? t("plan.full") : t("d.shelter." + s.status)) + '</span><div class="sub">' + esc(t("fc.updated", { ago: timeAgo(s.updated_at) })) +
          "</div></td><td>" + fcCell + "</td><td>" + ctl + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function renderCharts() {
    const tl = state.data.timeline;
    const max = Math.max(1, ...tl);
    $("timeline").innerHTML = '<div class="spark">' + tl.map((v) => '<i class="' + (v ? "" : "is-zero") + '" style="height:' + Math.max(3, (v / max) * 100) + '%" title="' + v + '"></i>').join("") +
      '</div><div class="spark-axis"><span>−30</span><span>−15</span><span>' + esc(t("time.now")) + "</span></div>";
    const bt = state.data.by_type;
    const tmax = Math.max(1, ...Object.values(bt));
    const colors = { fire: "#FF5A5F", collapse: "#F2A20C", flood: "#2F6FED", medical: "#6EE7B7" };
    $("typeChart").innerHTML = Object.keys(bt).map((k) => '<div class="tbar"><span>' + esc(t("sos.type." + k)) + "</span>" + bar((bt[k] / tmax) * 100, colors[k]) + "<b>" + bt[k] + "</b></div>").join("");
  }

  function renderRoads() {
    const list = state.data.closures;
    if (!list.length) { $("roadList").innerHTML = '<div class="d-empty" style="padding:10px">' + esc(t("d.roads.empty")) + "</div>"; return; }
    const manage = can("road:manage");
    $("roadList").innerHTML = list.map((c) => '<div class="road-item"><span>' + esc(c.name) + ' <span class="tag ' + (c.source === "external" ? "blue" : "amber") + '">' +
      esc(t("d.roads.source." + c.source)) + "</span></span>" + (manage && c.source === "dispatcher" ? '<button class="btn-xs" type="button" data-open-road="' + esc(c.edge_id) + '">' + esc(t("d.roads.open")) + "</button>" : "") + "</div>").join("");
  }

  // myGov əlillik təsdiqi növbəsi
  function renderVerifs() {
    const show = can("verification:manage");
    $("verifHead").hidden = !show; $("verifList").hidden = !show;
    if (!show) return;
    if (!state.verifs.length) { $("verifList").innerHTML = '<div class="d-empty" style="padding:10px">' + esc(t("d.verif.empty")) + "</div>"; return; }
    $("verifList").innerHTML = state.verifs.map((v) => {
      const tag = v.disability_status === "pending_review" ? "amber" : v.disability_status === "mygov_record" ? "blue" : "";
      const sub = [v.mygov_disability ? t("d.verif.group", { group: v.mygov_disability_group }) : "", v.certificate_uploaded ? "" : t("d.verif.no_file")].filter(Boolean).join(" · ");
      return '<div class="road-item verif-item"><span><b>' + esc(v.full_name) + '</b> <span class="tag ' + tag + '">' + esc(t("acc.status." + v.disability_status)) +
        "</span>" + (sub ? '<br><span class="sos-meta">' + esc(sub) + "</span>" : "") + '</span><span class="sos-btns" style="margin:0">' +
        (v.certificate_uploaded ? '<button class="btn-xs" type="button" data-vview="' + esc(v.citizen_id) + '">' + esc(t("d.verif.view")) + "</button>" : "") +
        '<button class="btn-xs pri" type="button" data-vdecide="approve" data-vid="' + esc(v.citizen_id) + '">' + esc(t("d.verif.approve")) + "</button>" +
        '<button class="btn-xs" type="button" data-vdecide="reject" data-vid="' + esc(v.citizen_id) + '">' + esc(t("d.verif.reject")) + "</button></span></div>";
    }).join("");
  }

  async function viewCertificate(cid) {
    // Fayl icazə tələb edir: token başlıqla göndərilir, nəticə blob kimi yeni pəncərədə açılır
    try {
      const res = await fetch("/api/v1/dispatch/verifications/" + encodeURIComponent(cid) + "/certificate", { headers: { Authorization: "Bearer " + state.token } });
      if (!res.ok) throw new Error("http_" + res.status);
      const url = URL.createObjectURL(await res.blob());
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (e) { toast(t("common.error"), "error"); }
  }

  function renderRouting() {
    const r = state.data.routing;
    const chip = $("routingChip");
    chip.className = "chip d-chip " + (r.degraded ? "is-bad" : "is-ok");
    const extra = r.degraded ? [r.degraded_since ? t("d.routing.since", { ago: timeAgo(r.degraded_since) }) : "", r.last_error ? t("d.routing.err", { err: r.last_error }) : ""].filter(Boolean).join(" · ") : "";
    chip.innerHTML = ICON.signal + "<span>" + esc(t(r.degraded ? "d.routing.fallback" : "d.routing.external")) + (extra ? ' <small class="chip-sub">' + esc(extra) + "</small>" : "") + "</span>";
    chip.title = r.degraded ? JSON.stringify(r.tier_calls || {}) : "";
    renderOutageSwitch();
  }

  // Drop 2 demo açarı: xarici API-ni həqiqətən 503 / timeout rejiminə salır (sadəcə görüntü deyil)
  state.outageOn = false;
  function renderOutageSwitch() {
    const box = $("outageBox");
    box.hidden = !can("demo:run");
    if (box.hidden) return;
    const r = state.data && state.data.routing;
    // Həqiqi vəziyyət serverdən: breaker açıqdırsa və biz açarı yandırmışıqsa — ON
    const on = state.outageOn && !!(r && r.degraded);
    box.classList.toggle("is-on", state.outageOn);
    $("outageSwitch").setAttribute("aria-checked", String(state.outageOn));
    $("outageState").textContent = state.outageOn ? t("d.outage.on") + (on ? "" : " …") : t("d.outage.off");
    $("outageMode").disabled = state.outageOn;
  }
  async function toggleOutage() {
    const turnOn = !state.outageOn;
    state.outageOn = turnOn;
    renderOutageSwitch();
    const body = turnOn ? { name: "api_outage", mode: $("outageMode").value } : { name: "api_restore" };
    const res = await action(() => call("/dispatch/demo/scenario", { method: "POST", body }),
      turnOn ? t("d.sim.api_outage" + ($("outageMode").value === "timeout" ? "_timeout" : "")) + " — " + t("d.toast.done") : t("d.sim.api_restore") + " — " + t("d.toast.done"));
    if (!res) { state.outageOn = !turnOn; renderOutageSwitch(); }
  }

  function renderConn() {
    $("connDot").className = "dot " + (state.conn === "live" ? "is-live" : state.conn === "offline" ? "is-off" : "is-wait");
    $("connText").textContent = t(state.conn === "live" ? "conn.live" : state.conn === "offline" ? "conn.offline" : "conn.reconnect");
  }

  function renderSimMenu() {
    const items = ["sos_cluster", "priority_sos", "shelter_fill", "crowd", "inflow_start", "inflow_stop", "-", "road_close", "road_open_all", "-", "api_outage", "api_outage_timeout", "api_restore", "-", "reset"];
    $("simList").innerHTML = items.map((n) => n === "-" ? "<hr>" : '<button type="button" data-sim="' + n + '">' + esc(t("d.sim." + n)) + "</button>").join("");
  }

  function renderLegend() {
    $("legend").innerHTML = '<span><i style="background:#E0282E"></i>' + esc(t("d.map.legend.sos")) + '</span><span><i class="sq" style="background:#2563EB"></i>' + esc(t("d.map.legend.brigade")) +
      '</span><span><i style="background:' + LEVEL_COLOR.low + '"></i><i style="background:' + LEVEL_COLOR.high + '"></i><i style="background:' + LEVEL_COLOR.critical + '"></i>' + esc(t("d.map.legend.shelter")) +
      '</span><span><i class="ln"></i>' + esc(t("legend.closed")) + "</span>";
  }

  // ---------- Əməliyyatlar ----------
  async function action(fn, okMsg) {
    try { const res = await fn(); if (okMsg) toast(okMsg, "success"); refresh(); return res; } catch (e) { return null; }
  }

  function bind() {
    $("loginForm").addEventListener("submit", doLogin);
    $("logoutBtn").addEventListener("click", logout);
    $("outageSwitch").addEventListener("click", toggleOutage);
    $("sosFilter").addEventListener("click", (e) => {
      const b = e.target.closest("[data-filter]");
      if (!b) return;
      state.filter = b.dataset.filter;
      $("sosFilter").querySelectorAll("button").forEach((x) => x.classList.toggle("is-active", x === b));
      renderSos();
    });
    $("simBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      const open = $("simList").hidden;
      $("simList").hidden = !open;
      $("simBtn").setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => { if (!e.target.closest("#simMenu")) $("simList").hidden = true; });

    document.addEventListener("click", async (e) => {
      const el = e.target.closest("button");
      if (!el) return;
      const ds = el.dataset;
      if (ds.confirm) {
        el.disabled = true;
        const res = await action(() => call("/dispatch/alerts/" + ds.confirm + "/confirm", { method: "POST" }));
        if (res) {
          const ac = res.actions || {};
          toast([ac.brigade ? t("d.alert.done_brigade", { name: ac.brigade.name, dist: fmtDist(ac.brigade.distance_m) }) : t("d.alert.no_brigade"),
            ac.new_shelter ? t("d.alert.done_shelter", { name: ac.new_shelter.name, cap: fmtNum(ac.new_shelter.capacity) }) : t("d.alert.no_shelter")].join(" · "), "success");
        }
      } else if (ds.vview) viewCertificate(ds.vview);
      else if (ds.vdecide) action(() => call("/dispatch/verifications/" + encodeURIComponent(ds.vid) + "/" + ds.vdecide, { method: "POST" }), t("d.toast.done"));
      else if (ds.dismiss) action(() => call("/dispatch/alerts/" + ds.dismiss + "/dismiss", { method: "POST" }));
      else if (ds.assign) {
        el.disabled = true;
        const res = await action(() => call("/dispatch/sos/" + ds.assign + "/assign", { method: "POST", body: {} }));
        if (res) toast(t("sos.assigned", { name: nm(brigadeById(res.brigade_id)) || res.brigade_name }) + " · " + fmtDist(res.brigade_distance_m), "success");
      } else if (ds.resolve) action(() => call("/dispatch/sos/" + ds.resolve + "/resolve", { method: "POST" }));
      else if (ds.brigade) action(() => call("/dispatch/brigades/" + ds.brigade, { method: "PATCH", body: { status: ds.bstatus } }));
      else if (ds.occ) {
        const s = state.data.shelters.find((x) => x.id === ds.occ);
        if (s) action(() => call("/dispatch/shelters/" + s.id, { method: "PATCH", body: { occupancy: Math.max(0, s.occupancy + Number(ds.d)) } }));
      } else if (ds.sstatus) action(() => call("/dispatch/shelters/" + ds.sstatus, { method: "PATCH", body: { status: ds.v } }));
      else if (ds.closeRoad) {
        map.closePopup();
        const res = await action(() => call("/dispatch/roads/" + ds.closeRoad + "/close", { method: "POST", body: { reason: "dispatcher" } }), t("d.toast.done"));
        if (res && res.rerouted_citizens) toast(t("d.toast.rerouted", { n: res.rerouted_citizens }));
      } else if (ds.openRoad) action(() => call("/dispatch/roads/" + ds.openRoad + "/open", { method: "POST" }), t("d.toast.done"));
      else if (ds.sim) {
        $("simList").hidden = true;
        if (ds.sim === "reset") { state.roadsDrawn = false; state.outageOn = false; }
        if (ds.sim === "api_outage" || ds.sim === "api_outage_timeout") state.outageOn = true;
        if (ds.sim === "api_restore") state.outageOn = false;
        const body = ds.sim === "api_outage_timeout" ? { name: "api_outage", mode: "timeout" } : { name: ds.sim, mode: ds.sim === "api_outage" ? "error" : undefined };
        action(() => call("/dispatch/demo/scenario", { method: "POST", body }), t("d.sim." + ds.sim) + " — " + t("d.toast.done"));
      }
    });

    window.I18N.onChange(() => { renderSimMenu(); renderLegend(); renderConn(); render(); });
  }

  function tick() {
    const now = new Date();
    $("clock").textContent = now.toLocaleTimeString("az-AZ", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Baku" });
  }

  function start() {
    $("brandMark").innerHTML = ICON.shield;
    $("loginMark").innerHTML = ICON.shield;
    $("icoPlay").innerHTML = ICON.play;
    $("icoLogout").innerHTML = ICON.logout;
    renderSimMenu(); renderLegend(); renderConn(); bind(); tick();
    setInterval(tick, 1000);
    // SSE-yə əlavə olaraq: nisbi vaxtlar yenilənsin və bağlantı kəsiləndə məlumat köhnəlməsin
    setInterval(() => { if (state.token && !$("main").hidden) refresh(); }, state.conn === "live" ? 15000 : 5000);
    if (state.token) enter(); else showLogin();
  }

  document.addEventListener("DOMContentLoaded", start);
})();

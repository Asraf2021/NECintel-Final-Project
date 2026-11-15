/* Modul 1 — Vətəndaş Təhlükəsizlik tətbiqi */
(function () {
  const { fcText, nm, api: rawApi, esc, connectStream, fmtDist, fmtMin, fmtNum, debounce, levelColor, LEVEL_COLOR, ICON, TYPE_ICON,
    createMap, shelterIcon, toast, t } = window.UCE;
  const $ = (id) => document.getElementById(id);
  const SOS_TYPES = ["fire", "collapse", "flood", "medical"];

  // Anonim cihaz identifikatoru (şəxsi məlumat toplanmır)
  function citizenId() {
    let id = null;
    try { id = localStorage.getItem("uce_cid"); } catch (e) { /* ignore */ }
    if (!id) {
      const bytes = new Uint8Array(12);
      crypto.getRandomValues(bytes);
      id = "c-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
      try { localStorage.setItem("uce_cid", id); } catch (e) { /* ignore */ }
    }
    return id;
  }

  const state = {
    cid: citizenId(), ctoken: storedToken(), account: null, meta: null, pos: null, plan: null, selectedId: null, loading: false,
    citizen: { status: "unknown", active_sos: [], shelter_id: null }, degraded: false, banners: [], conn: "reconnect",
    closures: [], barriers: [], role: storedRole(),
  };
  let map, meMarker, routeLayer, routeCasing, roadsLayer, closuresLayer, sheltersLayer, barrierLayer;

  // myGov ilə girişdən sonra verilən vətəndaş tokeni: bütün sorğulara avtomatik əlavə olunur
  const api = (path, opts) => rawApi(path, Object.assign({ token: state.ctoken }, opts || {}));
  function storedToken() {
    try { return localStorage.getItem("uce_ctoken") || null; } catch (e) { return null; }
  }

  function storedRole() {
    try { return localStorage.getItem("uce_role") === "priority_citizen" ? "priority_citizen" : "citizen"; } catch (e) { return "citizen"; }
  }
  const isPriority = () => state.role === "priority_citizen";

  // ---------- Xəritə ----------
  function initMap() {
    const c = state.meta.city;
    map = createMap("map", c.center, c.zoom, false);
    roadsLayer = L.layerGroup().addTo(map);
    barrierLayer = L.layerGroup().addTo(map);
    closuresLayer = L.layerGroup().addTo(map);
    sheltersLayer = L.layerGroup().addTo(map);
    map.on("click", (e) => setPosition(e.latlng.lat, e.latlng.lng, true));
    setTimeout(() => map.invalidateSize(), 200);
    window.addEventListener("resize", debounce(() => map.invalidateSize(), 150));
  }

  async function loadRoads() {
    try {
      const data = await api("/roads");
      roadsLayer.clearLayers();
      data.edges.forEach((e) => L.polyline(e.points, { color: "#93A1B3", weight: 2.500, opacity: .75, interactive: false }).addTo(roadsLayer));
      state.barriers = data.edges.filter((e) => !e.accessible);
      drawBarriers();
      state.closures = data.closures;
      setDegraded(data.routing.degraded);
      drawClosures();
    } catch (e) { /* xəritə yolsuz da işləyir */ }
  }

  // Prioritet rejimdə əlçatan olmayan seqmentlər (pilləkən, yeraltı keçid...) xəritədə göstərilir
  function drawBarriers() {
    if (!barrierLayer) return;
    barrierLayer.clearLayers();
    if (!isPriority()) return;
    state.barriers.forEach((e) => {
      L.polyline(e.points, { color: "#F2A20C", weight: 5, opacity: .95, dashArray: "2 7", lineCap: "round" })
        .bindTooltip(esc(t("barrier." + e.barrier)) + " — " + esc(e.name)).addTo(barrierLayer);
    });
  }

  function drawClosures() {
    closuresLayer.clearLayers();
    state.closures.forEach((c) => {
      L.polyline(c.points, { color: "#E0282E", weight: 5, opacity: .95, dashArray: "8 8" })
        .bindTooltip(esc(c.name) + " — " + t("legend.closed")).addTo(closuresLayer);
    });
  }

  function drawShelters() {
    sheltersLayer.clearLayers();
    if (!state.plan) return;
    state.plan.shelters.forEach((s) => {
      L.marker([s.lat, s.lon], { icon: shelterIcon(s, s.id === state.selectedId), zIndexOffset: s.id === state.selectedId ? 500 : 0 })
        .on("click", () => selectShelter(s.id)).bindTooltip(esc(nm(s))).addTo(sheltersLayer);
    });
  }

  function drawRoute(fit) {
    if (routeLayer) { map.removeLayer(routeLayer); map.removeLayer(routeCasing); routeLayer = routeCasing = null; }
    const s = selected();
    if (!s || !s.route) return;
    routeCasing = L.polyline(s.route.points, { color: "#fff", weight: 9, opacity: .95, lineJoin: "round" }).addTo(map);
    routeLayer = L.polyline(s.route.points, { color: "#2563EB", weight: 5, opacity: 1, lineJoin: "round" }).addTo(map);
    if (fit) map.fitBounds(routeLayer.getBounds(), { padding: [50, 50], maxZoom: 16 });
  }

  function selected() {
    return state.plan ? state.plan.shelters.find((s) => s.id === state.selectedId) : null;
  }

  // ---------- Yerləşmə və plan ----------
  function setPosition(lat, lon, fromMap) {
    state.pos = { lat, lon };
    // Son yerləşmə yadda saxlanılır (məs. myGov girişindən qayıdanda itməsin)
    try { localStorage.setItem("uce_pos", JSON.stringify({ lat, lon, district: fromMap ? "" : $("district").value, ts: Date.now() })); } catch (e) { /* ignore */ }
    if (!meMarker) {
      meMarker = L.marker([lat, lon], { icon: L.divIcon({ className: "", html: '<div class="mk-me"></div>', iconSize: [20, 20], iconAnchor: [10, 10] }), zIndexOffset: 1000 }).addTo(map);
    } else meMarker.setLatLng([lat, lon]);
    meMarker.bindTooltip(t("loc.you"));
    if (fromMap) $("district").value = "";
    state.banners = state.banners.filter((b) => b.kind !== "reroute");
    loadPlan({ fit: true });
  }

  async function loadPlan(opts) {
    opts = opts || {};
    if (!state.pos) return;
    if (!state.plan) { state.loading = true; renderPlan(); }
    try {
      const q = "lat=" + state.pos.lat.toFixed(6) + "&lon=" + state.pos.lon.toFixed(6) + "&citizen_id=" + state.cid + "&role=" + state.role;
      applyPlan(await api("/evacuation?" + q), opts);
    } catch (e) {
      state.loading = false;
      if (!state.plan) renderPlan(t("common.error"));
    }
  }

  function applyPlan(plan, opts) {
    opts = opts || {};
    const prevSelected = state.selectedId;
    const prevRec = state.plan && state.plan.recommended ? state.plan.recommended.shelter_id : null;
    state.plan = plan;
    state.loading = false;
    setDegraded(plan.routing.degraded);
    const rec = plan.recommended;
    const keep = !plan.rerouted && !opts.fit && prevSelected && prevSelected !== prevRec && plan.shelters.some((s) => s.id === prevSelected && s.route);
    state.selectedId = keep ? prevSelected : (rec ? rec.shelter_id : null);
    if (plan.rerouted) pushReroute(plan);
    renderAll(opts.fit || plan.rerouted);
  }

  function pushReroute(plan) {
    let text = t("reroute.road_closed");
    if (plan.reroute_reason === "shelter_full" && plan.previous_shelter) {
      text = t("reroute.shelter_full", { old: nm(plan.previous_shelter), pct: Math.round(plan.previous_shelter.occupancy_pct), new: nm(plan.recommended) });
    }
    state.banners = state.banners.filter((b) => b.kind !== "reroute");
    state.banners.unshift({ kind: "reroute", title: t("reroute.title"), text });
    if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
    $("panel").scrollTo({ top: 0, behavior: "smooth" });
  }

  function selectShelter(id) {
    state.selectedId = id;
    renderAll(true);
  }

  function setDegraded(flag) {
    if (state.degraded === flag) return;
    state.degraded = flag;
    renderBanners();
  }

  // ---------- Render ----------
  function renderAll(fit) {
    renderBanners(); renderStatus(); renderPlan(); drawShelters(); drawRoute(fit);
  }

  function renderBanners() {
    const parts = [];
    if (state.degraded) {
      parts.push('<div class="banner banner-degraded"><span class="b-ico">' + ICON.signal + "</span><div><b>" + esc(t("degraded.title")) +
        "</b><p>" + esc(t("degraded.text")) + "</p></div><span></span></div>");
    }
    state.banners.forEach((b, i) => {
      parts.push('<div class="banner banner-' + (b.kind === "reroute" ? "reroute" : "info") + '"><span class="b-ico">' + (b.kind === "reroute" ? ICON.route : ICON.shelter) +
        "</span><div><b>" + esc(b.title) + "</b><p>" + esc(b.text) + '</p></div><button class="b-x" type="button" data-dismiss="' + i +
        '" aria-label="' + esc(t("common.close")) + '">' + ICON.close + "</button></div>");
    });
    $("banners").innerHTML = parts.join("");
  }

  function renderStatus() {
    const c = state.citizen;
    const box = $("statusCard");
    if (c.active_sos && c.active_sos.length) {
      const s = c.active_sos[0];
      let line = s.status === "assigned" && s.brigade_name
        ? '<div class="st-line">' + ICON.brigade + "<span>" + esc(t("sos.assigned", { name: nm({ name: s.brigade_name, name_en: s.brigade_name_en }) })) + "</span></div>" : "";
      if (s.priority === 1) line = '<div class="st-line st-priority">' + ICON.wheelchair + "<span>" + esc(t("sos.priority")) + "</span></div>" + line;
      box.innerHTML = '<div class="status-card is-sos" role="status"><h3>' + ICON[s.type] + "<span>" + esc(t("sos.sent")) + " · " +
        esc(t("sos.type." + s.type)) + "</span></h3><p>" + esc(t("sos.wait")) + "</p>" + line + "</div>";
    } else if (c.status === "safe") {
      const sh = state.safeShelterName ? '<div class="st-line">' + ICON.shelter + "<span>" + esc(t("safe.at", { name: state.safeShelterName })) + "</span></div>" : "";
      box.innerHTML = '<div class="status-card is-safe" role="status"><h3>' + ICON.check + "<span>" + esc(t("safe.done")) + "</span></h3><p>" +
        esc(t("safe.text")) + "</p>" + sh + '<button class="btn btn-sm" type="button" id="undoSafe">' + esc(t("safe.undo")) + "</button></div>";
    } else box.innerHTML = "";
  }

  function occBar(s) {
    return '<div class="bar" style="--c:' + levelColor(s) + '"><i style="width:' + Math.min(100, s.occupancy_pct) + '%"></i></div>';
  }

  function renderPlan(error) {
    const box = $("planBox");
    if (error) { box.innerHTML = '<div class="card empty">' + esc(error) + '<br><button class="btn btn-sm" id="retryPlan" type="button">' + esc(t("common.retry")) + "</button></div>"; return; }
    if (state.loading) { box.innerHTML = '<div class="skeleton"></div>'; return; }
    if (!state.plan) { box.innerHTML = '<div class="card empty"><span class="e-ico">' + ICON.pin + "</span>" + esc(t("plan.empty")) + "</div>"; return; }
    const plan = state.plan;
    const wheel = plan.profile === "wheelchair";
    if (!plan.shelters.length || (wheel && !plan.recommended)) {
      box.innerHTML = '<div class="card empty">' + (wheel ? '<span class="e-ico">' + ICON.wheelchair + "</span>" + esc(t("plan.no_accessible")) : esc(t("plan.none"))) + "</div>";
      return;
    }

    const s = selected();
    const rec = plan.recommended;
    let html = "";
    if (s) {
      const isRec = rec && rec.shelter_id === s.id;
      const free = Math.max(0, s.capacity - s.occupancy);
      const r = s.route;
      html += '<section class="card rec"><h2 class="card-title"><span class="ti">' + ICON.route + "</span>" +
        esc(t(isRec ? "plan.recommended" : "plan.selected")) + "</h2>" +
        (wheel ? '<div class="acc-badge">' + ICON.wheelchair + "<span>" + esc(t("plan.accessible_route")) + "</span></div>" : "") +
        '<div class="rec-head"><span class="rec-ico" style="--c:' + levelColor(s) + '">' + ICON[TYPE_ICON[s.type]] + '</span><div><div class="rec-name">' + esc(nm(s)) +
        '</div><div class="rec-meta">' + esc(t("type." + s.type)) + " · " + esc(districtName(s.district_id)) + "</div></div></div>" +
        '<div class="rec-stats"><div class="stat"><b>' + (r ? esc(fmtMin(r.duration_min)) : "—") + "</b><span>" + esc(t(wheel ? "plan.wheel" : "plan.walk")) + "</span></div>" +
        '<div class="stat"><b>' + (r ? esc(fmtDist(r.distance_m)) : esc(fmtDist(s.distance_m))) + "</b><span>" + esc(t("plan.route")) + "</span></div>" +
        '<div class="stat"><b>' + esc(fmtNum(free)) + "</b><span>" + esc(t("plan.free")) + "</span></div></div>" +
        '<div class="occ-row"><span>' + esc(t("plan.occupancy")) + '</span><b>' + s.occupancy_pct + "% · " + fmtNum(s.occupancy) + " / " + fmtNum(s.capacity) + "</b></div>" + occBar(s);
      if (isRec && rec.reason !== "nearest") html += '<p class="note">' + esc(t("plan.reason." + rec.reason)) + "</p>";
      const fc = s.forecast;
      if (fc && fc.trend === "rising" && fc.t90_min > 0 && (fc.confidence === "medium" || fc.confidence === "high")) {
        html += '<p class="note note-fc">' + esc(t("fc.citizen", { min: Math.round(fc.t90_min), conf: t("fc.conf." + fc.confidence) })) + "</p>";
      }
      if (wheel && state.barriers.length) html += '<p class="note note-acc">' + esc(t("plan.barriers_avoided", { n: state.barriers.length })) + "</p>";
      if (r && r.steps && r.steps.length) {
        html += '<ol class="steps" aria-label="' + esc(t("plan.steps")) + '">' + r.steps.map((st) => "<li><span>" + esc(st.road) + "</span><span>" + esc(fmtDist(st.distance_m)) + "</span></li>").join("") + "</ol>";
      } else if (r) html += '<ol class="steps"><li><span>' + esc(t("plan.direct")) + "</span><span>" + esc(fmtDist(r.distance_m)) + "</span></li></ol>";
      else html += '<p class="note">' + esc(t("plan.no_route")) + "</p>";
      html += '<button class="btn btn-success btn-block" type="button" id="arriveBtn">' + ICON.check + "<span>" + esc(t("plan.arrived")) + "</span></button></section>";
    }

    html += '<section><h2 class="card-title" style="margin:4px 2px 10px"><span class="ti">' + ICON.pin + "</span>" + esc(t("plan.nearest")) + '</h2><div class="list">';
    plan.shelters.forEach((sh) => {
      const full = !sh.accepting;
      const f = fcText(sh.forecast);
      const sub = (sh.route ? fmtMin(sh.route.duration_min) + " · " + fmtDist(sh.route.distance_m) : t("plan.no_route")) + " · " + t("type." + sh.type) +
        (f.level === "rising" || f.level === "soon" ? " · " + f.main : "");
      html += '<button type="button" class="item' + (sh.id === state.selectedId ? " is-active" : "") + (full ? " is-full" : "") + '" data-shelter="' + esc(sh.id) + '" style="--c:' + levelColor(sh) + '">' +
        '<span class="item-ico">' + ICON[TYPE_ICON[sh.type]] + '</span><span><span class="item-name">' + esc(nm(sh)) + (sh.accessible ? ' <span class="acc-dot" title="' + esc(t("plan.accessible")) + '">' + ICON.wheelchair + "</span>" : "") +
        '</span><span class="item-sub" style="display:block">' + esc(sub) + "</span>" + occBar(sh) +
        '</span><span class="item-pct">' + Math.round(sh.occupancy_pct) + "%<small>" + (full ? esc(t("plan.full")) : esc(fmtNum(sh.capacity - sh.occupancy))) + "</small></span></button>";
    });
    html += "</div></section>";
    box.innerHTML = html;
  }

  function renderLegend() {
    const items = [["low", "<40%"], ["medium", "40–70%"], ["high", "70–90%"], ["full", "90–95%"], ["critical", ">95%"]];
    $("legend").innerHTML = "<b>" + esc(t("legend.title")) + "</b>" + items.map((i) => '<span><i style="background:' + LEVEL_COLOR[i[0]] + '"></i>' + i[1] + "</span>").join("") +
      '<span><i class="rt"></i>' + esc(t("legend.route")) + '</span><span><i class="ln"></i>' + esc(t("legend.closed")) + "</span>" +
      (isPriority() ? '<span><i class="bn"></i>' + esc(t("legend.barrier")) + "</span>" : "");
  }

  function renderConn() {
    $("connDot").className = "dot " + (state.conn === "live" ? "is-live" : state.conn === "offline" ? "is-off" : "is-wait");
    $("connText").textContent = t(state.conn === "live" ? "conn.live" : state.conn === "offline" ? "conn.offline" : "conn.reconnect");
  }

  function districtName(id) {
    const d = state.meta.districts.find((x) => x.id === id);
    return d ? nm(d) : "";
  }

  function fillDistricts() {
    const sel = $("district");
    const cur = sel.value;
    sel.innerHTML = '<option value="">' + esc(t("loc.district")) + "</option>" +
      state.meta.districts.map((d) => '<option value="' + esc(d.id) + '">' + esc(nm(d)) + "</option>").join("");
    sel.value = cur;
  }

  // ---------- SOS və status ----------
  function openSheet() {
    if (!state.pos) { toast(t("sos.need_location"), "error"); $("district").focus(); return; }
    $("sosPriorityNote").hidden = !isPriority();
    $("sosGrid").innerHTML = SOS_TYPES.map((type) => '<button type="button" class="sos-type" data-sos="' + type + '">' + ICON[type] + "<span>" + esc(t("sos.type." + type)) + "</span></button>").join("");
    $("sosSheet").hidden = false;
    const first = $("sosGrid").querySelector("button");
    if (first) first.focus();
  }
  function closeSheet() { $("sosSheet").hidden = true; $("sosBtn").focus(); }

  async function sendSos(type) {
    const note = $("sosNote").value.trim();
    closeSheet();
    try {
      await api("/sos", { method: "POST", body: { citizen_id: state.cid, type, lat: state.pos.lat, lon: state.pos.lon, note } });
      $("sosNote").value = "";
      toast(t("sos.sent"), "success");
      if (navigator.vibrate) navigator.vibrate(200);
      await loadCitizen();
    } catch (e) {
      toast(t(e.status === 429 ? "sos.rate" : "sos.error"), "error");
    }
  }

  async function markSafe(shelterId) {
    try {
      const body = { citizen_id: state.cid, status: "safe" };
      if (shelterId) body.shelter_id = shelterId;
      if (state.pos) { body.lat = state.pos.lat; body.lon = state.pos.lon; }
      const res = await api("/status", { method: "POST", body });
      state.safeShelterName = res.shelter ? nm(res.shelter) : null;
      toast(t("safe.done"), "success");
      await loadCitizen();
      $("panel").scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) { toast(t("common.error"), "error"); }
  }

  async function undoSafe() {
    try {
      await api("/status", { method: "POST", body: { citizen_id: state.cid, status: "evacuating" } });
      state.safeShelterName = null;
      await loadCitizen();
      loadPlan();
    } catch (e) { toast(t("common.error"), "error"); }
  }

  async function loadCitizen() {
    try { state.citizen = await api("/citizens/" + state.cid); } catch (e) { /* ignore */ }
    renderStatus();
  }

  // ---------- Rol: Hərəkət məhdudiyyətli / xüsusi qayğı (Challenge Drop 1) ----------
  function renderRole() {
    $("roleToggle").checked = isPriority();
    $("sosBadge").hidden = !isPriority();
    document.body.classList.toggle("is-priority", isPriority());
    renderLegend();
    drawBarriers();
  }

  async function saveRole(role, quiet) {
    try {
      await api("/citizens/" + state.cid + "/profile", { method: "PUT", body: { role } });
      state.role = role;
      if (state.account) loadAccount(false);
      try { localStorage.setItem("uce_role", role); } catch (e) { /* ignore */ }
      renderRole();
      if (!quiet) toast(t(role === "priority_citizen" ? "role.saved_on" : "role.saved_off"), "success");
      state.banners = state.banners.filter((b) => b.kind !== "reroute");
      if (state.pos) loadPlan({ fit: true });
      loadCitizen();
    } catch (e) {
      toast(t("common.error"), "error");
      renderRole();
    }
  }

  function restorePosition() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("uce_pos") || "null"); } catch (e) { saved = null; }
    if (!saved || Date.now() - saved.ts > 3600 * 1000) return;  // 1 saatdan köhnə yerləşmə istifadə olunmur
    if (saved.district) $("district").value = saved.district;
    setPosition(saved.lat, saved.lon, !saved.district);
  }

  // ---------- myGov hesabı və əlillik təsdiqi ----------
  function handleAuthHash() {
    const h = location.hash || "";
    if (h.startsWith("#mygov=")) {
      state.ctoken = decodeURIComponent(h.slice(7));
      try { localStorage.setItem("uce_ctoken", state.ctoken); } catch (e) { /* ignore */ }
    } else if (h.startsWith("#mygov_error=")) {
      setTimeout(() => toast(t("acc.err"), "error"), 300);
    }
    if (h.startsWith("#mygov")) history.replaceState(null, "", location.pathname + location.search);
  }

  async function loadAccount(showPrompt) {
    if (!state.ctoken) { state.account = null; renderAccount(); return; }
    try {
      const acc = await rawApi("/me", { token: state.ctoken });
      state.account = acc;
      if (acc.citizen_id !== state.cid) {  // hesab başqa cihazda yaradılıbsa, onun ID-si istifadə olunur
        state.cid = acc.citizen_id;
        try { localStorage.setItem("uce_cid", acc.citizen_id); } catch (e) { /* ignore */ }
      }
      const role = acc.disability_enabled ? "priority_citizen" : "citizen";
      if (role !== state.role) {
        state.role = role;
        try { localStorage.setItem("uce_role", role); } catch (e) { /* ignore */ }
        renderRole();
        if (state.pos) loadPlan({ fit: true });
      }
      renderAccount();
      if (showPrompt && acc.prompt_pending) openDis(1);
    } catch (e) {
      if (e.status === 401) {
        state.ctoken = null; state.account = null;
        try { localStorage.removeItem("uce_ctoken"); } catch (err) { /* ignore */ }
        toast(t("acc.expired"), "error");
      }
      renderAccount();
    }
  }

  function verChip(status) {
    const cls = status === "verified" ? "is-verified" : status === "rejected" ? "is-rejected"
      : (status === "pending_review" || status === "mygov_record" || status === "declared") ? "is-pending" : "";
    return '<span class="ver-chip ' + cls + '">' + (status === "verified" ? ICON.check : ICON.wheelchair) + "<span>" + esc(t("acc.status." + status)) + "</span></span>";
  }

  function renderAccount() {
    const box = $("accountCard");
    const a = state.account;
    if (!a) {
      box.innerHTML = '<h2 class="card-title"><span class="ti">' + ICON.users + "</span>" + esc(t("acc.title")) + '</h2><p class="acc-desc">' + esc(t("acc.desc")) +
        '</p><a class="btn btn-mygov" href="/api/v1/auth/mygov/start?citizen_id=' + encodeURIComponent(state.cid) + '">' + ICON.shield + "<span>" + esc(t("acc.login")) + "</span></a>";
      return;
    }
    const canUpload = a.disability_enabled && a.disability_status !== "verified";
    box.innerHTML = '<h2 class="card-title"><span class="ti">' + ICON.users + "</span>" + esc(t("acc.title")) + '</h2><div class="acc-row"><div><div class="acc-name">' +
      esc(a.given_name + " " + a.family_name) + '</div><div class="acc-sub">' + esc(t("acc.via")) + (a.phone_masked ? " · " + esc(a.phone_masked) : "") + "</div></div>" +
      verChip(a.disability_status) + '</div><div class="acc-actions">' +
      (canUpload ? '<button class="btn btn-sm" type="button" id="accUpload">' + esc(t("acc.upload_link")) + "</button>" : "") +
      '<button class="btn btn-sm btn-ghost" type="button" id="accLogout">' + ICON.logout + "<span>" + esc(t("acc.logout")) + "</span></button></div>";
  }

  function openDis(step) {
    if (!state.account) { toast(t("dis.need_login"), "error"); return; }
    $("disText").textContent = t("dis.text", { group: state.account.mygov_disability_group || "—" });
    $("disMygovLink").href = state.account.certificate_service_url || "https://mygov.az";
    $("disStep1").hidden = step !== 1;
    $("disStep2").hidden = step !== 2;
    $("disMsg").textContent = ""; $("disMsg").className = "dis-msg";
    $("disModal").hidden = false;
    const focus = step === 1 ? $("disConfirm") : $("disMygovLink");
    if (focus) focus.focus();
  }
  function closeDis() { $("disModal").hidden = true; }

  async function skipDis() {
    try { state.account = await api("/me/disability/skip", { method: "POST" }); } catch (e) { /* ignore */ }
    renderAccount(); closeDis();
  }

  async function confirmDis() {
    try {
      state.account = await api("/me/disability", { method: "PUT", body: { enabled: true } });
      state.role = "priority_citizen";
      try { localStorage.setItem("uce_role", state.role); } catch (e) { /* ignore */ }
      renderRole(); renderAccount();
      if (state.pos) loadPlan({ fit: true });
      openDis(2);
    } catch (e) { toast(t("common.error"), "error"); }
  }

  async function uploadCertificate() {
    const f = $("disFile").files[0];
    const msg = $("disMsg");
    if (!f) { $("disFile").click(); return; }
    const okType = ["application/pdf", "image/jpeg", "image/png"].includes(f.type);
    if (!okType) { msg.textContent = t("dis.err_type"); msg.className = "dis-msg is-err"; return; }
    if (f.size > 5 * 1024 * 1024) { msg.textContent = t("dis.err_size"); msg.className = "dis-msg is-err"; return; }
    msg.textContent = t("dis.uploading"); msg.className = "dis-msg";
    $("disUpload").disabled = true;
    try {
      const res = await fetch("/api/v1/me/disability/certificate", {
        method: "POST", body: f, headers: { "Content-Type": f.type, Authorization: "Bearer " + state.ctoken },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        msg.textContent = t(res.status === 413 ? "dis.err_size" : res.status === 415 ? "dis.err_type" : "common.error");
        msg.className = "dis-msg is-err";
        return;
      }
      state.account = data;
      renderAccount();
      msg.textContent = t(data.disability_status === "verified" ? "dis.done_verified" : "dis.done_pending");
      msg.className = "dis-msg is-ok";
      setTimeout(closeDis, 2200);
    } catch (e) {
      msg.textContent = t("common.error"); msg.className = "dis-msg is-err";
    } finally {
      $("disUpload").disabled = false;
    }
  }

  function logout() {
    try {
      localStorage.removeItem("uce_ctoken");
      localStorage.removeItem("uce_cid");   // hesabdan çıxanda yeni anonim cihaz ID-si yaradılır
      localStorage.removeItem("uce_role");
    } catch (e) { /* ignore */ }
    location.reload();
  }

  // ---------- Canlı hadisələr ----------
  const refreshPlan = debounce(() => loadPlan(), 400);
  function onEvent(name, data) {
    if (name === "reroute") applyPlan(data.plan, {});
    else if (name === "shelter_update") {
      if (data.opened && data.shelter) {
        state.banners = state.banners.filter((b) => b.kind !== "opened");
        state.banners.push({ kind: "opened", title: t("plan.nearest"), text: t("reroute.new_shelter", { name: nm(data.shelter) }) });
        renderBanners();
      }
      refreshPlan();
    } else if (name === "road_update") { state.closures = data.closures || []; drawClosures(); refreshPlan(); }
    else if (name === "routing_status") { setDegraded(!!data.degraded); refreshPlan(); }
    else if (name === "sos_assigned" || name === "sos_resolved") {
      if (name === "sos_resolved") toast(t("sos.resolved"));
      loadCitizen();
    } else if (name === "account_update") { loadAccount(false);
    } else if (name === "reset") { state.banners = []; loadRoads(); loadCitizen(); if (isPriority()) saveRole("priority_citizen", true); else refreshPlan(); }
  }

  // ---------- Başlanğıc ----------
  function bind() {
    $("district").addEventListener("change", (e) => {
      const d = state.meta.districts.find((x) => x.id === e.target.value);
      if (d) { setPosition(d.lat, d.lon, false); }
    });
    $("gpsBtn").addEventListener("click", () => {
      if (!navigator.geolocation) { toast(t("loc.gps_fail"), "error"); return; }
      navigator.geolocation.getCurrentPosition(
        (p) => { $("district").value = ""; setPosition(p.coords.latitude, p.coords.longitude, true); },
        () => toast(t("loc.gps_fail"), "error"), { enableHighAccuracy: true, timeout: 8000 });
    });
    $("sosBtn").addEventListener("click", openSheet);
    $("sosCancel").addEventListener("click", closeSheet);
    $("sosSheet").addEventListener("click", (e) => { if (e.target === $("sosSheet")) closeSheet(); });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("sosSheet").hidden) closeSheet();
      if (!$("disModal").hidden) closeDis();
    });
    $("accountCard").addEventListener("click", (e) => {
      if (e.target.closest("#accLogout")) logout();
      if (e.target.closest("#accUpload")) openDis(2);
    });
    $("disSkip").addEventListener("click", skipDis);
    $("disConfirm").addEventListener("click", confirmDis);
    $("disLater").addEventListener("click", closeDis);
    $("disUpload").addEventListener("click", uploadCertificate);
    $("disFile").addEventListener("change", () => {
      const f = $("disFile").files[0];
      $("disFileName").textContent = f ? f.name + " · " + Math.ceil(f.size / 1024) + " KB" : "PDF · JPG · PNG";
      $("disMsg").textContent = "";
    });
    $("sosGrid").addEventListener("click", (e) => { const b = e.target.closest("[data-sos]"); if (b) sendSos(b.dataset.sos); });
    $("safeBtn").addEventListener("click", () => markSafe(null));
    $("roleToggle").addEventListener("change", (e) => saveRole(e.target.checked ? "priority_citizen" : "citizen"));
    $("planBox").addEventListener("click", (e) => {
      const item = e.target.closest("[data-shelter]");
      if (item) return selectShelter(item.dataset.shelter);
      if (e.target.closest("#arriveBtn")) return markSafe(state.selectedId);
      if (e.target.closest("#retryPlan")) return loadPlan({ fit: true });
    });
    $("statusCard").addEventListener("click", (e) => { if (e.target.closest("#undoSafe")) undoSafe(); });
    $("banners").addEventListener("click", (e) => {
      const b = e.target.closest("[data-dismiss]");
      if (b) { state.banners.splice(Number(b.dataset.dismiss), 1); renderBanners(); }
    });
    window.I18N.onChange(() => { renderAccount(); if (!$("disModal").hidden && state.account) $("disText").textContent = t("dis.text", { group: state.account.mygov_disability_group || "—" }); fillDistricts(); renderLegend(); renderConn(); renderAll(false); drawClosures(); drawBarriers(); });
  }

  async function start() {
    $("brandMark").innerHTML = ICON.shield;
    $("icoLoc").innerHTML = ICON.pin;
    $("icoGps").innerHTML = ICON.gps;
    $("icoSafe").innerHTML = ICON.check;
    $("icoWheel").innerHTML = ICON.wheelchair;
    $("icoWheel2").innerHTML = ICON.wheelchair;
    $("icoWheel3").innerHTML = ICON.wheelchair;
    handleAuthHash();
    renderConn();
    try {
      state.meta = await api("/meta");
    } catch (e) {
      $("planBox").innerHTML = '<div class="card empty">' + esc(t("conn.offline")) + '<br><button class="btn btn-sm" type="button" id="reloadBtn">' + esc(t("common.retry")) + "</button></div>";
      $("planBox").addEventListener("click", (ev) => { if (ev.target.closest("#reloadBtn")) location.reload(); });
      return;
    }
    initMap(); fillDistricts(); renderLegend(); bind(); renderPlan(); renderRole();
    renderAccount();
    restorePosition();
    if (state.ctoken) await loadAccount(true);
    // Server bazası sıfırlansa belə rol itməsin: saxlanılmış rol serverə yenidən yazılır
    if (isPriority()) saveRole("priority_citizen", true);
    loadRoads(); loadCitizen();
    connectStream("/api/v1/stream?citizen_id=" + state.cid + (state.ctoken ? "&token=" + encodeURIComponent(state.ctoken) : ""), onEvent, (s) => { state.conn = s; renderConn(); if (s === "live" && state.pos) refreshPlan(); });
    // SSE kəsilsə belə məlumat köhnəlməsin
    setInterval(() => { if (state.conn !== "live" && state.pos) loadPlan(); }, 8000);
  }

  document.addEventListener("DOMContentLoaded", start);
})();

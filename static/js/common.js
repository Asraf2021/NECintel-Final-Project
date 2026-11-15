/* Ortaq köməkçilər: API müştərisi, canlı axın (SSE), format, ikonlar, xəritə. */
(function () {
  const API = "/api/v1";
  const t = (k, p) => window.I18N.t(k, p);

  // XSS-ə qarşı: serverdən gələn bütün mətn DOM-a yalnız bu funksiyadan keçərək yazılır
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    if (opts.token) headers.Authorization = "Bearer " + opts.token;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opts.timeout || 12000);
    try {
      const res = await fetch(API + path, {
        method: opts.method || "GET", headers, signal: ctrl.signal,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* boş cavab */ }
      if (!res.ok) {
        const err = new Error((data && data.detail) || "http_" + res.status);
        err.status = res.status;
        throw err;
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  // SSE: brauzer özü yenidən qoşulur; vəziyyət dəyişikliyi onState ilə bildirilir
  function connectStream(url, onEvent, onState) {
    let es = null;
    function open() {
      es = new EventSource(url);
      es.onopen = () => onState && onState("live");
      es.onerror = () => onState && onState(es.readyState === 2 ? "offline" : "reconnect");
      const names = ["hello", "shelter_update", "road_update", "reroute", "routing_status", "sos_new", "sos_update",
        "sos_assigned", "sos_resolved", "sos_priority", "verification_update", "account_update", "flood_status", "alert_new", "alert_update", "brigade_update", "citizen_status",
        "reroute_batch", "reset", "broadcast"];
      names.forEach((name) => es.addEventListener(name, (ev) => {
        let payload = {};
        try { payload = JSON.parse(ev.data); } catch (e) { return; }
        onEvent(name, payload.data || {}, payload);
      }));
    }
    open();
    return { close: () => es && es.close() };
  }

  function fmtDist(m) {
    if (m == null) return "—";
    return m >= 1000 ? (m / 1000).toFixed(1) + " " + t("plan.km") : Math.round(m / 10) * 10 + " " + t("plan.m");
  }
  function fmtMin(min) { return Math.max(1, Math.round(min)) + " " + t("plan.min"); }
  function fmtNum(n) { return Number(n || 0).toLocaleString("az-AZ").replace(/,/g, " "); }
  function timeAgo(ts) {
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 10) return t("time.now");
    if (s < 60) return t("time.s", { n: s });
    if (s < 3600) return t("time.m", { n: Math.floor(s / 60) });
    return t("time.h", { n: Math.floor(s / 3600) });
  }
  function debounce(fn, ms) {
    let timer = null;
    return function () { clearTimeout(timer); timer = setTimeout(fn, ms); };
  }

  const LEVEL_COLOR = { low: "#1FA971", medium: "#8DB600", high: "#F2A20C", full: "#E5553B", critical: "#B3122E", reserve: "#8A94A6" };
  function levelColor(shelter) {
    return shelter.status !== "open" ? LEVEL_COLOR.reserve : (LEVEL_COLOR[shelter.level] || LEVEL_COLOR.low);
  }

  const svg = (d, extra) => '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ' + (extra || "") + ">" + d + "</svg>";
  const ICON = {
    fire: svg('<path d="M12 3c1 3 4 4.5 4 8.5a4 4 0 0 1-8 0c0-1.5.6-2.6 1.3-3.4C9.2 9.6 10.5 8 12 3z"/><path d="M12 21a6.5 6.5 0 0 0 6.5-6.5"/><path d="M5.5 14.5A6.5 6.5 0 0 0 12 21"/>'),
    collapse: svg('<path d="M4 21V9l6-4v16"/><path d="M10 21V11l5 2v8"/><path d="M15 21v-5l5 1.5V21"/><path d="M2 21h20"/><path d="M14 4l2 2-1 2 2 1"/>'),
    flood: svg('<path d="M2 16c2 0 2-1.5 4-1.5S8 16 10 16s2-1.5 4-1.5 2 1.5 4 1.5 2-1.5 4-1.5"/><path d="M2 20c2 0 2-1.5 4-1.5S8 20 10 20s2-1.5 4-1.5 2 1.5 4 1.5 2-1.5 4-1.5"/><path d="M6 11l6-6 6 6"/><path d="M8 11V9.5M16 11V9.5"/>'),
    medical: svg('<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M12 7.5v9M7.5 12h9"/>'),
    shelter: svg('<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>'),
    park: svg('<path d="M12 3a5 5 0 0 0-4.6 7A4.5 4.5 0 0 0 9 18h6a4.5 4.5 0 0 0 1.6-8A5 5 0 0 0 12 3z"/><path d="M12 13v8"/>'),
    stadium: svg('<ellipse cx="12" cy="12" rx="9" ry="6"/><ellipse cx="12" cy="12" rx="4.5" ry="2.5"/><path d="M3 12v3c0 3.3 4 6 9 6s9-2.7 9-6v-3"/>'),
    brigade: svg('<path d="M2 16V7h11v9"/><path d="M13 10h4l4 3.5V16h-8"/><circle cx="6.5" cy="17.5" r="2"/><circle cx="17" cy="17.5" r="2"/><path d="M6 11h3M7.5 9.5v3"/>'),
    check: svg('<path d="M4 12.5l5 5L20 6.5"/>'),
    alert: svg('<path d="M12 3L2 20h20L12 3z"/><path d="M12 10v4.5"/><circle cx="12" cy="17.3" r=".6" fill="currentColor"/>'),
    pin: svg('<path d="M12 21s7-6.2 7-11.5A7 7 0 0 0 5 9.5C5 14.800 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>'),
    gps: svg('<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.2" fill="currentColor"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>'),
    route: svg('<circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M8.5 18H14a3.5 3.5 0 0 0 0-7h-4a3.5 3.5 0 0 1 0-7h5.500"/>'),
    walk: svg('<circle cx="13" cy="4.5" r="1.800"/><path d="M9 21l2.500-6.500L9.500 12l1.500-4.500 3 2.500 3 1"/><path d="M11 7.500l-3 1.500-1 3.500"/><path d="M13.500 15l2 6"/>'),
    close: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
    road: svg('<path d="M7 21L9 3M17 21L15 3"/><path d="M12 5v2.500M12 11v2.500M12 17v2.500"/>'),
    users: svg('<circle cx="9" cy="8" r="3.200"/><path d="M3 20c0-3.300 2.700-5.500 6-5.500s6 2.200 6 5.500"/><circle cx="17" cy="9" r="2.500"/><path d="M16.500 14.500c2.600.200 4.500 2 4.500 4.500"/>'),
    shield: svg('<path d="M12 3l8 3v6c0 4.500-3.300 8-8 9-4.700-1-8-4.500-8-9V6l8-3z"/><path d="M9 12l2.200 2.200L15.500 10"/>'),
    signal: svg('<path d="M5 19a10 10 0 0 1 0-14"/><path d="M19 5a10 10 0 0 1 0 14"/><path d="M8.500 15.500a5 5 0 0 1 0-7"/><path d="M15.500 8.500a5 5 0 0 1 0 7"/><circle cx="12" cy="12" r="1.300" fill="currentColor"/>'),
    play: svg('<path d="M7 4.500v15l12-7.500-12-7.500z"/>'),
    logout: svg('<path d="M10 4H5v16h5"/><path d="M14 8l4 4-4 4"/><path d="M18 12H9"/>'),
    wheelchair: svg('<circle cx="11" cy="4" r="1.800"/><path d="M11 7v6h5l2.500 5"/><path d="M11 10h4.500"/><path d="M8.200 10.300A5.500 5.500 0 1 0 15.300 18"/>'),
    home: svg('<path d="M3 11.500l9-7.500 9 7.500"/><path d="M6 10v10h12V10"/>'),
  };
  const TYPE_ICON = { shelter: "shelter", park: "park", stadium: "stadium" };

  // Leaflet xəritəsi: plitələr (tiles) internetdən gəlir; internet olmasa da vektor qatlar (yollar, məntəqələr) işləyir
  function createMap(elId, center, zoom, dark) {
    const map = L.map(elId, { zoomControl: false, attributionControl: true }).setView(center, zoom);
    L.control.zoom({ position: "topright" }).addTo(map);
    // Plitə mənbələri (API açarı tələb etmir). OSM-in istifadə siyasəti Referer başlığı tələb edir:
    // plitə sorğularında yalnız saytın mənşəyi (origin) göndərilir — səhifə yolu deyil.
    const cls = "";  // tünd tema plitələri CSS ilə tündləşdirir (html[data-theme="dark"])
    const bases = {
      "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, className: cls, referrerPolicy: "strict-origin-when-cross-origin",
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }),
      "Esri": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
        maxZoom: 19, className: cls, attribution: "Tiles &copy; Esri",
      }),
    };
    let chosen = "OpenStreetMap";
    try { chosen = localStorage.getItem("uce_tiles") || "OpenStreetMap"; } catch (e) { /* ignore */ }
    (bases[chosen] || bases.OpenStreetMap).addTo(map);
    // Mənbə əlçatmaz olsa (məs. şəbəkə bloklayır) — xəritənin sağ yuxarı küncündən bir kliklə dəyişmək olar
    L.control.layers(bases, null, { position: "topright", collapsed: true }).addTo(map);
    map.on("baselayerchange", (e) => { try { localStorage.setItem("uce_tiles", e.name); } catch (err) { /* ignore */ } });
    return map;
  }

  function shelterIcon(s, selected) {
    const color = levelColor(s);
    const label = s.status === "open" ? Math.round(s.occupancy_pct) + "%" : "";
    return L.divIcon({
      className: "",
      html: '<div class="mk-shelter' + (selected ? " is-selected" : "") + (s.status !== "open" ? " is-reserve" : "") +
        '" style="--c:' + color + '"><span class="mk-ico">' + ICON[TYPE_ICON[s.type] || "shelter"] + "</span>" +
        (label ? '<span class="mk-pct">' + label + "</span>" : "") + "</div>",
      iconSize: [44, 30], iconAnchor: [22, 15],
    });
  }

  // Məlumatın (məntəqə, briqada, rayon adı) seçilmiş dildə adı: name_en varsa ingilis dilində göstərilir
  function nm(obj) {
    if (!obj) return "";
    return window.I18N.lang === "en" && obj.name_en ? obj.name_en : obj.name;
  }

  // Time-to-Capacity proqnozunun qısa mətni
  function fcText(fc) {
    if (!fc) return { main: "", sub: "", level: "none" };
    if (fc.trend === "full") return { main: t("fc.full"), sub: "", level: "full" };
    if (fc.trend === "rising" && fc.t90_min != null) {
      const main = fc.t90_min > 0 ? t("fc.t90", { min: Math.round(fc.t90_min) }) : t("fc.t100", { min: Math.round(fc.t100_min) });
      const range = fc.t90_min > 0 && fc.t90_range ? t("fc.range", { a: fc.t90_range[0], b: fc.t90_range[1] }) : "";
      const sub = [range, t("fc.rate", { r: fc.rate_per_min }), t("fc.conf." + fc.confidence)].filter(Boolean).join(" · ");
      const soon = (fc.t90_min > 0 ? fc.t90_min : fc.t100_min) <= 15;
      return { main, sub, level: soon ? "soon" : "rising" };
    }
    if (fc.trend === "stable") return { main: t("fc.stable"), sub: "", level: "stable" };
    if (fc.trend === "falling") return { main: t("fc.falling"), sub: "", level: "stable" };
    return { main: t("fc.nodata"), sub: "", level: "none" };
  }

  function toast(message, kind) {
    let box = document.getElementById("toasts");
    if (!box) {
      box = document.createElement("div");
      box.id = "toasts";
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " toast-" + kind : "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => { el.classList.add("is-out"); setTimeout(() => el.remove(), 300); }, 4200);
  }

  // ----- Tema: açıq / tünd. Seçim yadda saxlanılır; səhifənin standart teması <body data-default-theme> -----
  const SUN = svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>');
  const MOON = svg('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>');
  function currentTheme() { return document.documentElement.dataset.theme || "light"; }
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "dark" ? "#0A121C" : "#0B1F33";
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      b.innerHTML = theme === "dark" ? SUN : MOON;
      const label = t(theme === "dark" ? "theme.light" : "theme.dark");
      b.title = label; b.setAttribute("aria-label", label);
    });
  }
  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("uce_theme"); } catch (e) { /* ignore */ }
    applyTheme(saved || document.body.dataset.defaultTheme || "light");
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      try { localStorage.setItem("uce_theme", next); } catch (e) { /* ignore */ }
      applyTheme(next);
    }));
    window.I18N.onChange(() => applyTheme(currentTheme()));
  }
  document.addEventListener("DOMContentLoaded", initTheme);

  window.UCE = { fcText, nm, api, esc, connectStream, fmtDist, fmtMin, fmtNum, timeAgo, debounce, levelColor, LEVEL_COLOR,
    ICON, TYPE_ICON, createMap, shelterIcon, toast, t };
})();

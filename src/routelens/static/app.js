/* RouteLens dashboard. Vanilla JS, no build step, no network beyond its own API.
   All DOM is built with textContent, never innerHTML: prompt previews are untrusted. */
(() => {
  "use strict";
  const BASE = "/routelens";
  const $app = document.getElementById("app");
  const $tip = document.getElementById("tip");
  const WINDOWS = ["1h", "6h", "24h", "7d", "14d"];

  const store = {
    get(k, s) { try { return (s ? sessionStorage : localStorage).getItem(k); } catch (e) { return null; } },
    set(k, v, s) { try { (s ? sessionStorage : localStorage).setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const state = {
    window: WINDOWS.includes(store.get("routelens-window")) ? store.get("routelens-window") : "24h",
    token: store.get("routelens-token", true) || "",
    providers: [], opened: new Map(), lastKey: "", updated: 0, healthy: true, shell: null, q: "",
  };

  /* ---------------------------------------------------------------- DOM helpers */
  const SVGNS = "http://www.w3.org/2000/svg";
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    apply(el, props, kids);
    return el;
  }
  function sv(tag, props, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    apply(el, props, kids);
    return el;
  }
  function apply(el, props, kids) {
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.setAttribute("class", v);
      else if (k === "text") el.textContent = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
  }
  const ICONS = {
    check: "M4 8.5l3 3 5-6.5", x: "M4.5 4.5l7 7M11.5 4.5l-7 7", alert: "M8 2.5L14.2 13H1.8L8 2.5zM8 6.5v3M8 11.2v.1",
    chevron: "M6 3.5l4.5 4.5L6 12.5", arrow: "M3 8h10M9.5 4.5L13 8l-3.5 3.5", back: "M13 8H3M6.5 4.5L3 8l3.5 3.5",
    search: "M7 12a5 5 0 100-10 5 5 0 000 10zM14 14l-3.5-3.5", sun: "M8 11a3 3 0 100-6 3 3 0 000 6zM8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1",
    moon: "M13.5 9.5A5.5 5.5 0 116.5 2.5a4.5 4.5 0 007 7z",
  };
  function icon(name, size = 14) {
    return sv("svg", { width: size, height: size, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor",
      "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
      sv("path", { d: ICONS[name] }));
  }

  /* ---------------------------------------------------------------- formatting */
  const nf = new Intl.NumberFormat();
  function compact(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "K" : nf.format(n); }
  function money(n) { n = n || 0; return n === 0 ? "$0" : n < 0.01 ? "$" + n.toFixed(4) : "$" + n.toFixed(n < 10 ? 3 : 2); }
  function ms(n) { return n == null ? "n/a" : n < 1000 ? Math.round(n) + " ms" : (n / 1000).toFixed(n < 10000 ? 2 : 1) + " s"; }
  function clock(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  function ago(ts) {
    const d = Math.max(0, Date.now() / 1000 - ts);
    return d < 60 ? Math.round(d) + "s ago" : d < 3600 ? Math.round(d / 60) + "m ago" : d < 86400 ? Math.round(d / 3600) + "h ago" : Math.round(d / 86400) + "d ago";
  }
  function span(secs) { return secs < 90 ? Math.round(secs) + "s" : secs < 5400 ? Math.round(secs / 60) + " min" : (secs / 3600).toFixed(1) + " h"; }
  function shortId(id) { return id.length > 22 ? id.slice(0, 20) + "…" : id; }
  const KINDS = {
    complexity: ["Complexity", "The complexity router scored the prompt and picked a tier"],
    fallback: ["Fallback", "A previous model group failed, so the config sent the request to another"],
    retry: ["Retry", "The same group was tried again after an error"],
    only_option: ["Only option", "No alternatives were eligible"],
    shuffle: ["Shuffle", "simple-shuffle: random pick among eligible deployments"],
    latency: ["Latency", "latency-based-routing: fastest recent deployment"],
    cost: ["Cost", "cost-based-routing: cheapest deployment"],
    usage: ["Usage", "usage-based-routing: lowest token/request usage"],
    least_busy: ["Least busy", "least-busy: fewest in-flight requests"],
    strategy: ["Strategy", "Chosen by the router's configured strategy"],
    direct: ["Direct", "Called without a model group"],
  };
  // Reasons use `backticks` for model/group names; render them as <code> (DOM only, never innerHTML).
  const rich = (text) => String(text == null ? "" : text).split("`").map((part, i) => (i % 2 ? h("code", { text: part }) : part));
  const plain = (text) => String(text == null ? "" : text).replace(/`/g, "");
  const kindLabel = (k) => (KINDS[k] || [k || "unknown"])[0];

  /* ---------------------------------------------------------------- colour: follows the provider, never its rank */
  function provColor(p) {
    let i = state.providers.indexOf(p);
    if (i < 0) { state.providers.push(p); i = state.providers.length - 1; }  // first-seen order, matches the server
    return i < 8 ? "var(--s" + (i + 1) + ")" : "var(--other)";
  }
  const dotFor = (p) => h("span", { class: "dot", style: { background: provColor(p) }, title: p });
  function modelChip(provider, model, showProv) {
    return h("span", { class: "model" }, dotFor(provider), h("span", { class: "name", title: model }, model),
      showProv ? h("span", { class: "prov" }, provider) : null);
  }
  function statusBadge(status, label) {
    const ok = status === "success";
    return h("span", { class: "status " + (ok ? "ok" : "fail") }, icon(ok ? "check" : "x", 13), label || (ok ? "Succeeded" : "Failed"));
  }

  /* ---------------------------------------------------------------- tooltip */
  function attachTip(el, build) {
    const show = (e) => {
      const c = build();
      if (!c) return;
      $tip.replaceChildren(c);
      $tip.hidden = false;
      const r = $tip.getBoundingClientRect();
      let x = e.clientX + 14, y = e.clientY + 14;
      if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
      if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
      $tip.style.left = Math.max(8, x) + "px"; $tip.style.top = Math.max(8, y) + "px";
    };
    el.addEventListener("mousemove", show);
    el.addEventListener("mouseenter", show);
    el.addEventListener("mouseleave", () => { $tip.hidden = true; });
    el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show({ clientX: r.left, clientY: r.bottom }); });
    el.addEventListener("blur", () => { $tip.hidden = true; });
  }
  const tipRow = (k, v) => h("div", null, h("span", { class: "k" }, k + " "), v);

  /* ---------------------------------------------------------------- API */
  class AuthError extends Error {}
  async function api(path) {
    const res = await fetch(BASE + "/api" + path, { headers: state.token ? { Authorization: "Bearer " + state.token } : {} });
    if (res.status === 401) throw new AuthError("auth");
    if (!res.ok) throw new Error(res.status + " " + res.statusText);
    return res.json();
  }

  /* ---------------------------------------------------------------- shell */
  function buildShell() {
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Time window" },
      WINDOWS.map((w) => h("button", { "data-w": w, "aria-pressed": String(w === state.window), onclick: () => { state.window = w; store.set("routelens-window", w); state.lastKey = ""; syncShell(); refresh(); } }, w)));
    const themeBtn = h("button", { class: "iconbtn", title: "Toggle theme", "aria-label": "Toggle theme", onclick: toggleTheme });
    const dot = h("i", { class: "pulse" });
    const liveText = h("span", { text: "Connecting" });
    const nav = h("nav", { class: "nav" },
      h("a", { href: "#/", "data-nav": "overview", text: "Overview" }),
      h("a", { href: "#/sessions", "data-nav": "sessions", text: "Sessions" }),
      h("a", { href: "#/moderation", "data-nav": "moderation", text: "Moderation" }));
    const main = h("main", { id: "view" });
    $app.replaceChildren(
      h("header", { class: "top" },
        h("a", { class: "brand", href: "#/" }, h("img", { src: BASE + "/assets/logo.svg", alt: "" }), "RouteLens"),
        nav, h("div", { class: "spacer" }), seg, h("div", { class: "live", title: "Refreshes every 4 seconds" }, dot, liveText), themeBtn),
      main);
    state.shell = { seg, themeBtn, dot, liveText, nav, main };
    syncShell();
  }
  function isDark() {
    const t = document.documentElement.getAttribute("data-theme");
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function toggleTheme() {
    const next = isDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    store.set("routelens-theme", next);
    syncShell();
  }
  function syncShell() {
    const s = state.shell; if (!s) return;
    s.seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.w === state.window)));
    s.themeBtn.replaceChildren(icon(isDark() ? "sun" : "moon", 16));
    const route = parseRoute().name;
    s.nav.querySelectorAll("a").forEach((a) => {
      const on = (a.dataset.nav === "overview" && route === "overview")
        || (a.dataset.nav === "sessions" && (route === "sessions" || route === "session"))
        || (a.dataset.nav === "moderation" && route === "moderation");
      on ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current");
    });
    s.dot.className = "pulse" + (state.healthy ? "" : " off");
    s.liveText.textContent = state.healthy ? (state.updated ? "Live · " + ago(state.updated) : "Connecting") : "Disconnected";
  }

  /* ---------------------------------------------------------------- routing */
  function parseRoute() {
    const p = (location.hash || "#/").slice(1).split("/").filter(Boolean);
    if (p[0] === "sessions") return { name: "sessions" };
    if (p[0] === "session" && p[1]) return { name: "session", id: decodeURIComponent(p.slice(1).join("/")) };
    if (p[0] === "moderation") return { name: "moderation" };
    return { name: "overview" };
  }

  async function refresh(force) {
    if (!state.shell) buildShell();
    const route = parseRoute();
    syncShell();
    try {
      const meta = await api("/meta");
      state.providers = meta.providers;
      state.moderation = meta.moderation;
      let data, view;
      if (route.name === "session") { data = await api("/sessions/" + encodeURIComponent(route.id)); view = viewSession; }
      else if (route.name === "sessions") { data = await api("/sessions?window=" + state.window + "&limit=100" + (state.q ? "&q=" + encodeURIComponent(state.q) : "")); view = viewSessions; }
      else if (route.name === "moderation") {
        // Not gated on meta.moderation.enabled: that reflects the *live* callback's setting, but
        // `routelens serve` (standalone/demo mode) never knows that and would always report
        // false even when the database has real moderation history -- show data whenever
        // there's data, exactly like every other page here already does with an empty state.
        const chain = state.modChain || "all";
        const [o, r] = await Promise.all([
          api("/moderation/overview?window=" + state.window + "&chain=" + chain),
          api("/moderation/recent?limit=14&chain=" + chain)]);
        data = { o, r, chain, mode: meta.moderation.mode }; view = viewModeration;
      }
      else { const [o, r] = await Promise.all([api("/overview?window=" + state.window), api("/recent?limit=10")]); data = { o, r }; view = viewOverview; }
      state.healthy = true; state.updated = Date.now() / 1000;
      const key = route.name + ":" + state.window + ":" + state.q + ":" + JSON.stringify(data);
      if (force || key !== state.lastKey) {
        state.lastKey = key;
        const y = scrollY;
        state.shell.main.replaceChildren(view(data, route));
        if (!force && route.name === state.lastRoute) scrollTo(0, y);
        state.lastRoute = route.name;
      }
    } catch (e) {
      if (e instanceof AuthError) return viewLogin(state.token ? "That token was rejected." : "");
      state.healthy = false;
      if (!state.shell.main.firstChild) state.shell.main.replaceChildren(h("div", { class: "card empty" }, h("h2", { text: "Can't reach the RouteLens API" }), h("p", { text: String(e.message || e) })));
    }
    syncShell();
  }

  function viewLogin(msg) {
    const input = h("input", { type: "password", placeholder: "LiteLLM master key or ROUTELENS_TOKEN", autocomplete: "off", "aria-label": "Access token" });
    const go = () => { state.token = input.value.trim(); store.set("routelens-token", state.token, true); state.lastKey = ""; refresh(true); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    state.shell.main.replaceChildren(h("div", { class: "card login" },
      h("h1", { text: "Sign in" }),
      h("p", { class: "ink2", text: "This RouteLens instance is protected. Use your LiteLLM master key, or the ROUTELENS_TOKEN you configured." }),
      input, msg ? h("p", { class: "err-text", text: msg }) : null, h("button", { class: "btn", onclick: go, text: "Continue" })));
    input.focus();
  }

  function emptyState() {
    return h("div", { class: "card empty" },
      h("h2", { text: "No routing decisions yet" }),
      h("p", null, "Send a request through your LiteLLM proxy and it will show up here within a couple of seconds."),
      h("p", { class: "muted" }, "Want to look around first? Run ", h("code", { text: "routelens demo --serve" }), "."));
  }

  /* ---------------------------------------------------------------- charts */
  function niceMax(v) {
    if (v <= 4) return Math.max(v, 1);
    const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }
  function roundedTop(x, y, w, hgt, r) {
    r = Math.min(r, w / 2, hgt);
    return "M" + x + "," + (y + hgt) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y + "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + hgt) + "Z";
  }
  function timeSeries(o, opts) {
    opts = opts || {};
    const badKey = opts.badKey || "failed", badLabel = opts.badLabel || "Failed", totalLabel = opts.totalLabel || "Calls";
    const W = 760, H = 230, m = { l: 40, r: 8, t: 10, b: 26 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    const step = o.bucket_s, start = Math.floor(o.since / step) * step, end = Math.floor(Date.now() / 1000 / step) * step;
    const n = Math.max(1, Math.round((end - start) / step) + 1);
    const byT = new Map(o.series.map((b) => [b.t, b]));
    const bins = Array.from({ length: n }, (_, i) => { const t = start + i * step, b = byT.get(t) || { n: 0 }; return { t, n: b.n, bad: b[badKey] || 0 }; });
    const top = niceMax(Math.max(1, ...bins.map((b) => b.n)));
    const slot = pw / n, bw = Math.min(24, Math.max(2, slot - 2));
    const y = (v) => m.t + ph - (v / top) * ph;
    const svg = sv("svg", { class: "chart", viewBox: "0 0 " + W + " " + H, width: "100%", role: "img", "aria-label": totalLabel + " over time, with " + badLabel.toLowerCase() });
    for (const f of [0, 0.5, 1]) {
      const v = top * f;
      svg.append(sv("line", { class: f === 0 ? "axisline" : "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }),
        sv("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, compact(Math.round(v))));
    }
    const fmt = (t) => { const d = new Date(t * 1000); return step >= 3600 * 6 ? d.toLocaleDateString([], { weekday: "short", day: "numeric" }) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
    const every = Math.max(1, Math.ceil(n / 6));
    bins.forEach((b, i) => {
      const cx = m.l + slot * i + slot / 2, x = cx - bw / 2;
      const okN = b.n - b.bad, hOk = (okN / top) * ph, hBad = (b.bad / top) * ph;
      const g = sv("g");
      if (okN > 0) g.append(sv("path", { d: roundedTop(x, y(okN), bw, Math.max(1, hOk), b.bad ? 0.01 : 4), fill: "var(--bar)", opacity: 0.55, class: "col" }));
      if (b.bad > 0) g.append(sv("path", { d: roundedTop(x, y(b.n) , bw, Math.max(2, hBad), 4), fill: "var(--critical)", transform: okN > 0 ? "translate(0,-2)" : null }));
      const hit = sv("rect", { x: m.l + slot * i, y: m.t, width: slot, height: ph, fill: "transparent", tabindex: 0, "aria-label": fmt(b.t) + ": " + b.n + " " + totalLabel.toLowerCase() + ", " + b.bad + " " + badLabel.toLowerCase() });
      attachTip(hit, () => h("div", null, h("b", { text: fmt(b.t) }), tipRow(totalLabel, h("b", { text: nf.format(b.n) })), tipRow(badLabel, h("b", { text: nf.format(b.bad) }))));
      g.append(hit); svg.append(g);
      if (i % every === 0) svg.append(sv("text", { x: cx, y: H - 6, "text-anchor": "middle" }, fmt(b.t)));
    });
    return svg;
  }

  function barRows(items, valueFn, labelFn, colorFn, cls) {
    const max = Math.max(1, ...items.map((i) => i.n));
    const total = items.reduce((a, i) => a + i.n, 0) || 1;
    return h("div", { class: "bars " + (cls || "") },
      items.map((it) => {
        const fill = h("div", { class: "fill", style: { width: Math.max(1, (it.n / max) * 100) + "%", background: colorFn(it) } });
        const row = h("div", { class: "row" }, labelFn(it), h("div", { class: "track" }, fill), h("div", { class: "val tnum", text: valueFn(it, total) }));
        return row;
      }));
  }

  /* ---------------------------------------------------------------- overview */
  function tile(label, value, sub, hero) {
    return h("div", { class: "card tile" + (hero ? " hero" : "") }, h("div", { class: "label", text: label }), h("div", { class: "value" }, value), h("div", { class: "sub" }, sub));
  }
  function viewOverview({ o, r }) {
    const t = o.totals;
    if (!t.attempts) return emptyState();
    const pct = t.requests ? (t.rerouted / t.requests) * 100 : 0;
    const models = o.by_model.map((m) => Object.assign({ label: m.model }, m));
    const legendProv = [...new Set(models.map((m) => m.provider))];
    const reasons = o.by_reason.map((x) => ({ n: x.n, kind: x.kind }));
    const feed = h("ul", { class: "feed" }, r.attempts.map((a) =>
      h("li", { tabindex: 0, role: "link", onclick: () => go("#/session/" + encodeURIComponent(a.session_id)), onkeydown: (e) => { if (e.key === "Enter") go("#/session/" + encodeURIComponent(a.session_id)); } },
        h("div", { class: "t tnum mono", text: clock(a.ts) }),
        h("div", null,
          h("div", { class: "line" }, modelChip(a.provider, a.model, false), statusBadge(a.status), h("span", { class: "chip kind", text: kindLabel(a.reason_kind) }),
            h("span", { class: "muted mono", text: shortId(a.session_id) })),
          h("div", { class: "why" }, rich(a.reason))))));
    return h("div", null,
      h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "Overview" }), h("p", { text: "Every model call your proxy routed, and why." }))),
      h("div", { class: "grid g-tiles" },
        tile("Requests", compact(t.requests), t.sessions + " sessions · " + compact(t.attempts) + " model calls", true),
        tile("Rerouted", pct.toFixed(pct < 10 ? 1 : 0) + "%", nf.format(t.rerouted) + " needed a fallback or retry"),
        tile("Median latency", ms(t.p50_ms), "p95 " + ms(t.p95_ms)),
        tile("Spend", money(t.cost), t.requests ? money(t.cost / t.requests) + " per request" : ""),
        tile("Provider errors", nf.format(t.failed_attempts), t.attempts ? ((t.failed_attempts / t.attempts) * 100).toFixed(1) + "% of calls" : "")),
      h("div", { class: "grid g-2", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("header", null, h("h2", { text: "Model calls over time" }),
          h("div", { class: "legend" }, h("span", null, h("i", { class: "dot", style: { background: "var(--bar)", opacity: 0.55 } }), "Succeeded"), h("span", null, h("i", { class: "dot", style: { background: "var(--critical)" } }), "Failed"))),
          h("div", { class: "body" }, timeSeries(o))),
        h("div", { class: "card" }, h("header", null, h("h2", { text: "Why decisions were made" })),
          h("div", { class: "body" }, barRows(reasons, (it, total) => Math.round((it.n / total) * 100) + "%",
            (it) => h("span", { title: (KINDS[it.kind] || [])[1] || "" }, kindLabel(it.kind)), () => "var(--bar)", "plain")))),
      h("div", { class: "grid g-2b", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("header", null, h("h2", { text: "Where traffic went" }),
          h("div", { class: "legend" }, legendProv.map((p) => h("span", null, dotFor(p), p)))),
          h("div", { class: "body" }, barRows(models, (it, total) => nf.format(it.n) + " · " + Math.round((it.n / total) * 100) + "%",
            (it) => h("span", { class: "model" }, dotFor(it.provider), h("span", { class: "name", title: it.model, text: it.model })), (it) => provColor(it.provider)))),
        h("div", { class: "card" }, h("header", null, h("h2", { text: "Live decisions" }), h("a", { href: "#/sessions", class: "muted", text: "All sessions" })),
          h("div", { class: "body" }, feed))));
  }
  function go(hash) { location.hash = hash; }

  /* ---------------------------------------------------------------- moderation */
  const MOD_STATUS = {
    pass: ["ok", "check", "Passed"], blocked: ["fail", "x", "Blocked"], error: ["warn", "alert", "Check failed"],
  };
  function modStatusBadge(status) {
    const [cls, ic, label] = MOD_STATUS[status] || ["warn", "alert", status];
    return h("span", { class: "status " + cls }, icon(ic, 13), label);
  }
  function chainChip(chain) {
    return h("span", { class: "chip", style: { textTransform: "capitalize" }, text: chain });
  }

  function viewModeration(data, route) {
    const head = h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "Moderation" }),
      h("p", { text: "Every prompt and response checked against a moderation filter, and what happened to it." })),
      data.mode ? h("span", { class: "chip", title: "Set by ROUTELENS_MODERATION_MODE", text: "mode: " + data.mode }) : null);
    const { o, r } = data;
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Filter by chain" },
      ["all", "input", "output"].map((c) => h("button", {
        "aria-pressed": String(c === data.chain),
        onclick: () => { state.modChain = c; state.lastKey = ""; refresh(); },
      }, c === "all" ? "All" : c[0].toUpperCase() + c.slice(1))));
    const t = o.totals;
    if (!t.checked) {
      return h("div", null, head, seg, h("div", { class: "card empty", style: { marginTop: "16px" } },
        h("h2", { text: "No checks yet" }),
        h("p", { text: "Send a request through your proxy and moderation results will show up here." }),
        h("p", { class: "muted", style: { marginTop: "10px" } },
          "Moderation is off by default -- set ", h("code", { text: "ROUTELENS_MODERATION=1" }),
          " and an API key (", h("code", { text: "OPENAI_API_KEY" }), " or ",
          h("code", { text: "ROUTELENS_MODERATION_API_KEY" }), ") to turn it on. Try ",
          h("code", { text: "ROUTELENS_MODERATION_MODE=observe" }),
          " first to see what it would catch before anything is actually blocked.")));
    }
    const feed = h("ul", { class: "feed" }, r.events.map((e) =>
      h("li", { tabindex: 0, role: "link", onclick: () => go("#/session/" + encodeURIComponent(e.session_id)),
                onkeydown: (ev) => { if (ev.key === "Enter") go("#/session/" + encodeURIComponent(e.session_id)); } },
        h("div", { class: "t tnum mono", text: clock(e.ts) }),
        h("div", null,
          h("div", { class: "line" }, chainChip(e.chain), modStatusBadge(e.status),
            e.categories && e.categories.length ? e.categories.map((c) => h("span", { class: "chip", text: c })) : null,
            h("span", { class: "muted mono", text: shortId(e.session_id) })),
          e.preview ? h("div", { class: "why", text: "“" + e.preview + "”" }) : null))));
    return h("div", null, head,
      h("div", { style: { marginBottom: "16px" } }, seg),
      h("div", { class: "grid g-tiles" },
        tile("Checked", compact(t.checked), "across " + (data.chain === "all" ? "input + output" : data.chain), true),
        tile("Blocked", (t.block_rate * 100).toFixed(t.block_rate * 100 < 10 ? 1 : 0) + "%", nf.format(t.blocked) + " of " + nf.format(t.checked)),
        tile("Check failures", nf.format(t.errors), t.checked ? ((t.errors / t.checked) * 100).toFixed(1) + "% of checks" : ""),
        tile("Avg. check latency", ms(t.avg_latency_ms), "moderation API only")),
      h("div", { class: "grid g-2", style: { marginTop: "16px" } },
        h("div", { class: "card" }, h("header", null, h("h2", { text: "Checks over time" }),
          h("div", { class: "legend" }, h("span", null, h("i", { class: "dot", style: { background: "var(--bar)", opacity: 0.55 } }), "Passed"),
            h("span", null, h("i", { class: "dot", style: { background: "var(--critical)" } }), "Blocked"))),
          h("div", { class: "body" }, timeSeries(o, { badKey: "blocked", badLabel: "Blocked", totalLabel: "Checked" }))),
        h("div", { class: "card" }, h("header", null, h("h2", { text: "Blocked by category" })),
          h("div", { class: "body" }, o.by_category.length
            ? barRows(o.by_category.map((c) => ({ n: c.n, label: c.category })),
                (it, total) => nf.format(it.n), (it) => it.label, () => "var(--critical)", "plain")
            : h("p", { class: "muted", text: "Nothing blocked in this window." })))),
      h("div", { class: "card", style: { marginTop: "16px" } },
        h("header", null, h("h2", { text: "Recent checks" })), h("div", { class: "body" }, feed)));
  }

  /* ---------------------------------------------------------------- sessions list */
  function viewSessions({ sessions }) {
    const search = h("input", { type: "search", placeholder: "Search sessions, models, prompts", value: state.q, "aria-label": "Search sessions" });
    let timer;
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.q = search.value.trim(); state.lastKey = ""; refresh(); }, 250); });
    const head = h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "Sessions" }), h("p", { text: "Follow one conversation across turns and see when the model changed." })),
      h("label", { class: "search" }, icon("search", 15), search));
    if (!sessions.length) return h("div", null, head, state.q ? h("div", { class: "card empty" }, h("h2", { text: "No sessions match" })) : emptyState());
    const rows = sessions.map((s) => {
      const tr = h("tr", { tabindex: 0, onclick: () => go("#/session/" + encodeURIComponent(s.session_id)), onkeydown: (e) => { if (e.key === "Enter") go("#/session/" + encodeURIComponent(s.session_id)); } },
        h("td", null, h("div", { class: "mono", title: s.session_id, text: shortId(s.session_id) }), h("div", { class: "muted", style: { fontSize: "12px" }, text: s.session_source === "inferred" ? "grouped automatically" : s.session_source === "explicit" ? "session id from client" : "single request" })),
        h("td", null, ribbon(s.path, false)),
        h("td", { class: "num tnum", text: s.turns }),
        h("td", { class: "num tnum", text: s.switches }),
        h("td", { class: "num tnum" }, s.rerouted ? h("span", { class: "status warn" }, icon("alert", 13), s.rerouted) : h("span", { class: "muted", text: "0" })),
        h("td", { class: "num tnum", text: money(s.cost) }),
        h("td", { class: "num muted", text: ago(s.last_ts) }));
      return tr;
    });
    return h("div", null, head, h("div", { class: "card" }, h("div", { style: { overflowX: "auto" } }, h("table", { class: "tbl" },
      h("thead", null, h("tr", null, h("th", { text: "Session" }), h("th", { text: "Route by turn" }), h("th", { class: "num", text: "Turns" }), h("th", { class: "num", text: "Model switches" }), h("th", { class: "num", text: "Rerouted" }), h("th", { class: "num", text: "Spend" }), h("th", { class: "num", text: "Last active" }))),
      h("tbody", null, rows)))));
  }
  function ribbon(path, big, extra) {
    return h("div", { class: "ribbon" + (big ? " big" : "") }, path.map((p, i) => {
      const blocked = p.status === "blocked" && !p.model;  // blocked before a model was ever picked
      const seg = h("i", {
        style: { background: blocked ? "var(--critical)" : provColor(p.provider), opacity: p.status === "success" ? 1 : (blocked ? 0.85 : 0.4) },
        tabindex: big ? 0 : null, "aria-label": "Turn " + (i + 1) + ": " + (blocked ? "blocked" : p.model),
      });
      attachTip(seg, () => h("div", null, h("b", { text: "Turn " + (i + 1) }),
        blocked ? tipRow("Status", h("b", { text: "Blocked" })) : [tipRow("Model", p.model), tipRow("Provider", p.provider)],
        extra && extra[i] ? h("div", { class: "ink2", style: { marginTop: "4px" }, text: extra[i] }) : null));
      return seg;
    }));
  }

  /* ---------------------------------------------------------------- session detail */
  function viewSession(data) {
    const turns = data.turns;
    // A turn blocked before reaching a model has no `final` -- represent it in the ribbon the
    // same way store.sessions() already does, rather than crashing on t.final.provider.
    const path = turns.map((t) => t.final
      ? { provider: t.final.provider, model: t.final.model, status: t.status }
      : { provider: null, model: null, status: "blocked" });
    const switches = path.filter((p, i) => i && p.model !== path[i - 1].model).length;
    const blocked = turns.filter((t) => t.status === "blocked").length;
    const rerouted = turns.filter((t) => t.attempts.length > 1).length;
    const cost = turns.reduce((a, t) => a + (t.cost || 0), 0);
    const dur = turns.length > 1 ? turns[turns.length - 1].ts - turns[0].ts : 0;
    const provs = [...new Set(path.map((p) => p.provider).filter(Boolean))];
    const markers = h("div", { class: "ribbon big", style: { marginTop: "6px", minHeight: "16px" } }, turns.map((t) =>
      h("span", { style: { flex: "1 1 0", display: "flex", justifyContent: "center", color: "var(--serious)" }, title: t.attempts.length > 1 ? "Rerouted: " + t.attempts.length + " attempts" : null }, t.attempts.length > 1 ? icon("alert", 14) : null)));
    return h("div", null,
      h("a", { class: "back", href: "#/sessions" }, icon("back", 14), "All sessions"),
      h("div", { class: "pagehead" }, h("div", null, h("h1", { class: "mono", style: { fontSize: "19px" }, text: data.session_id }),
        h("div", { class: "metas" },
          h("span", { class: "chip", text: turns.length + (turns.length === 1 ? " turn" : " turns") }),
          h("span", { class: "chip", text: switches + (switches === 1 ? " model switch" : " model switches") }),
          h("span", { class: "chip", text: rerouted + " rerouted" }), h("span", { class: "chip", text: money(cost) + " spend" }),
          dur ? h("span", { class: "chip", text: span(dur) + " long" }) : null,
          blocked ? h("span", { class: "chip", style: { color: "var(--critical)" }, text: blocked + " blocked" }) : null,
          data.session_source === "inferred" ? h("span", { class: "chip", title: "No session id was sent. Turns were grouped by matching the first user message and API key. Send x-litellm-session-id for exact grouping.", text: "grouped automatically" }) : null))),
      h("div", { class: "card" }, h("header", null, h("h2", { text: "Route by turn" }),
        h("div", { class: "legend" }, provs.map((p) => h("span", null, dotFor(p), p)), rerouted ? h("span", { style: { color: "var(--ink-2)" } }, h("span", { style: { color: "var(--serious)", display: "inline-flex" } }, icon("alert", 13)), "Fallback or retry") : null)),
        h("div", { class: "body" }, ribbon(path, true, turns.map((t) => t.final ? plain(t.final.reason) : "Blocked by moderation")), markers,
          h("div", { class: "ribbon big", style: { marginTop: "4px", alignItems: "flex-start" } }, turns.map((t, i) => {
            const showModel = turns.length <= 12, switched = i > 0 && path[i - 1].model !== path[i].model;
            return h("span", { style: { flex: "1 1 0", minWidth: 0, textAlign: "center", fontSize: "11.5px", lineHeight: "1.35" } },
              h("div", { class: "muted tnum", text: turns.length > 24 && t.turn % 5 ? "" : "T" + t.turn }),
              showModel ? h("div", { class: switched ? "" : "muted", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: path[i].model ? (switched ? "var(--ink)" : null) : "var(--critical)", fontWeight: switched ? "600" : "400" }, title: path[i].model || "Blocked", text: path[i].model || "Blocked" }) : null);
          })))),
      h("div", { class: "turns" }, turns.map((t, i) => turnCard(t, i ? turns[i - 1] : null))));
  }

  function modCol(label, ev) {
    if (!ev) return h("div", { class: "col" }, h("h3", { text: label }),
      h("p", { class: "muted", style: { margin: 0, fontSize: "12.5px" } }, "not checked"));
    return h("div", { class: "col" }, h("h3", { text: label }), modStatusBadge(ev.status),
      ev.categories && ev.categories.length ? h("div", { class: "details", style: { marginTop: "6px" } },
        ev.categories.map((c) => h("span", { class: "chip", text: c }))) : null,
      ev.reason && ev.status !== "pass" ? h("div", { class: "why muted", style: { fontSize: "11.5px", marginTop: "4px" } }, ev.reason) : null,
      h("div", { class: "muted tnum", style: { fontSize: "11px", marginTop: "4px" }, text: ms(ev.latency_ms) }));
  }
  function moderationFlowRow(modIn, modelNode, modOut) {
    return h("div", { class: "flow" }, modCol("Input filter", modIn), h("div", { class: "arrow" }, icon("arrow", 16)),
      modelNode ? h("div", { class: "col" }, h("h3", { text: "Model" }), modelNode)
                : h("div", { class: "col" }, h("h3", { text: "Model" }), h("p", { class: "muted", style: { margin: 0, fontSize: "12.5px" } }, "never reached")),
      h("div", { class: "arrow" }, icon("arrow", 16)), modCol("Output filter", modOut));
  }

  function turnCard(t, prev) {
    const f = t.final, multi = t.attempts.length > 1;
    const modIn = t.moderation && t.moderation.input, modOut = t.moderation && t.moderation.output;
    const key = data_key(t);

    if (!f) {
      // Blocked before any model was ever called -- there is no routing decision to show, so
      // this renders a deliberately different (much shorter) card, not a half-filled normal one.
      return h("div", { class: "turn" },
        h("div", { class: "rail" }, h("div", { class: "n tnum", text: t.turn })),
        h("div", { class: "card" },
          h("div", { class: "head" }, h("span", { class: "status fail" }, icon("x", 13), "Blocked"),
            h("span", { class: "chip", text: "input moderation" }),
            h("div", { class: "stats tnum" }, h("span", { class: "muted", text: clock(t.ts) }))),
          t.preview ? h("blockquote", { class: "quote", text: t.preview }) : null,
          h("p", { class: "headline", text: (modIn && modIn.reason) || "Blocked before reaching a model" }),
          moderationFlowRow(modIn, null, null)));
    }

    const switched = prev && prev.final && prev.final.model !== f.model;
    const open = state.opened.has(key) ? state.opened.get(key) : (multi || switched);
    const det = h("details", { class: "path", open: open || null });
    det.addEventListener("toggle", () => { state.opened.set(key, det.open); });
    det.append(h("summary", null, icon("chevron", 13), "Decision path"), decisionPath(t));
    return h("div", { class: "turn" },
      h("div", { class: "rail" }, h("div", { class: "n tnum", text: t.turn })),
      h("div", { class: "card" },
        h("div", { class: "head" }, modelChip(f.provider, f.model, true), statusBadge(t.status, t.status === "blocked" ? "Blocked" : null),
          h("span", { class: "chip", title: (KINDS[f.reason_kind] || [])[1] || "", text: kindLabel(f.reason_kind) }),
          switched ? h("span", { class: "chip", title: "Different model than the previous turn" }, icon("arrow", 12), "from " + prev.final.model) : null,
          h("div", { class: "stats tnum" }, h("span", { text: ms(t.latency_ms) }), h("span", { text: nf.format(f.prompt_tokens) + " → " + nf.format(f.completion_tokens) + " tok" }), h("span", { text: money(t.cost) }), h("span", { class: "muted", text: clock(t.ts) }))),
        t.preview ? h("blockquote", { class: "quote", text: t.preview }) : null,
        h("p", { class: "headline" }, rich(f.reason)),
        f.reason_detail && f.reason_detail.length ? h("div", { class: "details" }, f.reason_detail.map((d) => h("span", { class: "chip", text: d }))) : null,
        (modIn || modOut) ? moderationFlowRow(modIn, modelChip(f.provider, f.model, false), modOut) : null,
        det));
  }
  const data_key = (t) => t.request_id;

  function decisionPath(t) {
    const f = t.final, box = h("div", null);
    if (t.attempts.length > 1) {
      box.append(h("div", { class: "attempts" }, t.attempts.map((a, i) =>
        h("div", { class: "attempt" },
          h("span", { class: "muted tnum", text: "Attempt " + (i + 1) }), modelChip(a.provider, a.model, false), statusBadge(a.status),
          h("span", { class: "muted", text: "group " + (a.model_group || "n/a") }),
          a.status === "failure" ? h("span", { class: "err", text: [a.error_class, a.error_code].filter(Boolean).join(" · ") }) : h("span", { class: "muted tnum", text: ms(a.latency_ms) })))));
    }
    const sig = f.signals;
    const rule = h("div", null,
      h("div", { class: "cand" }, h("b", { text: f.strategy || (f.reason_kind === "direct" ? "no router" : "router") })),
      sig ? h("div", { class: "cand" }, h("span", { class: "chip", text: "tier " + sig.tier }), h("span", { class: "muted tnum", text: "score " + Number(sig.score).toFixed(2) })) : null,
      sig && sig.signals && sig.signals.length ? h("div", { class: "why muted", style: { fontSize: "12px" }, text: sig.signals.join(" · ") }) : null,
      f.reason_kind === "fallback" || f.reason_kind === "retry" ? h("div", { class: "why muted", style: { fontSize: "12px" }, text: "Triggered by the previous attempt's failure" }) : null);
    const cands = (f.candidates || []).map((c) => h("div", { class: "cand" + (c.id === f.deployment_id ? " chosen" : "") },
      dotFor(c.provider), h("span", { class: "name", text: c.model }), c.id === f.deployment_id ? icon("check", 13) : null));
    const outs = (f.excluded || []).map((c) => h("div", { class: "cand out", title: c.why },
      dotFor(c.provider), h("span", null, h("span", { class: "name", text: c.model }), h("div", { class: "why", text: c.why }))));
    box.append(h("div", { class: "flow" },
      h("div", { class: "col" }, h("h3", { text: "Requested" }), h("div", { class: "cand" }, h("b", { text: f.requested_model || f.model_group || "n/a" })),
        f.model_group && f.model_group !== f.requested_model ? h("div", { class: "cand muted" }, icon("arrow", 12), "group " + f.model_group) : null),
      h("div", { class: "arrow" }, icon("arrow", 16)),
      h("div", { class: "col" }, h("h3", { text: "Rule" }), rule),
      h("div", { class: "arrow" }, icon("arrow", 16)),
      h("div", { class: "col" }, h("h3", { text: "Eligible" + (cands.length ? " (" + cands.length + ")" : "") }), cands, outs.length ? h("h3", { style: { marginTop: "8px" }, text: "Excluded (" + outs.length + ")" }) : null, outs)));
    return box;
  }

  /* ---------------------------------------------------------------- boot */
  addEventListener("hashchange", () => { state.lastKey = ""; state.q = ""; syncShell(); refresh(true); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncShell);
  buildShell();
  refresh(true);
  setInterval(() => { if (!document.hidden) refresh(); }, 4000);
  setInterval(syncShell, 1000);
})();

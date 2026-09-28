// Ripple read-only UI. Every number is fetched from the JSON API, which wraps the same library
// functions the CLI and MCP server use. Nothing here computes an analytic of its own.
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const state = { themes: [], health: null, calibration: null, calibrating: null };

// --- small helpers -----------------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function params() {
  return { as_of: $("#as-of").value, min_confidence: $("#min-conf").value };
}

function $(sel) {
  return document.querySelector(sel);
}

async function api(path, query = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...params(), ...query })) {
    if (v !== "" && v !== null && v !== undefined) q.set(k, v);
  }
  const response = await fetch(`${path}?${q}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function fmt(x, digits = 4, signed = true) {
  if (x === null || x === undefined) return h("span", { class: "unknown" }, "unknown");
  const text = (signed && x > 0 ? "+" : "") + Number(x).toFixed(digits);
  return h("span", { class: x < 0 ? "neg" : "" }, text);
}

function label(node) {
  return node.label || node.id;
}

function view() {
  return $("#view");
}

function nodes(children) {
  // Views pass nested arrays and nulls for optional parts; flatten and drop the empties.
  return children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false);
}

function show(...children) {
  view().replaceChildren(...nodes(children));
}

function fail(err) {
  show(h("p", { class: "error" }, String(err.message || err)));
}

function tip(event, lines) {
  const el = $("#tip");
  el.replaceChildren(...lines.map((line) => h("div", {}, line)));
  el.hidden = false;
  const x = Math.min(event.clientX + 14, window.innerWidth - 350);
  el.style.left = `${x}px`;
  el.style.top = `${event.clientY + 14}px`;
}

function untip() {
  $("#tip").hidden = true;
}

function openPanel(...children) {
  $("#panel-body").replaceChildren(...nodes(children));
  $("#panel").hidden = false;
}

function themeSelect(selected, onchange, { withProducts = false } = {}) {
  const select = h(
    "select",
    { onchange: (e) => onchange(e.target.value) },
    state.themes.map((t) =>
      h("option", { value: t.id, selected: t.id === selected }, `${t.label} (${t.kind})`),
    ),
  );
  if (!withProducts) return select;
  const custom = h("input", {
    placeholder: "or a product ID, e.g. product/hbm",
    size: 28,
    onkeydown: (e) => {
      if (e.key === "Enter" && e.target.value.trim()) onchange(e.target.value.trim());
    },
  });
  return h("span", {}, select, " ", custom);
}

function hashParams() {
  const [name, query] = location.hash.slice(1).split("?");
  return { name: name || "flow", query: new URLSearchParams(query || "") };
}

function go(name, query = {}) {
  location.hash = `${name}?${new URLSearchParams(query)}`;
}

// --- edge evidence panel -----------------------------------------------------------------

async function showEdge(edgeId) {
  openPanel(h("p", { class: "loading" }, "loading edge…"));
  try {
    const e = await api(`/api/edge/${edgeId}`);
    openPanel(
      h("h2", {}, `${e.src_label} → ${e.dst_label}`),
      h("div", { class: "mono muted" }, `${e.edge}  ${e.rel}`),
      h(
        "p",
        {},
        "weight ",
        h("b", {}, e.weight),
        " ",
        h("span", { class: `tag ${e.weight_source}` }, e.weight_source),
        `  polarity ${e.polarity}  confidence ${e.confidence}  `,
        e.propagates ? "propagates" : h("b", { class: "neg" }, "does not propagate"),
      ),
      h("p", { class: "muted" }, `source layer: ${e.source}, valid from ${e.valid_from}`),
      h("h3", {}, "Evidence"),
      e.evidence.map((item) =>
        h(
          "div",
          { class: `evidence ${item.verified ? "verified" : ""}` },
          h("div", {}, h("a", { href: item.url, target: "_blank", rel: "noopener" }, item.url)),
          h("div", {}, item.note),
          h(
            "div",
            { class: "muted" },
            `accessed ${item.accessed || "?"} · `,
            item.verified ? "verified" : "unverified: a lead, not a checked fact",
            item.span ? ` · ${item.span.section} [${item.span.start}, ${item.span.end})` : "",
          ),
        ),
      ),
      e.alternatives.length ? h("h3", {}, "Rows from other layers that lost (D63)") : null,
      e.alternatives.map((alt) =>
        h(
          "div",
          { class: "evidence" },
          `${alt.source}: weight ${alt.weight} (${alt.weight_source}), confidence ${alt.confidence}`,
          alt.evidence.map((item) => h("div", { class: "muted" }, item.note)),
        ),
      ),
      h("p", { class: "muted" }, e.notes.join(" ")),
    );
  } catch (err) {
    openPanel(h("p", { class: "error" }, err.message));
  }
}

async function showPaths(from, to) {
  openPanel(h("p", { class: "loading" }, "loading paths…"));
  try {
    const data = await api("/api/paths", { from, to, k: 5 });
    openPanel(
      h("h2", {}, `${data.from_node.label} → ${data.to_node.label}`),
      h("p", { class: "muted" }, `strongest demand paths, as of ${data.as_of}`),
      data.paths.length ? null : h("p", {}, "No demand flows between these nodes within range."),
      data.paths.map((p, i) =>
        h(
          "div",
          { class: "path card" },
          h(
            "div",
            {},
            h("b", {}, `${i + 1}. `),
            fmt(p.contribution),
            h("span", { class: "muted" }, `  confidence ${p.confidence}, ${p.guessed_weights} guessed`),
          ),
          h("div", { class: "chain" }, p.nodes.map((n) => n.label).join(" → ")),
          h(
            "div",
            {},
            p.edges.map((e) =>
              h(
                "div",
                {},
                h("a", { href: "#", onclick: (ev) => (ev.preventDefault(), showEdge(e.edge)) }, e.edge),
                ` ${e.rel} ${e.weight} `,
                h("span", { class: `tag ${e.weight_source}` }, e.weight_source),
                e.evidence.some((x) => x.verified) ? "" : h("span", { class: "muted" }, " unverified"),
              ),
            ),
          ),
        ),
      ),
      h("p", { class: "muted" }, data.notes[0]),
    );
  } catch (err) {
    openPanel(h("p", { class: "error" }, err.message));
  }
}

// --- Flow view ---------------------------------------------------------------------------

async function flowView(query) {
  const node = query.get("node") || state.themes[0]?.id;
  const direction = query.get("direction") || "up";
  const toolbar = h(
    "div",
    { class: "toolbar" },
    themeSelect(node, (id) => go("flow", { node: id, direction }), { withProducts: true }),
    h(
      "select",
      { onchange: (e) => go("flow", { node, direction: e.target.value }) },
      h("option", { value: "up", selected: direction === "up" }, "demand up"),
      h("option", { value: "down", selected: direction === "down" }, "demand down"),
    ),
  );
  show(h("h2", {}, "Propagation flow"), toolbar, h("p", { class: "loading" }, "loading…"));
  try {
    const data = await api("/api/flow", { node, direction });
    show(h("h2", {}, "Propagation flow"), toolbar, drawFlow(data), flowLegend(data));
  } catch (err) {
    fail(err);
  }
}

function drawFlow(data) {
  const colW = 230, rowH = 30, boxW = 180, boxH = 22, pad = 20;
  const columns = new Map();
  for (const n of data.nodes) {
    if (!columns.has(n.depth)) columns.set(n.depth, []);
    columns.get(n.depth).push(n);
  }
  const weight = new Map();
  for (const e of data.edges) {
    for (const end of [e.src, e.dst]) weight.set(end, (weight.get(end) || 0) + e.flow);
  }
  const pos = new Map();
  let maxRows = 0;
  for (const [depth, nodes] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    // Products above companies; products by the flow they carry, companies by exposure, and
    // companies reached only by a below-threshold edge last.
    const rank = (n) => (n.type === "company" ? (n.exposure === undefined ? 2 : 1) : 0);
    nodes.sort((a, b) =>
      rank(a) - rank(b) ||
      (rank(a) === 1
        ? Math.abs(b.exposure) - Math.abs(a.exposure)
        : (weight.get(b.id) || 0) - (weight.get(a.id) || 0)),
    );
    nodes.forEach((n, i) => pos.set(n.id, { x: pad + depth * colW, y: pad + i * rowH }));
    maxRows = Math.max(maxRows, nodes.length);
  }
  const width = pad * 2 + (Math.max(...columns.keys()) + 1) * colW;
  const height = pad * 2 + maxRows * rowH;
  const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
  const maxFlow = Math.max(...data.edges.map((e) => e.flow), 1e-9);
  const byId = new Map(data.nodes.map((n) => [n.id, n]));

  for (const e of data.edges) {
    // Edges are stored in natural direction; draw them the way demand flows (PRODUCES and
    // SUPPLIES reversed), so every curve runs left to right.
    const reversed = e.rel === "PRODUCES" || e.rel === "SUPPLIES";
    const [from, to] = reversed ? [e.dst, e.src] : [e.src, e.dst];
    const a = pos.get(from), b = pos.get(to);
    if (!a || !b) continue;
    const x1 = a.x + boxW, y1 = a.y + boxH / 2, x2 = b.x, y2 = b.y + boxH / 2;
    const mid = (x1 + x2) / 2;
    // Coloured by the sign of the shock the edge carries, not its own polarity: on a share shift
    // the loser's PRODUCES edges are positive but carry a loss.
    const carriesLoss = e.propagates ? e.signed_flow < 0 : e.polarity < 0;
    const classes = ["edge", carriesLoss ? "neg" : "pos"];
    if (!e.propagates) classes.push("ghost");
    else if (e.weight_source === "bucket") classes.push("bucket");
    const widthPx = e.propagates ? 1 + 7 * Math.sqrt(e.flow / maxFlow) : 1;
    svg.append(
      s("path", {
        class: classes.join(" "),
        d: `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`,
        "stroke-width": widthPx.toFixed(2),
        "stroke-opacity": e.propagates ? 0.55 : 0.8,
        onclick: () => showEdge(e.edge),
        onmousemove: (ev) =>
          tip(ev, [
            `${label(byId.get(e.src))} ${e.rel} ${label(byId.get(e.dst))}`,
            `weight ${e.weight} (${e.weight_source}), confidence ${e.confidence}`,
            e.propagates ? `carries ${e.signed_flow.toFixed(4)} of the shown paths' shock` : "below threshold: not propagated",
            `source layer ${e.source} · click for evidence`,
          ]),
        onmouseleave: untip,
      }),
    );
  }
  for (const n of data.nodes) {
    const p = pos.get(n.id);
    const held = n.type === "company" && n.exposure === undefined;
    const text = n.exposure !== undefined ? `${n.label}  ${(n.exposure > 0 ? "+" : "") + n.exposure.toFixed(3)}` : n.label;
    const g = s(
      "g",
      {
        class: `node ${n.type} ${held ? "held" : ""}`,
        transform: `translate(${p.x},${p.y})`,
        onclick: () => {
          if (n.type === "company") go("company", { id: n.id });
          else if (n.id !== data.node) go("flow", { node: n.id });
        },
        onmousemove: (ev) =>
          tip(ev, [
            `${n.label} (${n.id})`,
            n.exposure !== undefined ? `exposure ${n.exposure} per unit shock` : "",
            n.exposure !== undefined
              ? n.novelty === null ? "novelty unknown (no coverage series)" : `novelty ${n.novelty}`
              : "",
            held ? "reached only by a below-threshold edge" : "",
            n.type === "company" ? "click for company profile" : n.id !== data.node ? "click to shock this node" : "",
          ].filter(Boolean)),
        onmouseleave: untip,
      },
      s("rect", { width: boxW, height: boxH, rx: 5 }),
      s("text", { x: 7, y: 15 }, text.length > 30 ? text.slice(0, 29) + "…" : text),
    );
    svg.append(g);
  }
  return h("div", { class: "svg-wrap" }, svg);
}

function flowLegend(data) {
  const sample = (cls) =>
    s("svg", { width: 34, height: 10 }, s("path", { class: `edge ${cls}`, d: "M2,5 L32,5", "stroke-width": 2.5 }));
  return h(
    "div",
    {},
    h(
      "div",
      { class: "legend" },
      h("span", {}, sample("pos"), " measured share (filing or manual)"),
      h("span", {}, sample("pos bucket"), " bucket guess"),
      h("span", {}, sample("neg"), " carries a loss"),
      h("span", {}, sample("ghost"), " below confidence threshold: evidence only"),
      h("span", {}, "width ∝ √(share of the shown paths it carries)"),
    ),
    h(
      "p",
      { class: "muted" },
      `${data.node} (${data.shock_type}${data.kind ? ", " + data.kind : ""}), demand ${data.direction}, as of ${data.as_of}. `,
      data.notes.join(" "),
    ),
  );
}

// --- Ranking view ------------------------------------------------------------------------

async function rankingView(query) {
  const node = query.get("node") || state.themes[0]?.id;
  const sort = query.get("sort") || "exposure";
  const hide = query.get("hide") === "true";
  const nav = (over) => go("ranking", { node, sort, hide, ...over });
  const toolbar = h(
    "div",
    { class: "toolbar" },
    themeSelect(node, (id) => nav({ node: id }), { withProducts: true }),
    h(
      "select",
      { onchange: (e) => nav({ sort: e.target.value }) },
      h("option", { value: "exposure", selected: sort === "exposure" }, "sort by exposure"),
      h("option", { value: "novelty", selected: sort === "novelty" }, "sort by novelty"),
    ),
    h(
      "label",
      {},
      h("input", { type: "checkbox", checked: hide, onchange: (e) => nav({ hide: e.target.checked }) }),
      " hide obvious",
    ),
  );
  show(h("h2", {}, "Ranking"), toolbar, h("p", { class: "loading" }, "loading…"));
  try {
    const data = await api("/api/exposed", { node, hide_obvious: hide, limit: 60 });
    let rows = data.results;
    if (sort === "novelty") {
      // Unknown novelty sorts last, never first: missing data must not look like a discovery.
      rows = [...rows].sort((a, b) =>
        a.novelty === null ? 1 : b.novelty === null ? -1 : Math.abs(b.novelty) - Math.abs(a.novelty),
      );
    }
    const table = h(
      "table",
      {},
      h(
        "tr",
        {},
        ["#", "Company", "Ticker", "Exposure", "Novelty", "Attn %ile", "Hops", "Guessed", "Conf", "Top path"].map(
          (c, i) => h("th", { class: i >= 3 && i <= 8 ? "num" : "" }, c),
        ),
      ),
      rows.map((r, i) =>
        h(
          "tr",
          { class: "clickable", onclick: () => showPaths(node, r.company) },
          h("td", {}, i + 1),
          h("td", {}, r.label),
          h("td", { class: "muted" }, r.ticker || ""),
          h("td", { class: "num" }, fmt(r.exposure)),
          h("td", { class: "num" }, fmt(r.novelty)),
          h("td", { class: "num" }, fmt(r.attention_percentile, 2, false)),
          h("td", { class: "num" }, r.min_hops ?? ""),
          h("td", { class: "num" }, r.guessed_weights),
          h("td", { class: "num" }, r.path_confidence.toFixed(2)),
          h("td", { class: "muted" }, r.paths[0] ? r.paths[0].nodes.slice(1, -1).map(shortId).join(" → ") : ""),
        ),
      ),
    );
    show(
      h("h2", {}, "Ranking"),
      toolbar,
      h("div", { class: "card" }, table),
      h("p", { class: "muted" }, `as of ${data.as_of}. Click a row for its paths and evidence. `, data.notes.join(" ")),
    );
  } catch (err) {
    fail(err);
  }
}

function shortId(id) {
  return id.split("/")[1];
}

// --- Company view ------------------------------------------------------------------------

async function companyView(query) {
  const id = query.get("id");
  const search = h("input", { placeholder: "search a company…", size: 30 });
  const results = h("div", {});
  search.addEventListener("input", async () => {
    const q = search.value.trim();
    if (q.length < 2) return results.replaceChildren();
    const data = await api("/api/search", { q, type: "company", limit: 8 });
    results.replaceChildren(
      ...data.results.map((m) =>
        h("div", {}, h("a", { href: `#company?id=${encodeURIComponent(m.id)}` }, `${m.label} `), h("span", { class: "muted" }, m.ticker || "")),
      ),
    );
  });
  const head = [h("h2", {}, "Company"), h("div", { class: "toolbar" }, search), results];
  if (!id) return show(...head, h("p", { class: "muted" }, "Search for a company, or click one in the flow or ranking."));
  show(...head, h("p", { class: "loading" }, "loading…"));
  try {
    const p = await api("/api/company", { id });
    const mix = h(
      "table",
      {},
      h("tr", {}, h("th", {}, "Product"), h("th", {}, "Share of revenue"), h("th", {}, "Source"), h("th", { class: "num" }, "Conf")),
      p.revenue_mix.map((r) =>
        h(
          "tr",
          { class: "clickable", onclick: () => showEdge(r.edge) },
          h("td", {}, r.label, r.propagates ? "" : h("span", { class: "muted" }, " (held)")),
          h(
            "td",
            {},
            h(
              "div",
              { class: "bar-cell" },
              h("div", { class: `bar-fill ${r.weight_source === "bucket" ? "bucket" : ""}`, style: `width:${Math.round(r.weight * 240)}px` }),
              r.weight.toFixed(3),
            ),
          ),
          h("td", {}, h("span", { class: `tag ${r.weight_source}` }, r.weight_source)),
          h("td", { class: "num" }, r.confidence),
        ),
      ),
    );
    const neighbours = (title, rows, key) =>
      rows.length
        ? h(
            "div",
            {},
            h("h3", {}, title),
            rows.map((r) =>
              h(
                "div",
                {},
                h("a", { href: "#", onclick: (e) => (e.preventDefault(), showEdge(r.edge)) }, r.label),
                ` ${r.weight.toFixed(3)} `,
                h("span", { class: `tag ${r.weight_source}` }, r.weight_source),
                key === "region" || !r[key]?.startsWith("company/")
                  ? ""
                  : h("a", { href: `#company?id=${encodeURIComponent(r[key])}`, class: "muted" }, " profile"),
              ),
            ),
          )
        : null;
    const themes = h(
      "table",
      {},
      h("tr", {}, h("th", {}, "Theme"), h("th", {}, "Kind"), h("th", { class: "num" }, "Exposure"), h("th", { class: "num" }, "Rank"), h("th", {}, "Top path")),
      p.themes.map((t) =>
        h(
          "tr",
          { class: "clickable", onclick: () => showPaths(t.theme, id) },
          h("td", {}, t.label),
          h("td", { class: "muted" }, t.kind),
          h("td", { class: "num" }, fmt(t.exposure)),
          h("td", { class: "num" }, `${t.rank} / ${t.of}`),
          h("td", { class: "muted" }, t.top_path ? t.top_path.nodes.slice(1, -1).map((n) => n.label).join(" → ") : ""),
        ),
      ),
    );
    show(
      ...head,
      h("h2", {}, `${p.company.label} `, h("span", { class: "muted" }, p.company.ticker || "")),
      h("p", { class: "muted" }, `as of ${p.as_of} · ${p.company.id}`),
      h(
        "div",
        { class: "grid" },
        h(
          "div",
          { class: "card" },
          h("h3", {}, "Revenue mix"),
          mix,
          h("p", { class: "muted" }, `mapped share of revenue ${p.mapped_share.toFixed(3)}; the rest is outside the graph`),
        ),
        h(
          "div",
          { class: "card" },
          neighbours("Customers", p.customers, "customer"),
          neighbours("Suppliers", p.suppliers, "supplier"),
          neighbours("Parent", p.parents, "parent"),
          neighbours("Subsidiaries", p.subsidiaries, "subsidiary"),
          neighbours("Revenue by region", p.regions, "region"),
          h("h3", {}, "Attention"),
          p.attention
            ? h("div", {}, `${p.attention.share.toExponential(2)} of all coverage over ${p.attention.days} days`)
            : h("div", { class: "unknown" }, "unknown: no coverage series is held for this company"),
        ),
      ),
      h("h3", {}, "Exposure per theme"),
      h("div", { class: "card" }, themes),
      h("p", { class: "muted" }, "Exposures are listed per theme and must never be added across themes (D9)."),
    );
  } catch (err) {
    fail(err);
  }
}

// --- Signals view ------------------------------------------------------------------------

async function signalsView(query) {
  const key = query.get("key") || state.themes.find((t) => t.series_days)?.id || state.themes[0]?.id;
  const days = query.get("days") || "730";
  await awaitCalibration(query);
  const calibrated = state.calibration?.threshold;
  const threshold = query.get("min_surprise") || (calibrated ?? 6);
  const nav = (over) => go("signals", { key, days, min_surprise: threshold, ...over });
  const input = h("input", { type: "number", step: "0.25", min: "0", value: threshold, onchange: (e) => nav({ min_surprise: e.target.value }) });
  const toolbar = h(
    "div",
    { class: "toolbar" },
    themeSelect(key, (id) => nav({ key: id })),
    h(
      "select",
      { onchange: (e) => nav({ days: e.target.value }) },
      ["90", "365", "730", "1800"].map((d) => h("option", { value: d, selected: d === days }, `last ${d} days`)),
    ),
    h("label", {}, "burst threshold ", input),
    state.calibration
      ? h(
          "span",
          { class: "muted" },
          `calibrated ${state.calibration.threshold} on ${state.calibration.series} series (${state.calibration.rate}/theme-year); `,
          `${state.calibration.in_code} in code`,
        )
      : null,
  );
  show(h("h2", {}, "Coverage signal"), toolbar, h("p", { class: "loading" }, "loading…"));
  try {
    const [data, events] = await Promise.all([
      api("/api/signals", { key, days, min_surprise: threshold }),
      api("/api/events", { min_surprise: threshold }),
    ]);
    if (!data.days.length) {
      return show(
        h("h2", {}, "Coverage signal"),
        toolbar,
        h("p", { class: "unknown" }, `No coverage series held for ${key}: unknown, not quiet. Run ripple signals fetch.`),
      );
    }
    const marks = events.events.filter((e) => e.theme === key);
    const bursts = h(
      "table",
      {},
      h("tr", {}, ["Window", "Peak", "Ratio", "Surprise", "Articles", "Flag"].map((c) => h("th", {}, c))),
      data.bursts.map((b) =>
        h("tr", {}, h("td", {}, `${b.start} → ${b.end}`), h("td", {}, b.peak_day), h("td", {}, `${b.ratio}×`), h("td", {}, b.surprise), h("td", {}, b.matched), h("td", {}, b.tone_flag || "")),
      ),
    );
    show(
      h("h2", {}, "Coverage signal"),
      toolbar,
      drawSeries(data, marks),
      h(
        "div",
        { class: "legend" },
        h("span", {}, "solid: share of all monitored articles, per 100k"),
        h("span", {}, "dashed: trailing median baseline"),
        h("span", {}, "shaded: burst windows"),
        h("span", {}, "red ticks: pre-registered events (display only; never tuned against, D101)"),
      ),
      h("h3", {}, `${data.bursts.length} bursts at threshold ${data.min_surprise}`),
      h("div", { class: "card" }, bursts),
      h("p", { class: "muted" }, `query ${data.query_hash} · ${data.held_days} days held · as of ${data.as_of}. `, data.notes[0]),
    );
  } catch (err) {
    fail(err);
  }
}

function drawSeries(data, marks) {
  const W = 1000, H = 280, L = 50, R = 12, T = 12, B = 26;
  const points = data.days.map((d) => ({ ...d, t: Date.parse(d.day), y: d.share * 1e5, base: d.baseline * 1e5 }));
  const t0 = points[0].t, t1 = points[points.length - 1].t;
  const ymax = Math.max(...points.map((p) => p.y), 1e-9) * 1.05;
  const x = (t) => L + ((t - t0) / Math.max(t1 - t0, 1)) * (W - L - R);
  const y = (v) => H - B - (v / ymax) * (H - T - B);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%" });
  for (const b of data.bursts) {
    const a = x(Date.parse(b.start)), z = x(Date.parse(b.end) + 86400000);
    svg.append(s("rect", { class: "burst", x: a, y: T, width: Math.max(z - a, 2), height: H - T - B }));
  }
  for (const e of marks) {
    const t = Date.parse(e.day);
    if (t < t0 || t > t1) continue;
    svg.append(
      s("line", {
        class: "event",
        x1: x(t), x2: x(t), y1: T, y2: H - B,
        onmousemove: (ev) => tip(ev, [e.label, `${e.day} · ${e.hit ? "hit" : "miss"}${e.approximate ? " · approximate date" : ""}`]),
        onmouseleave: untip,
      }),
    );
  }
  const line = (key) => points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
  svg.append(s("path", { class: "baseline", d: line("base") }), s("path", { class: "series", d: line("y") }));
  const axis = s("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const v = (ymax * i) / 4;
    axis.append(s("line", { x1: L, x2: W - R, y1: y(v), y2: y(v) }), s("text", { x: 4, y: y(v) + 4 }, v.toFixed(v < 10 ? 1 : 0)));
  }
  const years = new Set(points.map((p) => p.day.slice(0, 7)));
  let last = "";
  for (const p of points) {
    const month = p.day.slice(0, 7);
    if (month !== last && (years.size < 14 || month.endsWith("-01"))) {
      axis.append(s("text", { x: x(p.t), y: H - 8 }, years.size < 14 ? month.slice(5) + "/" + month.slice(2, 4) : month.slice(0, 4)));
      last = month;
    }
  }
  svg.append(axis);
  const hover = s("rect", {
    x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent",
    onmousemove: (ev) => {
      const box = svg.getBoundingClientRect();
      const t = t0 + ((ev.clientX - box.left) / box.width * W - L) / (W - L - R) * (t1 - t0);
      const p = points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
      tip(ev, [
        p.day,
        `${p.matched} articles, share ${p.y.toFixed(2)} per 100k`,
        `expected ${p.expected}, ratio ${p.ratio}×, surprise ${p.surprise}`,
        p.tone === null ? "" : `tone ${p.tone.toFixed(2)}`,
      ].filter(Boolean));
    },
    onmouseleave: untip,
  });
  svg.append(hover);
  return h("div", { class: "svg-wrap" }, svg);
}

async function awaitCalibration(query) {
  // The default threshold is the calibrated one (D109), so wait for it unless the URL sets one.
  if (query.get("min_surprise") || state.calibration || !state.calibrating) return;
  show(h("p", { class: "loading" }, "calibrating the burst threshold from the coverage series…"));
  await state.calibrating;
}

// --- Events view -------------------------------------------------------------------------

async function eventsView(query) {
  await awaitCalibration(query);
  const threshold = query.get("min_surprise") || (state.calibration?.threshold ?? 6);
  const input = h("input", { type: "number", step: "0.25", min: "0", value: threshold, onchange: (e) => go("events", { min_surprise: e.target.value }) });
  const toolbar = h("div", { class: "toolbar" }, h("label", {}, "burst threshold ", input));
  show(h("h2", {}, "Pre-registered events"), toolbar, h("p", { class: "loading" }, "loading…"));
  try {
    const d = await api("/api/events", { min_surprise: threshold });
    const table = h(
      "table",
      {},
      h("tr", {}, ["Date", "Event", "Theme", "Direction", "Result", "Nearest burst", "Tone flag", "Shares a burst with"].map((c) => h("th", {}, c))),
      d.events.map((e) =>
        h(
          "tr",
          { class: "clickable", onclick: () => go("signals", { key: e.theme, min_surprise: threshold }) },
          h("td", {}, e.day, e.approximate ? h("span", { class: "muted" }, " ≈") : ""),
          h("td", {}, e.label, e.reversal ? h("span", { class: "tag" }, " reversal") : ""),
          h("td", { class: "muted" }, shortId(e.theme)),
          h("td", {}, e.direction),
          h(
            "td",
            {},
            !e.has_series
              ? h("span", { class: "unknown" }, "no series")
              : h("span", { class: `tag ${e.hit ? "hit" : "miss"}` }, e.hit ? "hit" : "miss"),
          ),
          h("td", {}, e.burst ? `${e.burst.start} → ${e.burst.end}` : e.nearest_gap !== null ? `${e.nearest_gap} days away` : ""),
          h("td", {}, e.hit ? (e.flagged ? "possible reversal" : "none") : ""),
          h("td", { class: "muted" }, e.shares_burst_with || ""),
        ),
      ),
    );
    show(
      h("h2", {}, "Pre-registered events"),
      toolbar,
      h(
        "div",
        { class: "grid" },
        h("div", { class: "card" }, h("div", { class: "muted" }, "hit rate, all events"), h("div", { class: "stat" }, `${Math.round(d.hit_rate * 100)}%`), h("div", { class: "muted" }, `bar ${Math.round(d.min_hit_rate * 100)}%, ±${d.tolerance_days} days`)),
        h("div", { class: "card" }, h("div", { class: "muted" }, "hit rate, exact dates"), h("div", { class: "stat" }, `${Math.round(d.exact_hit_rate * 100)}%`)),
        h("div", { class: "card" }, h("div", { class: "muted" }, "events with a series"), h("div", { class: "stat" }, `${d.measurable} / ${d.events.length}`), h("div", { class: "muted" }, "events without one count as misses")),
      ),
      h("h3", {}, "Events"),
      h("div", { class: "card" }, table),
      h(
        "p",
        { class: "muted" },
        "The event set was written before any coverage was fetched (D101). The threshold must come from calibration on the coverage series alone, never from this table.",
      ),
    );
  } catch (err) {
    fail(err);
  }
}

// --- Quality view ------------------------------------------------------------------------

async function qualityView() {
  show(h("h2", {}, "Quality"), h("p", { class: "loading" }, "loading…"));
  try {
    const q = await api("/api/quality");
    const v = q.verification;
    const sources = h(
      "table",
      {},
      h("tr", {}, h("th", {}, "Relation"), h("th", { class: "num" }, "filing"), h("th", { class: "num" }, "manual"), h("th", { class: "num" }, "bucket"), h("th", { class: "num" }, "bucket share")),
      Object.entries(q.weight_sources).map(([rel, c]) => {
        const total = (c.filing || 0) + (c.manual || 0) + (c.bucket || 0);
        return h(
          "tr",
          {},
          h("td", {}, rel),
          h("td", { class: "num" }, c.filing || 0),
          h("td", { class: "num" }, c.manual || 0),
          h("td", { class: "num" }, c.bucket || 0),
          h("td", { class: "num" }, `${Math.round(((c.bucket || 0) / total) * 100)}%`),
        );
      }),
    );
    const maxConf = Math.max(...Object.values(q.confidence));
    const conf = h(
      "div",
      {},
      Object.entries(q.confidence).map(([bin, n]) =>
        h("div", { class: "bar-cell" }, h("span", { class: "mono", style: "width:34px" }, bin), h("div", { class: "bar-fill", style: `width:${Math.round((n / maxConf) * 220)}px` }), n),
      ),
      h("p", { class: "muted" }, "edges below 0.5 are stored as evidence and do not propagate"),
    );
    show(
      h("h2", {}, "Quality"),
      h("p", { class: "muted" }, `as of ${q.as_of}: how much of the graph is measured, checked and covered`),
      h(
        "div",
        { class: "grid" },
        h("div", { class: "card" }, h("div", { class: "muted" }, "evidence ruled on"), h("div", { class: "stat" }, `${v.ruled} / ${v.total}`), h("div", { class: "muted" }, `${v.verified} verified, ${v.failed} failed, ${v.documents_left} documents left · ripple verify walk`)),
        h("div", { class: "card" }, h("div", { class: "muted" }, "theme coverage series"), h("div", { class: "stat" }, `${q.coverage.themes_held} / ${q.coverage.themes}`)),
        h("div", { class: "card" }, h("div", { class: "muted" }, "company coverage series"), h("div", { class: "stat" }, `${q.coverage.companies_held} / ${q.coverage.companies}`), h("div", { class: "muted" }, "attention and novelty are unknown without them")),
        h("div", { class: "card" }, h("div", { class: "muted" }, "edges"), h("div", { class: "stat" }, q.stored_edges), h("div", { class: "muted" }, `${q.propagating_edges} propagate · winners by layer: ${Object.entries(q.sources).map(([k, n]) => `${k} ${n}`).join(", ")}`)),
      ),
      h("h3", {}, "Weight sources on propagating edges"),
      h("div", { class: "card" }, sources),
      h("h3", {}, "Edge confidence"),
      h("div", { class: "card" }, conf),
    );
  } catch (err) {
    fail(err);
  }
}

// --- boot --------------------------------------------------------------------------------

const VIEWS = { flow: flowView, ranking: rankingView, company: companyView, signals: signalsView, events: eventsView, quality: qualityView };

async function route() {
  const { name, query } = hashParams();
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("active", a.getAttribute("href") === `#${name}`);
  $("#panel").hidden = true;
  untip();
  await (VIEWS[name] || flowView)(query);
}

async function refreshBanner() {
  const health = await api("/api/health");
  state.health = health;
  const warnings = [];
  if (health.themes.missing.length)
    warnings.push(`${health.themes.missing.length} of ${health.themes.total} themes have no coverage series`);
  if (health.companies.missing)
    warnings.push(`${health.companies.missing} of ${health.companies.total} companies have none, so their attention and novelty are unknown`);
  $("#banner").replaceChildren(
    ...(warnings.length ? [h("div", { class: "warn" }, warnings.join("; "), ". Run ripple signals fetch.")] : []),
  );
}

async function boot() {
  $("#panel-close").addEventListener("click", () => ($("#panel").hidden = true));
  document.addEventListener("keydown", (e) => e.key === "Escape" && ($("#panel").hidden = true));
  $("#as-of").addEventListener("change", () => route());
  $("#min-conf").addEventListener("change", () => route());
  try {
    const [themes] = await Promise.all([api("/api/themes"), refreshBanner()]);
    state.themes = themes.themes;
    $("#as-of").value = themes.as_of;
    state.calibrating = api("/api/calibration").then((c) => (state.calibration = c)).catch(() => null);
  } catch (err) {
    return fail(err);
  }
  window.addEventListener("hashchange", route);
  route();
}

boot();

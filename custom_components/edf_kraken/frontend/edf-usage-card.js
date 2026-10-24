// EDF usage card, served by the EDF Energy (Kraken) integration.
// Daily peak and off-peak use for each circuit and device, in kWh or £. Hours
// EDF hasn't settled yet are the CT clamps' own estimate and drawn striped;
// once EDF's data arrives the integration rewrites them and the bars go solid.

const PALETTE = [
  ["#2f7bff", "#6aa3ff"], ["#1f9d63", "#4cc9a0"], ["#e8742f", "#f4a36a"],
  ["#6f5cff", "#a99bff"], ["#d6457f", "#f78fb6"], ["#1a9f95", "#5fe0d4"], ["#c28a0c", "#f2b84a"],
];
const NAMED = { blue: 0, green: 1, orange: 2, purple: 3, pink: 4, teal: 5, yellow: 6 };
const KINDS = ["peak_energy", "off_peak_energy", "peak_cost", "off_peak_cost"];
const DAY = 86400000;
const REFRESH = 10 * 60000;
const STORE_KEY = "edf-usage-card-unit";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function startOfDay(t, offset = 0) {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + offset);
  return d.getTime();
}

/** A palette name, any CSS colour, or the next palette colour. */
function colourOf(name, i, dark) {
  if (name && !(name in NAMED)) return name;
  return PALETTE[name ? NAMED[name] : i % PALETTE.length][dark ? 1 : 0];
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

class EdfUsageCard extends HTMLElement {
  static getStubConfig() {
    return { days: 7 };
  }

  static getConfigElement() {
    return document.createElement("edf-usage-card-editor");
  }

  setConfig(config) {
    this._config = { days: 7, ...config };
    let saved;
    try { saved = localStorage.getItem(STORE_KEY); } catch { saved = null; }
    this._unit = saved || this._config.unit || "kwh";
    this._sel = undefined;
    this._render();
  }

  set hass(hass) {
    const first = !this._hass, dark = !!hass?.themes?.darkMode;
    this._hass = hass;
    // Statistics, not states, so only a theme change needs a redraw.
    if (first) this._load();
    else if (dark !== this._dark) this._render();
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, rows: 6, min_columns: 6, min_rows: 5 };
  }

  connectedCallback() {
    this._timer = setInterval(() => this._load(), REFRESH);
    this._ro = new ResizeObserver(() => {
      if (this.clientWidth !== this._width) this._render();
    });
    this._ro.observe(this);
    if (this._hass) this._load();
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    clearTimeout(this._retry);
    this._ro?.disconnect();
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      const { meters } = await this._hass.callWS({ type: "edf_kraken/usage" });
      const meter = meters.find((m) => m.mpan === this._config.mpan) ?? meters[0];
      if (!meter) { this._error = "No EDF meter found."; return; }
      this._meter = meter;
      this._series = this._pickSeries(meter);
      const days = Math.min(Math.max(Number(this._config.days) || 7, 3), 31);
      const start = startOfDay(Date.now(), -(days - 1));
      const ids = [meter.id, ...this._series.map((s) => s.id)].flatMap((id) => KINDS.map((k) => `${id}_${k}`));
      const stats = await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: new Date(start).toISOString(),
        statistic_ids: ids,
        period: "day",
        types: ["change"],
      });
      this._days = Array.from({ length: days }, (_, i) => startOfDay(start, i));
      this._data = {};
      for (const [id, rows] of Object.entries(stats ?? {})) {
        const byDay = {};
        for (const r of rows) {
          const t = typeof r.start === "number" ? r.start : Date.parse(r.start);
          byDay[startOfDay(t)] = (byDay[startOfDay(t)] ?? 0) + (r.change ?? 0);
        }
        this._data[id] = byDay;
      }
      this._error = undefined;
    } catch (err) {
      this._error = err?.message ?? String(err);
    } finally {
      this._loading = false;
      this._render();
    }
    // Just after a restart the integration may still be loading: look again soon.
    clearTimeout(this._retry);
    if (this._error || !this._meter?.settled_to) this._retry = setTimeout(() => this._load(), 60000);
  }

  /** Circuits, the loads inside them, then devices on their own, as the integration lists them. */
  _pickSeries(meter) {
    const chosen = this._config.series;
    if (Array.isArray(chosen) && chosen.length) {
      return chosen.map((c) => {
        const conf = typeof c === "string" ? { id: c } : c;
        const found = meter.series.find((s) => s.id === conf.id || s.name === conf.id || s.name === conf.name);
        return found ? { ...found, ...conf, id: found.id, name: conf.name ?? found.name } : undefined;
      }).filter(Boolean);
    }
    return meter.series;
  }

  /** Configured figures: "total" (the meter once settled) or a series by name, each with its off-peak share. */
  _figures(sel, label, status, totals) {
    const cells = this._config.figures.slice(0, 4).map((f) => {
      const conf = typeof f === "string" ? { series: f } : f;
      let peak, off, name;
      if (conf.series === "total") {
        const meter = (b) => this._value(this._meter.id, sel, b);
        // Settled: the meter itself, so anything not on a circuit is counted too.
        [peak, off] = status === "settled" ? [meter("peak"), meter("off_peak")]
          : [totals.reduce((a, t) => a + t.peak, 0), totals.reduce((a, t) => a + t.off, 0)];
        name = conf.label ?? label;
      } else {
        const t = totals.find((x) => x.s.name === conf.series || x.s.id === conf.series);
        if (!t) return `<div><div class="lab">${esc(conf.label ?? conf.series)}</div><div class="big">–</div><div class="note">Not found</div></div>`;
        [peak, off, name] = [t.peak, t.off, conf.label ?? t.s.name];
      }
      const all = peak + off;
      return `<div><div class="lab">${esc(name)}</div>
        <div class="big">${esc(this._fmt(all, false))}${this._unit === "kwh" ? "<small>kWh</small>" : ""}</div>
        <div class="note">${all > 0 ? Math.round((off / all) * 100) : 0}% off-peak</div></div>`;
    });
    const note = status === "settled" ? "" : `<div class="note est">${status === "partial" ? "Partly settled" : "CT estimate"}</div>`;
    return `<div class="stats ${cells.length > 3 ? "tight" : ""}">${cells.join("")}</div>${note}`;
  }

  _value(id, day, band) {
    const kind = this._unit === "gbp" ? `${band}_cost` : `${band}_energy`;
    return this._data?.[`${id}_${kind}`]?.[day] ?? 0;
  }

  _fmt(v, withUnit = true) {
    if (this._unit === "gbp") return `£${v.toFixed(2)}`;
    const s = v.toFixed(v >= 10 ? 1 : 2);
    return withUnit ? `${s} kWh` : s;
  }

  /** "settled", "partial" or "estimate" for the day starting at `day`. */
  _status(day) {
    const to = this._meter?.settled_to ? Date.parse(this._meter.settled_to) : 0;
    if (to >= startOfDay(day, 1)) return "settled";
    return to > day ? "partial" : "estimate";
  }

  _render() {
    if (!this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const hass = this._hass, dark = !!hass?.themes?.darkMode;
    this._dark = dark;
    this._width = this.clientWidth;
    const title = esc(this._config.title ?? "Electricity use");
    const pill = `<div class="seg" role="group" aria-label="Unit">
      <button data-unit="kwh" aria-pressed="${this._unit === "kwh"}">kWh</button>
      <button data-unit="gbp" aria-pressed="${this._unit === "gbp"}">£</button></div>`;
    let body;
    if (this._error) body = `<div class="empty">${esc(this._error)}</div>`;
    else if (!this._days || !this._series) body = `<div class="empty">Loading…</div>`;
    else if (!this._series.length) body = `<div class="empty">Add tracked devices or circuits to the EDF integration to split your use here.</div>`;
    else body = this._body(dark);
    this.shadowRoot.innerHTML = `<style>${STYLES}</style>
      <ha-card class="${dark ? "dark" : ""}"><div class="inner">
        <header><h2>${title}</h2><div class="end">${pill}${this._config.logo ? `<img class="logo" src="${esc(this._config.logo)}" alt="">` : ""}</div></header>${body}
      </div></ha-card>`;
    for (const b of this.shadowRoot.querySelectorAll("[data-unit]")) {
      b.addEventListener("click", () => {
        this._unit = b.dataset.unit;
        try { localStorage.setItem(STORE_KEY, this._unit); } catch { /* private mode */ }
        this._render();
      });
    }
    for (const g of this.shadowRoot.querySelectorAll("[data-day]")) {
      g.addEventListener("click", () => { this._sel = Number(g.dataset.day); this._render(); });
    }
  }

  _body(dark) {
    const days = this._days, series = this._series;
    const settledDays = days.filter((d) => this._status(d) === "settled");
    const first = this._config.day === "settled" && settledDays.length ? settledDays[settledDays.length - 1] : days[days.length - 1];
    const sel = days.includes(this._sel) ? this._sel : first;
    const status = this._status(sel);

    // Figures for the chosen day.
    const totals = series.map((s) => ({ s, peak: this._value(s.id, sel, "peak"), off: this._value(s.id, sel, "off_peak") }));
    const peak = totals.reduce((a, t) => a + t.peak, 0), off = totals.reduce((a, t) => a + t.off, 0);
    const all = peak + off;
    const label = sel === startOfDay(Date.now()) ? "Today" : new Date(sel).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    const note = status === "settled" ? "Settled by EDF" : status === "partial" ? "Partly settled" : "CT estimate";
    const meterTotal = this._value(this._meter.id, sel, "peak") + this._value(this._meter.id, sel, "off_peak");
    const unmeasured = status === "settled" && meterTotal - all > 0.005 * Math.max(meterTotal, 1)
      ? `<div class="note">${this._fmt(meterTotal - all)} not on any circuit</div>` : "";
    const figs = this._config.figures?.length
      ? this._figures(sel, label, status, totals)
      : `<div class="stats">
        <div><div class="lab">${esc(label)}</div><div class="big">${esc(this._fmt(all, false))}${this._unit === "kwh" ? "<small>kWh</small>" : ""}</div>
          <div class="note ${status === "settled" ? "good" : ""}">${note}</div>${unmeasured}</div>
        <div><div class="lab">Off-peak</div><div class="big sm">${esc(this._fmt(off, false))}${this._unit === "kwh" ? "<small>kWh</small>" : ""}</div>
          <div class="note">${all > 0 ? Math.round((off / all) * 100) : 0}% of the day</div></div>
        <div><div class="lab">Peak</div><div class="big sm">${esc(this._fmt(peak, false))}${this._unit === "kwh" ? "<small>kWh</small>" : ""}</div>
          <div class="note">${all > 0 ? Math.round((peak / all) * 100) : 0}% of the day</div></div>
      </div>`;

    // Chart: a group of bars per day, one per series, off-peak below peak.
    const W = Math.max(this.clientWidth - 32, 240), H = 150, L = 34, B = 18, T = 6;
    const max = niceMax(Math.max(...days.flatMap((d) => series.map((s) => this._value(s.id, d, "peak") + this._value(s.id, d, "off_peak"))), 0));
    const Y = (v) => T + (H - T - B) * (1 - v / max);
    const slot = (W - L) / days.length, groupW = slot * 0.74, barW = Math.max(groupW / series.length - 2, 2);
    const parts = [];
    for (let i = 0; i <= 2; i++) {
      const v = (max / 2) * i, y = Y(v);
      parts.push(`<line class="grid ${i ? "dash" : ""}" x1="${L}" x2="${W}" y1="${y}" y2="${y}"/>`);
      const n = String(Number(v.toFixed(2)));
      parts.push(`<text class="ax" x="${L - 6}" y="${y + 3}" text-anchor="end">${this._unit === "gbp" ? "£" + n : n}</text>`);
    }
    days.forEach((d, di) => {
      const x0 = L + slot * di + (slot - groupW) / 2, st = this._status(d);
      parts.push(`<g data-day="${d}" class="day ${d === sel ? "sel" : ""}"><rect class="hit" x="${L + slot * di}" y="0" width="${slot}" height="${H}"/>`);
      series.forEach((s, si) => {
        const c = colourOf(s.colour, si, dark), x = x0 + si * (barW + 2);
        const offV = this._value(s.id, d, "off_peak"), peakV = this._value(s.id, d, "peak");
        const yOff = Y(offV), yTop = Y(offV + peakV), base = Y(0);
        const fill = st === "settled" ? "" : ` mask="url(#stripes)"`;
        if (offV > 0) parts.push(`<rect x="${x}" y="${yOff}" width="${barW}" height="${Math.max(base - yOff, 1)}" rx="1.5" fill="${c}" fill-opacity=".45"${fill}/>`);
        if (peakV > 0) parts.push(`<rect x="${x}" y="${yTop}" width="${barW}" height="${Math.max(yOff - yTop, 1)}" rx="1.5" fill="${c}"${fill}/>`);
      });
      const lab = new Date(d).toLocaleDateString(undefined, days.length > 10 ? { day: "numeric" } : { weekday: "short" });
      parts.push(`<text class="ax ${d === sel ? "on" : ""}" x="${L + slot * (di + 0.5)}" y="${H - 4}" text-anchor="middle">${esc(lab)}</text></g>`);
    });
    const defs = `<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="5" height="5" fill="#fff" fill-opacity=".55"/><rect width="2.5" height="5" fill="#fff"/></pattern>
        <mask id="stripes" maskContentUnits="userSpaceOnUse"><rect x="0" y="0" width="${W}" height="${H}" fill="url(#hatch)"/></mask></defs>`;
    const chart = `<div class="chart"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Daily use">${defs}${parts.join("")}</svg></div>`;

    // Legend doubles as the chosen day's breakdown.
    const rows = totals.map(({ s, peak: p, off: o }, si) => `<div class="row">
        <i style="background:${colourOf(s.colour, si, dark)}"></i><span class="name">${esc(s.name)}</span>
        <span class="v">${esc(this._fmt(p + o))}</span>
        <span class="split">${esc(this._fmt(o, this._unit === "gbp"))} off · ${esc(this._fmt(p, this._unit === "gbp"))} peak</span></div>`).join("");
    const key = `<div class="key"><span><b class="k solid"></b>Peak</span><span><b class="k light"></b>Off-peak</span><span><b class="k striped"></b>Estimate</span></div>`;
    // With figures configured the list would repeat them.
    return `${figs}${chart}${this._config.figures?.length ? "" : `<div class="rows">${rows}</div>`}${key}`;
  }
}

const STYLES = `
  :host { display: block; height: 100%; }
  ha-card {
    --surface: var(--ha-card-background, var(--card-background-color, #fff));
    --ink: var(--primary-text-color, #1b1b1f);
    --ink-2: var(--secondary-text-color, #5c5c66);
    --line: var(--divider-color, rgba(0,0,0,.12));
    --sunken: color-mix(in srgb, var(--ink) 9%, transparent);
    --good: #1f8a5b;
    --press: 0 1px 2px rgba(0,0,0,.28), 0 2px 6px rgba(0,0,0,.14);
    display: block; height: 100%; box-sizing: border-box; overflow: hidden; background: var(--surface); color: var(--ink);
    border-radius: var(--ha-card-border-radius, 12px);
    font-family: var(--paper-font-body1_-_font-family, system-ui, sans-serif);
    box-shadow: 0 14px 36px -4px rgba(20, 20, 40, .22), 0 3px 8px rgba(20, 20, 40, .12);
  }
  ha-card.dark { --sunken: color-mix(in srgb, var(--ink) 14%, transparent); --good: #4cc9a0; --press: 0 1px 2px rgba(0,0,0,.6), 0 2px 8px rgba(0,0,0,.4);
    box-shadow: 0 14px 36px -4px rgba(0,0,0,.65), 0 3px 8px rgba(0,0,0,.45); }
  .inner { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; height: 100%; box-sizing: border-box; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 24px; }
  h2 { margin: 0; font-size: 16px; line-height: 22px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .seg { flex: none; display: flex; padding: 3px; border-radius: 99px; background: var(--sunken); }
  .seg button { border: 0; background: none; padding: 5px 14px; border-radius: 99px; font: inherit; font-size: 13px; line-height: 18px; font-weight: 500; color: var(--ink-2); cursor: pointer; transition: background .2s ease, color .2s ease; }
  .seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); box-shadow: var(--press); }
  .lab { font-size: 10px; line-height: 12px; font-weight: 500; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-2); font-family: var(--code-font-family, ui-monospace, monospace); }
  .stats { display: flex; gap: 8px; }
  .stats > div { flex: 1; min-width: 0; }
  .stats > div:first-child { flex: 1.3; }
  .big { font-size: 26px; line-height: 30px; font-weight: 600; letter-spacing: -.04em; margin-top: 2px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .big.sm { font-size: 20px; line-height: 30px; }
  .big small { font-size: 12px; font-weight: 500; letter-spacing: 0; color: var(--ink-2); margin-left: 3px; }
  .note { font-size: 12px; line-height: 16px; color: var(--ink-2); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .note.good { color: var(--good); }
  .note.est { margin-top: -4px; }
  .stats.tight .big { font-size: 21px; letter-spacing: -.03em; }
  .stats.tight > div:first-child { flex: 1; }
  .end { display: flex; align-items: center; gap: 10px; flex: none; }
  .logo { height: 28px; width: auto; max-width: 72px; object-fit: contain; display: block; }
  .chart svg { display: block; overflow: visible; }
  .grid { stroke: var(--line); stroke-width: 1; }
  .grid.dash { stroke-dasharray: 2 4; }
  .ax { fill: var(--ink-2); opacity: .8; font: 500 10px var(--code-font-family, ui-monospace, monospace); }
  .ax.on { fill: var(--ink); opacity: 1; font-weight: 700; }
  .day { cursor: pointer; }
  .hit { fill: transparent; }
  .day.sel .hit { fill: var(--sunken); }
  .rows { display: flex; flex-direction: column; gap: 4px; }
  .row { display: grid; grid-template-columns: 10px 1fr auto; column-gap: 8px; align-items: baseline; font-size: 13px; line-height: 18px; }
  .row i { width: 10px; height: 10px; border-radius: 3px; align-self: center; }
  .row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row .v { font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; }
  .row .split { grid-column: 2 / 4; font-size: 11px; line-height: 14px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
  .key { display: flex; gap: 12px; justify-content: center; font: 500 11px/14px var(--code-font-family, ui-monospace, monospace); color: var(--ink-2); }
  .key span { display: inline-flex; align-items: center; gap: 5px; }
  .k { display: inline-block; width: 10px; height: 10px; border-radius: 2px; background: var(--ink-2); }
  .k.light { opacity: .45; }
  .k.striped { background: repeating-linear-gradient(45deg, var(--ink-2) 0 2px, transparent 2px 4px); }
  .empty { padding: 24px 8px; text-align: center; color: var(--ink-2); font-size: 13px; line-height: 18px; }
  @media (prefers-reduced-motion: reduce) { .seg button { transition: none; } }
`;


const BASIC_SCHEMA = [
  { name: "title", selector: { text: {} } },
  {
    type: "grid", name: "", schema: [
      { name: "days", selector: { number: { min: 3, max: 31, mode: "box" } } },
      { name: "unit", selector: { select: { mode: "dropdown", options: [{ value: "kwh", label: "kWh" }, { value: "gbp", label: "£" }] } } },
      { name: "day", selector: { select: { mode: "dropdown", options: [{ value: "today", label: "Today" }, { value: "settled", label: "Last settled day" }] } } },
    ],
  },
  { name: "logo", selector: { text: {} } },
];
const LABELS = { title: "Title", days: "Days shown", unit: "Start in", day: "Open on", logo: "Logo image URL (e.g. /local/edf.svg)" };

/** Visual editor: the basics, then each series (shown, colour, order) and up to four hero figures. */
class EdfUsageCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._form) this._form.hass = hass;
    if (first) this._load();
  }

  async _load() {
    try {
      const { meters } = await this._hass.callWS({ type: "edf_kraken/usage" });
      const meter = meters.find((m) => m.mpan === this._config.mpan) ?? meters[0];
      this._available = meter ? meter.series.map((s) => s.name) : [];
    } catch {
      this._available = [];
    }
    this._render();
  }

  /** Every series in display order: configured ones first (shown), then the rest (hidden, or all shown if none are configured). */
  _rows() {
    const conf = (this._config.series ?? []).map((c) => (typeof c === "string" ? { id: c } : c));
    const configured = conf.length > 0;
    const rows = conf.map((c) => ({ name: c.name ?? c.id, colour: c.colour ?? "", shown: true }));
    for (const name of this._available ?? []) {
      if (!rows.some((r) => r.name === name)) rows.push({ name, colour: "", shown: !configured });
    }
    return rows;
  }

  _changed(config) {
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
    this._render();
  }

  _saveRows(rows) {
    const series = rows.filter((r) => r.shown).map((r) => (r.colour ? { id: r.name, colour: r.colour } : { id: r.name }));
    const config = { ...this._config, series };
    if (!series.length) delete config.series;
    this._changed(config);
  }

  _saveFigures(figures) {
    const config = { ...this._config, figures: figures.filter(Boolean) };
    if (!config.figures.length) delete config.figures;
    this._changed(config);
  }

  _render() {
    if (!this._config) return;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${EDITOR_STYLES}</style><div class="basic"></div><div class="extra"></div>`;
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => LABELS[s.name] ?? s.name;
      this._form.addEventListener("value-changed", (e) => this._changed({ ...this._config, ...e.detail.value }));
      this.shadowRoot.querySelector(".basic").appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = BASIC_SCHEMA;
    this._form.data = this._config;

    const rows = this._rows();
    const colourOptions = (sel) => [["", "Auto"], ...Object.keys(NAMED).map((n) => [n, n[0].toUpperCase() + n.slice(1)])]
      .map(([v, l]) => `<option value="${v}" ${v === sel ? "selected" : ""}>${l}</option>`).join("")
      + (sel && !(sel in NAMED) ? `<option value="${esc(sel)}" selected>${esc(sel)}</option>` : "");
    const seriesHtml = rows.length
      ? rows.map((r, i) => `<div class="srow ${r.shown ? "" : "off"}" data-i="${i}">
          <input type="checkbox" class="shown" ${r.shown ? "checked" : ""} aria-label="Show ${esc(r.name)}">
          <i style="background:${colourOf(r.colour, rows.filter((x, j) => x.shown && j < i).length, false)}"></i>
          <span class="nm">${esc(r.name)}</span>
          <select class="colour" aria-label="Colour">${colourOptions(r.colour)}</select>
          <button class="up" ${i === 0 ? "disabled" : ""} aria-label="Move up">▲</button>
          <button class="down" ${i === rows.length - 1 ? "disabled" : ""} aria-label="Move down">▼</button></div>`).join("")
      : `<div class="hint">${this._available ? "No circuits or tracked devices yet." : "Loading…"}</div>`;

    const figures = [...(this._config.figures ?? []).map((f) => (typeof f === "string" ? f : f.series)), "", "", "", ""].slice(0, 4);
    const figOptions = (sel) => [["", "None"], ["total", "Total (meter)"], ...rows.map((r) => [r.name, r.name])]
      .map(([v, l]) => `<option value="${esc(v)}" ${v === sel ? "selected" : ""}>${esc(l)}</option>`).join("");
    const figHtml = figures.map((f, i) => `<label>Figure ${i + 1}<select class="fig" data-i="${i}">${figOptions(f)}</select></label>`).join("");

    const extra = this.shadowRoot.querySelector(".extra");
    extra.innerHTML = `<h3>Bars</h3><div class="hint">Tick what to show, pick colours and set the order with the arrows.</div>${seriesHtml}
      <h3>Hero figures</h3><div class="hint">Up to four across the top, each with its off-peak share. Leave them all as None for the standard Today / Off-peak / Peak.</div>
      <div class="figs">${figHtml}</div>`;

    extra.querySelectorAll(".srow").forEach((el) => {
      const i = Number(el.dataset.i);
      el.querySelector(".shown").addEventListener("change", (e) => { rows[i].shown = e.target.checked; this._saveRows(rows); });
      el.querySelector(".colour").addEventListener("change", (e) => { rows[i].colour = e.target.value; this._saveRows(rows); });
      el.querySelector(".up").addEventListener("click", () => { [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]]; this._saveRows(rows); });
      el.querySelector(".down").addEventListener("click", () => { [rows[i + 1], rows[i]] = [rows[i], rows[i + 1]]; this._saveRows(rows); });
    });
    extra.querySelectorAll(".fig").forEach((el) => {
      el.addEventListener("change", (e) => { figures[Number(el.dataset.i)] = e.target.value; this._saveFigures(figures); });
    });
  }
}

const EDITOR_STYLES = `
  :host { display: block; }
  h3 { margin: 20px 0 4px; font-size: 15px; font-weight: 500; }
  .hint { font-size: 12px; line-height: 16px; color: var(--secondary-text-color); margin-bottom: 8px; }
  .srow { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--divider-color); }
  .srow.off .nm, .srow.off i { opacity: .45; }
  .srow i { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .srow .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  select { font: inherit; font-size: 14px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color, transparent); color: var(--primary-text-color); }
  button { font: inherit; font-size: 11px; width: 30px; height: 30px; border-radius: 6px; border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); cursor: pointer; }
  button:disabled { opacity: .3; cursor: default; }
  input[type=checkbox] { width: 18px; height: 18px; margin: 0; }
  .figs { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
  .figs label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--secondary-text-color); }
`;

if (!customElements.get("edf-usage-card-editor")) customElements.define("edf-usage-card-editor", EdfUsageCardEditor);
if (!customElements.get("edf-usage-card")) {
  customElements.define("edf-usage-card", EdfUsageCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "edf-usage-card",
    name: "EDF usage",
    description: "Daily peak and off-peak use for each circuit and device, in kWh or £, settled by EDF.",
    preview: false,
  });
}

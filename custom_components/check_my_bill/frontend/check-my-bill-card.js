/*
 * Check My Bill card (docs/specs/card.md).
 *
 * Plain custom elements with escaped template strings: no build step, no imports, and no
 * reliance on Home Assistant's bundled lit. The only private frontend API used is the
 * Energy date-picker collection, isolated in energyCollection().
 */

const DOMAIN = "check_my_bill";
const CARD = "check-my-bill-card";
const EDITOR = "check-my-bill-card-editor";
const REFRESH_MS = 5 * 60 * 1000;
const ENERGY_RETRIES = 10;
const MAX_RANGE_DAYS = 400;
const DAY_MS = 24 * 60 * 60 * 1000;

const STATUS = {
  ok: ["OK", "var(--success-color, #43a047)"],
  warn: ["Check", "var(--warning-color, #ffa600)"],
  mismatch: ["Mismatch", "var(--error-color, #db4437)"],
  no_data: ["No utility data", "var(--disabled-text-color, #9e9e9e)"],
  in_progress: ["In progress", "var(--info-color, #039be5)"],
};

const COST_LINES = [
  ["supply", "Supply"],
  ["delivery", "Delivery"],
  ["riders", "Riders"],
  ["fixed", "Fixed charges"],
  ["tax", "Tax"],
];

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

/* ---------- dates: ISO "YYYY-MM-DD" strings are local calendar dates ---------- */

const pad = (n) => String(n).padStart(2, "0");
const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDate = (iso) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
};
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const daysBetween = (a, b) => Math.round((b - a) / DAY_MS);
const today = () => {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
};

/**
 * The Energy dashboard's date-selection collection, or null.
 *
 * This is a private frontend API (bill-validation.md §2.4): every access to it lives here,
 * so a frontend change disables follow_energy mode and nothing else.
 */
function energyCollection(hass, key) {
  try {
    const conn = hass && hass.connection;
    if (!conn) return null;
    const segment = window.location.pathname.split("/")[1] || "";
    const keys = key ? [`_${key}`] : [`_energy_${segment}`, "_energy"];
    for (const k of keys) {
      const collection = conn[k];
      if (collection && typeof collection.subscribe === "function") return collection;
    }
  } catch (_err) {
    // Fall through: treated as "no date picker".
  }
  return null;
}

class CheckMyBillCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._expanded = new Set();
    this._onClick = this._onClick.bind(this);
    this._onKey = this._onKey.bind(this);
    this._onVisible = this._onVisible.bind(this);
    this.shadowRoot.addEventListener("click", this._onClick);
    this.shadowRoot.addEventListener("keydown", this._onKey);
  }

  static getConfigElement() {
    return document.createElement(EDITOR);
  }

  static getStubConfig() {
    return { mode: "cycles", cycles: 6 };
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    const mode = config.mode ?? "cycles";
    if (!["cycles", "follow_energy"].includes(mode)) {
      throw new Error("mode must be 'cycles' or 'follow_energy'");
    }
    const cycles = Number(config.cycles ?? 6);
    if (!Number.isInteger(cycles) || cycles < 1 || cycles > 24) {
      throw new Error("cycles must be a whole number from 1 to 24");
    }
    const old = this._config;
    this._config = { ...config, mode, cycles };
    const changed =
      old && (old.mode !== mode || ["entry_id", "collection_key"].some((k) => old[k] !== config[k]));
    if (changed) {
      this._resetData();
      if (this.isConnected && this._hass) this._start();
    }
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._start();
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    return 3 + Math.ceil((this._config?.cycles ?? 6) / 2);
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisible);
    if (this._hass) this._start();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisible);
    this._stop();
  }

  /* ---------- lifecycle ---------- */

  _resetData() {
    this._stop();
    this._data = null;
    this._entryId = null;
    this._error = null;
    this._range = null;
    this._rangeKey = null;
    this._rangeNote = null;
    this._energyNote = null;
    this._expanded.clear();
  }

  _start() {
    if (!this._config || this._timer) return;
    this._timer = window.setInterval(() => this._load(), REFRESH_MS);
    this._load();
    if (this._config.mode === "follow_energy") this._attachEnergy(0);
  }

  _stop() {
    window.clearInterval(this._timer);
    window.clearTimeout(this._energyRetry);
    this._timer = null;
    this._energyRetry = null;
    if (this._unsubEnergy) {
      try {
        this._unsubEnergy();
      } catch (_err) {
        // The collection may already be gone with its dashboard.
      }
    }
    this._unsubEnergy = null;
  }

  _onVisible() {
    if (document.visibilityState === "visible" && this._timer) this._load();
  }

  async _resolveEntry() {
    if (this._config.entry_id) return this._config.entry_id;
    if (this._entryId) return this._entryId;
    const entries = await this._hass.callWS({ type: "config_entries/get", domain: DOMAIN });
    if (entries.length !== 1) {
      throw new Error(
        entries.length
          ? "Several Check My Bill entries exist: pick one in the card editor."
          : "No Check My Bill integration is set up.",
      );
    }
    this._entryId = entries[0].entry_id;
    return this._entryId;
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      const entryId = await this._resolveEntry();
      this._data = await this._hass.callWS({ type: `${DOMAIN}/cycles`, entry_id: entryId });
      this._error = null;
    } catch (err) {
      this._error = err?.message ?? String(err);
    } finally {
      this._loading = false;
    }
    this._render();
  }

  /* ---------- follow_energy ---------- */

  _attachEnergy(attempt) {
    if (this._unsubEnergy || !this._timer) return;
    const collection = energyCollection(this._hass, this._config.collection_key);
    if (collection) {
      this._energyNote = null;
      try {
        this._unsubEnergy = collection.subscribe((data) => this._onEnergy(data));
      } catch (_err) {
        this._energyNote = "Energy date picker not usable; showing billing cycles.";
      }
      this._render();
      return;
    }
    if (attempt < ENERGY_RETRIES) {
      this._energyRetry = window.setTimeout(() => this._attachEnergy(attempt + 1), 1000);
      return;
    }
    this._energyNote = "Energy date picker not found; showing billing cycles.";
    this._render();
  }

  async _onEnergy(data) {
    if (!data?.start) return;
    const start = isoDate(new Date(data.start));
    // The picker's end is the last moment of the last day; the API's end is exclusive.
    const lastDay = data.end ? new Date(data.end) : today();
    const end = isoDate(addDays(lastDay, 1));
    const key = `${start}/${end}`;
    if (key === this._rangeKey) return;
    this._rangeKey = key;
    this._range = null;
    this._rangeNote = null;
    if (daysBetween(parseDate(start), parseDate(end)) > MAX_RANGE_DAYS) {
      this._rangeNote = `Pick a range of ${MAX_RANGE_DAYS} days or less to see it here.`;
      this._render();
      return;
    }
    this._render();
    try {
      const entryId = await this._resolveEntry();
      const record = await this._hass.callWS({
        type: `${DOMAIN}/range`,
        entry_id: entryId,
        start,
        end,
      });
      if (this._rangeKey === key) this._range = record;
    } catch (err) {
      if (this._rangeKey === key) this._rangeNote = err?.message ?? String(err);
    }
    this._render();
  }

  /* ---------- events ---------- */

  _toggle(row, keyboard) {
    const start = row.dataset.start;
    if (this._expanded.has(start)) this._expanded.delete(start);
    else this._expanded.add(start);
    this._render();
    if (keyboard) this.shadowRoot.querySelector(`.row[data-start="${start}"]`)?.focus();
  }

  _onClick(ev) {
    const row = ev.composedPath().find((el) => el.classList?.contains("row"));
    if (row) this._toggle(row);
  }

  _onKey(ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const row = ev.composedPath().find((el) => el.classList?.contains("row"));
    if (!row) return;
    ev.preventDefault();
    this._toggle(row, true);
  }

  /* ---------- formatting ---------- */

  get _lang() {
    return this._hass?.locale?.language ?? this._hass?.language ?? navigator.language;
  }

  _kwh(value, digits = 0) {
    if (value === null || value === undefined) return "—";
    return new Intl.NumberFormat(this._lang, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }

  _money(value) {
    if (value === null || value === undefined) return "—";
    try {
      return new Intl.NumberFormat(this._lang, {
        style: "currency",
        currency: this._data?.currency || "USD",
      }).format(value);
    } catch (_err) {
      return value.toFixed(2);
    }
  }

  _pct(value) {
    return new Intl.NumberFormat(this._lang, { style: "percent", maximumFractionDigits: 0 }).format(
      value,
    );
  }

  _dates(record) {
    const first = parseDate(record.start);
    const last = parseDate(record.last_day);
    const thisYear = today().getFullYear();
    const short = { month: "short", day: "numeric" };
    const withYear = { ...short, year: "numeric" };
    const a = first.toLocaleDateString(this._lang, first.getFullYear() === thisYear ? short : withYear);
    const b = last.toLocaleDateString(this._lang, last.getFullYear() === thisYear ? short : withYear);
    const est = record.estimated ? ' <span class="est" title="Estimated dates">est.</span>' : "";
    return `${esc(a)} – ${esc(b)}${est}`;
  }

  _status(status) {
    return STATUS[status] ?? [status, "var(--secondary-text-color)"];
  }

  _pill(status) {
    const [label, color] = this._status(status);
    return `<span class="pill" style="--pill:${color}"><span class="dot"></span>${esc(label)}</span>`;
  }

  /* ---------- rendering ---------- */

  /** Two horizontal bars comparing Home Assistant's meter with the utility's. */
  _meters(record) {
    const ha = record.metered_kwh ?? 0;
    const utility = record.utility_kwh ?? 0;
    const max = Math.max(ha, utility, 1e-9);
    const coverage = record.coverage?.utility ?? 0;
    const partial =
      record.utility_kwh != null && coverage < 1
        ? ` <span class="muted">· ${this._pct(coverage)} reported</span>`
        : "";
    const bar = (name, cls, value, extra) => `
      <div class="meter">
        <div class="meter-label"><span>${name}</span><span class="num">${this._kwh(value)} kWh${extra}</span></div>
        <div class="track"><div class="fill ${cls}" style="width:${((value ?? 0) / max) * 100}%"></div></div>
      </div>`;
    return `<div class="meters">${bar("Home Assistant", "ha", record.metered_kwh, "")}${bar(
      "Utility",
      "utility",
      record.utility_kwh,
      partial,
    )}</div>`;
  }

  _summary(record, { label, projected }) {
    const hasTariff = this._data?.has_tariff;
    const coverage = record.coverage?.utility ?? 0;
    const start = parseDate(record.start);
    const total = daysBetween(start, parseDate(record.end));
    const day = Math.min(Math.max(daysBetween(start, today()) + 1, 1), total);
    const open = record.status === "in_progress";

    const progress = open
      ? `
        <div class="progress" role="progressbar" aria-valuemin="1" aria-valuemax="${total}"
             aria-valuenow="${day}" aria-label="Day ${day} of ${total}">
          <div class="progress-fill" style="width:${(day / total) * 100}%"></div>
        </div>
        <div class="progress-label muted"><span>Day ${day} of ${total}</span>
          <span>${total - day === 1 ? "1 day left" : `${total - day} days left`}</span></div>`
      : "";

    let figures = "";
    if (hasTariff) {
      const main =
        projected !== undefined
          ? ["Projected bill", this._money(projected?.total)]
          : ["Expected cost", this._money(record.expected_cost?.total)];
      const side = projected !== undefined
        ? [["So far", this._money(record.expected_cost?.total)]]
        : [];
      figures = `
        <div class="figures">
          <div class="figure main"><div class="figure-label">${esc(main[0])}</div>
            <div class="figure-value">${main[1]}</div></div>
          ${side
            .map(
              ([k, v]) => `<div class="figure"><div class="figure-label">${esc(k)}</div>
                <div class="figure-value">${v}</div></div>`,
            )
            .join("")}
        </div>`;
    }

    const lag =
      open && coverage < 1
        ? '<div class="note">Utility data lags 1–2 days behind the meter.</div>'
        : "";
    return `
      <section class="summary">
        <div class="head">
          <div>
            <div class="label">${esc(label)}</div>
            <div class="dates">${this._dates(record)}</div>
          </div>
          ${this._pill(record.status)}
        </div>
        ${progress}
        ${figures}
        ${this._meters(record)}
        ${lag}
      </section>`;
  }

  /** The tolerance band as a scale, with a marker where the utility's reading landed. */
  _band(record) {
    const [low, high] = record.band;
    const value = record.utility_kwh;
    const points = [low, high, ...(value == null ? [] : [value])];
    const span = Math.max(...points) - Math.min(...points) || 1;
    const lo = Math.min(...points) - span * 0.35;
    const hi = Math.max(...points) + span * 0.35;
    const at = (v) => `${((v - lo) / (hi - lo)) * 100}%`;
    const [, color] = this._status(record.status);
    const tol = this._data?.tolerance_pct;
    let caption = `Expected utility reading ${this._kwh(low)}–${this._kwh(high)} kWh`;
    if (tol !== undefined) caption += ` (±${esc(tol)}%)`;
    if (record.deviation_pct) {
      const sign = record.deviation_pct > 0 ? "+" : "";
      caption += `; utility is ${sign}${this._kwh(record.deviation_pct, 1)}% outside it`;
    }
    const marker =
      value == null
        ? ""
        : `<div class="band-marker" style="left:${at(value)};--pill:${color}"
              title="Utility ${this._kwh(value)} kWh"></div>`;
    return `
      <div class="block">
        <div class="block-title">Reconciliation</div>
        <div class="band" aria-hidden="true">
          <div class="band-range" style="left:${at(low)};width:calc(${at(high)} - ${at(low)})"></div>
          ${marker}
        </div>
        <div class="muted">${caption}.</div>
      </div>`;
  }

  _details(record) {
    const blocks = [];
    const cost = record.expected_cost;
    if (cost) {
      const rows = COST_LINES.map(([key, name]) => [name, cost[key]]);
      if (cost.export_credit) rows.push(["Export credit", -cost.export_credit]);
      const billed =
        record.bill?.cost != null
          ? `<tr class="billed"><td>Utility billed</td><td>${this._money(record.bill.cost)}</td></tr>`
          : "";
      blocks.push(`
        <div class="block">
          <div class="block-title">Expected cost</div>
          <table class="cost">${rows
            .map(([name, v]) => `<tr><td>${esc(name)}</td><td>${this._money(v)}</td></tr>`)
            .join("")}
            <tr class="total"><td>Total</td><td>${this._money(cost.total)}</td></tr>${billed}
          </table>
          <div class="muted">Priced on ${
            record.cost_basis === "utility" ? "the utility's" : "Home Assistant's"
          } ${this._kwh(cost.billed_kwh, 1)} kWh.</div>
        </div>`);
    }
    const side = [this._meters(record)];
    if (record.band) side.push(this._band(record));
    if (this._data?.export_mode && this._data.export_mode !== "none") {
      side.push(
        `<div class="muted">Export: HA ${this._kwh(record.export_kwh, 1)} kWh, utility ${this._kwh(
          record.utility_export_kwh,
          1,
        )} kWh.</div>`,
      );
    }
    if (record.status === "no_data") {
      side.push('<div class="muted">The utility has no data for these dates yet.</div>');
    }
    blocks.push(`<div class="block">${side.join("")}</div>`);
    return `<div class="details">${blocks.join("")}</div>`;
  }

  _table(cycles) {
    if (!cycles.length) return '<div class="muted">No previous cycles yet.</div>';
    const hasTariff = this._data?.has_tariff;
    const rows = cycles
      .map((c) => {
        const open = this._expanded.has(c.start);
        const [label, color] = this._status(c.status);
        const right = hasTariff
          ? `<div class="row-cost">${this._money(c.expected_cost?.total)}${
              c.bill?.cost != null
                ? `<div class="muted small">billed ${this._money(c.bill.cost)}</div>`
                : ""
            }</div>`
          : "";
        return `
          <div class="cycle${open ? " open" : ""}">
            <div class="row" data-start="${esc(c.start)}" tabindex="0" role="button"
                 aria-expanded="${open}">
              <span class="status-dot" style="--pill:${color}" title="${esc(label)}"></span>
              <div class="row-main">
                <div class="row-dates">${this._dates(c)}</div>
                <div class="muted small">${esc(label)} · HA ${this._kwh(c.metered_kwh)} · utility ${this._kwh(
                  c.utility_kwh,
                )} kWh</div>
              </div>
              ${right}
              <svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="M7.41 8.58 12 13.17l4.59-4.59L18 10l-6 6-6-6z"/></svg>
            </div>
            ${open ? this._details(c) : ""}
          </div>`;
      })
      .join("");
    return `
      <section>
        <div class="label section-title">Past cycles</div>
        <div class="cycles">${rows}</div>
      </section>`;
  }

  _content() {
    if (this._error) return `<div class="error">${esc(this._error)}</div>`;
    if (!this._data) return '<div class="muted">Loading…</div>';
    const parts = [];
    if (this._energyNote) parts.push(`<div class="note">${esc(this._energyNote)}</div>`);
    const following = this._config.mode === "follow_energy" && this._unsubEnergy;
    if (following && this._rangeNote) {
      parts.push(`<div class="note">${esc(this._rangeNote)}</div>`);
    } else if (following && this._range) {
      parts.push(this._summary({ ...this._range, estimated: false }, { label: "Selected range" }));
    } else if (following) {
      parts.push('<div class="muted">Loading the selected range…</div>');
    } else {
      const current = this._data.current;
      parts.push(
        this._summary(current, { label: "Current cycle", projected: current.projected_cost }),
      );
    }
    parts.push(this._table(this._data.cycles.slice(0, this._config.cycles)));
    return parts.join("");
  }

  _render() {
    if (!this._config) return;
    const title = this._config.title ?? "Electric bill";
    const header = title
      ? `<div class="card-header">
          <span class="badge" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M11 15H6l7-14v8h5l-7 14v-8z"/></svg></span>
          <h1>${esc(title)}</h1>
        </div>`
      : "";
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <ha-card>
        ${header}
        <div class="content">${this._content()}</div>
      </ha-card>`;
  }
}

const STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .card-header { display: flex; align-items: center; gap: 12px; padding: 16px 16px 0; }
  .card-header h1 { margin: 0; font-size: 20px; font-weight: 400; line-height: 1.2;
    color: var(--ha-card-header-color, var(--primary-text-color)); }
  .badge { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 50%;
    flex: none; color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 15%, transparent); }
  .badge svg { width: 20px; height: 20px; fill: currentColor; }
  .content { container-type: inline-size; padding: 16px; display: flex; flex-direction: column; gap: 20px; }
  .muted { color: var(--secondary-text-color); }
  .small { font-size: 0.85em; }
  .num { font-variant-numeric: tabular-nums; }
  .note { color: var(--secondary-text-color); font-size: 0.85em; font-style: italic; }
  .error { color: var(--error-color, #db4437); }
  .label { font-size: 0.75em; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--secondary-text-color); }
  .est { font-size: 0.7em; font-weight: 500; padding: 1px 6px; border-radius: 8px;
    vertical-align: middle; color: var(--secondary-text-color);
    border: 1px dashed var(--divider-color, #bdbdbd); }

  /* summary */
  .summary { display: flex; flex-direction: column; gap: 14px; }
  .summary .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .dates { font-size: 1.25em; font-weight: 500; margin-top: 2px; }
  .pill { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
    padding: 3px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 500; color: var(--pill);
    background: color-mix(in srgb, var(--pill) 14%, transparent); }
  .pill .dot, .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--pill);
    flex: none; }
  .progress { height: 8px; border-radius: 4px; overflow: hidden;
    background: color-mix(in srgb, var(--primary-color) 15%, transparent); }
  .progress-fill { height: 100%; border-radius: 4px; background: var(--primary-color); }
  .progress-label { display: flex; justify-content: space-between; font-size: 0.85em; margin-top: -8px; }
  .figures { display: flex; flex-wrap: wrap; gap: 8px 32px; align-items: flex-end; }
  .figure-label { font-size: 0.85em; color: var(--secondary-text-color); }
  .figure-value { font-size: 1.4em; font-weight: 500; font-variant-numeric: tabular-nums; }
  .figure.main .figure-value { font-size: 2.2em; font-weight: 400; line-height: 1.1; }

  /* meter comparison */
  .meters { display: flex; flex-direction: column; gap: 8px; }
  .meter-label { display: flex; justify-content: space-between; gap: 8px; font-size: 0.85em;
    margin-bottom: 3px; }
  .track { height: 6px; border-radius: 3px; background: var(--divider-color, #e0e0e0); overflow: hidden; }
  .fill { height: 100%; border-radius: 3px; }
  .fill.ha { background: var(--primary-color); }
  .fill.utility { background: var(--energy-grid-consumption-color, #488fc2); }

  /* past cycles */
  .section-title { margin-bottom: 6px; }
  .cycle { border-top: 1px solid var(--divider-color, #e0e0e0); }
  .cycle:last-child { border-bottom: 1px solid var(--divider-color, #e0e0e0); }
  .row { display: grid; grid-template-columns: 8px 1fr auto 20px; align-items: center; gap: 12px;
    padding: 10px 8px; margin: 0 -8px; cursor: pointer; border-radius: 8px; outline: none; }
  .row:hover, .row:focus-visible { background: var(--secondary-background-color, rgba(127,127,127,.08)); }
  .row:focus-visible { box-shadow: inset 0 0 0 2px var(--primary-color); }
  .row-dates { font-weight: 500; }
  .row-cost { text-align: right; font-weight: 500; font-variant-numeric: tabular-nums; }
  .row-cost .muted { font-weight: 400; }
  .chevron { width: 20px; height: 20px; fill: var(--secondary-text-color); transition: transform .2s; }
  .cycle.open .chevron { transform: rotate(180deg); }

  /* details */
  .details { display: grid; grid-template-columns: 1fr; gap: 16px; padding: 4px 0 16px 20px; }
  @container (min-width: 480px) { .details { grid-template-columns: 1fr 1fr; gap: 24px; } }
  .block { display: flex; flex-direction: column; gap: 8px; font-size: 0.9em; min-width: 0; }
  .block .block { font-size: 1em; }
  .block-title { font-weight: 500; }
  table.cost { width: 100%; border-collapse: collapse; }
  .cost td { padding: 2px 0; }
  .cost td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
  .cost .total td { border-top: 1px solid var(--divider-color, #e0e0e0); font-weight: 500; padding-top: 4px; }
  .cost .billed td { color: var(--secondary-text-color); }
  .band { position: relative; height: 14px; }
  .band::before { content: ""; position: absolute; left: 0; right: 0; top: 5px; height: 4px;
    border-radius: 2px; background: var(--divider-color, #e0e0e0); }
  .band-range { position: absolute; top: 3px; height: 8px; border-radius: 4px;
    background: color-mix(in srgb, var(--success-color, #43a047) 35%, transparent); }
  .band-marker { position: absolute; top: 0; width: 4px; height: 14px; margin-left: -2px;
    border-radius: 2px; background: var(--pill); }
`;

/* ---------- visual editor ---------- */

const SCHEMA = [
  { name: "entry_id", selector: { config_entry: { integration: DOMAIN } } },
  { name: "title", selector: { text: {} } },
  {
    name: "mode",
    selector: {
      select: {
        mode: "dropdown",
        options: [
          { value: "cycles", label: "Billing cycles" },
          { value: "follow_energy", label: "Follow the Energy date picker" },
        ],
      },
    },
  },
  { name: "cycles", selector: { number: { min: 1, max: 24, mode: "box" } } },
];

const LABELS = {
  entry_id: "Check My Bill entry (optional with only one)",
  title: "Title",
  mode: "Mode",
  cycles: "Past cycles to list",
};

class CheckMyBillCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => LABELS[schema.name] ?? schema.name;
      this._form.addEventListener("value-changed", (ev) => this._changed(ev.detail.value));
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = SCHEMA;
    this._form.data = { mode: "cycles", cycles: 6, ...this._config };
  }

  _changed(value) {
    const config = { ...this._config, ...value };
    for (const key of ["entry_id", "title", "collection_key"]) {
      if (config[key] === "" || config[key] === null) delete config[key];
    }
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }),
    );
  }
}

if (!customElements.get(CARD)) customElements.define(CARD, CheckMyBillCard);
if (!customElements.get(EDITOR)) customElements.define(EDITOR, CheckMyBillCardEditor);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD)) {
  window.customCards.push({
    type: CARD,
    name: "Check My Bill",
    description: "Billing cycles checked against the utility's meter, with expected costs.",
    documentationURL: "https://github.com/mpreissner/ha-check-my-bill",
  });
}

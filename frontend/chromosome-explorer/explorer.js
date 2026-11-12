/*
 * GEPER Clinical -- Chromosome Explorer (clinician view).
 *
 * Dependency-free ES module. Everything that decides what is drawn is a PURE
 * function from the API response to a string, so it is tested under plain
 * `node --test` with no DOM and no build step. Only `mount()` touches the page.
 *
 * WHAT THIS PAGE IS NOT. It does not draw a karyotype, does not say a
 * chromosome or region is normal, and does not read "no call" as "no variant":
 * a layer with no call set is "not assessed", a layer with a call set but no call
 * on a chromosome is "no calls reported". Colour is never the only encoding
 * (position above/below the baseline, shape and a text label always accompany it).
 *
 * EVERY string that comes from the API reaches the page through esc(). Nothing is
 * assigned to innerHTML without passing through a function in this file.
 *
 * It calls ONLY the clinician route  GET /clinic/patients/{id}/genome/map  (and
 * .../map/{name}?start=&end=). It has no patient-facing mode.
 */

export const NS = "http://www.w3.org/2000/svg";

export function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function fmtBp(n) {
  return Number.isFinite(n) ? Math.round(n).toLocaleString("en-US") : "—";
}

export const LAYER_TEXT = {
  not_assessed: "Not assessed",
  no_calls_reported: "No calls reported (not a normal result)",
  findings_present: "Findings present",
  variants_present: "Variants present",
  no_variants_reported: "No variants reported (not a normal result)",
};

export function layerLabel(status) {
  return LAYER_TEXT[status] || "Unknown status";
}

const KIND_LABEL = {
  deletion: "Deletion",
  duplication: "Duplication",
  copy_number_change: "Copy-number change (direction not stated)",
  insertion: "Insertion",
  inversion: "Inversion",
  breakend: "Breakend",
  translocation: "Translocation (source-labelled)",
  small_variant: "Small variant",
};

// ── states ───────────────────────────────────────────────────────────────────

export function stateHtml(state, info = {}) {
  switch (state) {
    case "loading":
      return `<div class="state" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span>Loading genomic map…</div>`;
    case "empty":
      return `<div class="state" role="status"><h2>No genomic analysis</h2><p>This patient has no analysis that can be shown. That is not a finding about their genome.</p></div>`;
    case "error": {
      const known = {
        401: "You are not signed in. Sign in to the clinical service and reload.",
        403: "Your role does not permit viewing genomic data.",
        404: "No such patient in your organisation.",
        400: "The requested region is not valid.",
      };
      const text = known[info.status] || "The map could not be loaded. Try again; if it persists, contact support.";
      return `<div class="state state-error" role="alert"><h2>Could not load</h2><p>${esc(text)}</p></div>`;
    }
    case "unusable":
      return `<div class="state" role="status"><h2>No map for this analysis</h2><p>${esc(info.reason || "The analysis cannot be placed on chromosomes.")}</p></div>`;
    default:
      return "";
  }
}

// ── layout ───────────────────────────────────────────────────────────────────

/** Overview geometry: one bar per chromosome, all on ONE scale (the longest = full width). */
export function overviewLayout(map, { width = 960, gutter = 52, pad = 16, rowHeight = 22, gap = 6 } = {}) {
  const chroms = map.chromosomes || [];
  const longest = Math.max(1, ...chroms.map((c) => c.length_bp));
  const usable = Math.max(10, width - gutter - pad);
  const rows = chroms.map((c, i) => ({
    name: c.name,
    length_bp: c.length_bp,
    x: gutter,
    y: pad + i * (rowHeight + gap),
    w: (c.length_bp / longest) * usable,
    h: rowHeight,
    layers: c.layers,
  }));
  const height = pad * 2 + Math.max(0, chroms.length) * (rowHeight + gap);
  return { width, height, gutter, pad, rowHeight, rows };
}

function ringClass(layers) {
  const l = [layers.small_variants?.status, layers.cnv?.status, layers.sv?.status];
  return l.some((s) => s === "findings_present" || s === "variants_present") ? "has-findings" : "";
}

export function overviewSvg(map, { width = 960, selected = null } = {}) {
  const L = overviewLayout(map, { width });
  const parts = [`<svg class="overview" viewBox="0 0 ${L.width} ${L.height}" role="group" aria-label="Chromosome overview, ${esc(map.assembly)}">`];
  for (const r of L.rows) {
    const sel = r.name === selected;
    const label = `Chromosome ${r.name}, ${fmtBp(r.length_bp)} bp. Small variants: ${layerLabel(r.layers.small_variants?.status)}. CNV: ${layerLabel(r.layers.cnv?.status)}. SV: ${layerLabel(r.layers.sv?.status)}.`;
    parts.push(
      `<g class="chrom ${ringClass(r.layers)}${sel ? " selected" : ""}" data-chrom="${esc(r.name)}" tabindex="0" role="button" aria-pressed="${sel}" aria-label="${esc(label)}">` +
        `<text class="chrom-name" x="${L.gutter - 8}" y="${r.y + r.h * 0.7}" text-anchor="end">${esc(r.name)}</text>` +
        `<rect class="chrom-bar${r.layers.cnv?.status === "not_assessed" && r.layers.sv?.status === "not_assessed" ? " unassessed" : ""}" x="${r.x}" y="${r.y}" width="${r.w.toFixed(2)}" height="${r.h}" rx="${r.h / 2}"/>`,
    );
    for (const m of r.layers.small_variants?.markers || []) {
      parts.push(`<line class="mk-small" x1="${(r.x + m.frac * r.w).toFixed(2)}" x2="${(r.x + m.frac * r.w).toFixed(2)}" y1="${r.y + 3}" y2="${r.y + r.h - 3}"/>`);
    }
    for (const s of r.layers.cnv?.segments || []) {
      const x0 = r.x + s.x0 * r.w;
      const w = Math.max(2, (s.x1 - s.x0) * r.w);
      parts.push(`<rect class="mk-cnv ${esc(s.kind_class)}" x="${x0.toFixed(2)}" y="${s.cnv_type === "deletion" ? r.y + r.h / 2 : r.y}" width="${w.toFixed(2)}" height="${r.h / 2}"/>`);
    }
    for (const s of r.layers.sv?.intervals || []) {
      const x0 = r.x + s.x0 * r.w;
      parts.push(`<rect class="mk-svi ${esc(s.kind_class)}" x="${x0.toFixed(2)}" y="${r.y - 2}" width="${Math.max(2, (s.x1 - s.x0) * r.w).toFixed(2)}" height="3"/>`);
    }
    for (const b of r.layers.sv?.breakends || []) {
      const cx = r.x + b.breakpoint.frac * r.w;
      parts.push(`<path class="mk-bnd" d="M${cx.toFixed(2)} ${r.y - 4} l4 4 l-4 4 l-4 -4 z"/>`);
    }
    parts.push(`</g>`);
  }
  parts.push(`</svg>`);
  return parts.join("");
}

/** Detail geometry for one chromosome: a full-width track stack. */
export function detailLayout(chrom, { width = 960, gutter = 120, pad = 16 } = {}) {
  const usable = Math.max(10, width - gutter - pad);
  const lanes = (arr) => 1 + Math.max(-1, ...arr.map((a) => a.lane ?? 0));
  const cnvLanes = Math.max(1, lanes(chrom.layers.cnv?.segments || []));
  const svIntervals = chrom.layers.sv?.intervals || [];
  const svLaneCount = Math.max(1, packLanes(svIntervals).count);
  const track = { ruler: 28, small: 34, cnv: 14 + cnvLanes * 18, svi: 14 + svLaneCount * 16, bnd: 40 };
  let y = pad;
  const tops = {};
  for (const k of ["ruler", "small", "cnv", "svi", "bnd"]) {
    tops[k] = y;
    y += track[k] + 10;
  }
  return { width, height: y + pad, gutter, pad, usable, tops, track };
}

export function packLanes(items) {
  const order = items.map((it, i) => i).sort((a, b) => items[a].start.pos - items[b].start.pos || a - b);
  const ends = [];
  const lane = new Array(items.length).fill(0);
  for (const i of order) {
    const s = items[i].start.pos;
    const e = items[i].end.pos;
    let k = ends.findIndex((last) => s > last);
    if (k === -1) {
      k = ends.length;
      ends.push(e);
    } else ends[k] = e;
    lane[i] = k;
  }
  return { lane, count: ends.length };
}

function rangeRect(bp, x, w, y, h, cls) {
  // Uncertainty is drawn as a RANGE. An unquantified one gets a dashed marker, not a point.
  if (bp.precision === "interval") {
    const a = x + bp.ci_low_frac * w;
    const b = x + bp.ci_high_frac * w;
    return `<rect class="ci ${cls}" x="${a.toFixed(2)}" y="${y}" width="${Math.max(3, b - a).toFixed(2)}" height="${h}"/>`;
  }
  if (bp.precision === "imprecise_unquantified") {
    const c = x + bp.frac * w;
    return `<rect class="ci unquantified ${cls}" x="${(c - 5).toFixed(2)}" y="${y}" width="10" height="${h}"/>`;
  }
  return "";
}

export function detailSvg(chrom, { width = 960, selectedId = null, ticks = 8 } = {}) {
  const D = detailLayout(chrom, { width });
  const x = D.gutter;
  const w = D.usable;
  const out = [`<svg class="detail" viewBox="0 0 ${D.width} ${D.height}" role="group" aria-label="Chromosome ${esc(chrom.name)} tracks">`];
  const labelFor = (key, text, status) =>
    `<text class="track-label" x="${x - 10}" y="${D.tops[key] + 16}" text-anchor="end">${esc(text)}</text>` +
    `<text class="track-status" x="${x - 10}" y="${D.tops[key] + 30}" text-anchor="end">${esc(layerLabelShort(status))}</text>`;

  // ruler
  out.push(`<line class="ruler" x1="${x}" x2="${x + w}" y1="${D.tops.ruler + 20}" y2="${D.tops.ruler + 20}"/>`);
  for (let i = 0; i <= ticks; i++) {
    const tx = x + (i / ticks) * w;
    out.push(`<line class="tick" x1="${tx.toFixed(2)}" x2="${tx.toFixed(2)}" y1="${D.tops.ruler + 16}" y2="${D.tops.ruler + 24}"/>`);
    out.push(`<text class="tick-label" x="${tx.toFixed(2)}" y="${D.tops.ruler + 12}" text-anchor="middle">${esc(fmtMb((i / ticks) * chrom.length_bp))}</text>`);
  }
  out.push(`<text class="track-label" x="${x - 10}" y="${D.tops.ruler + 24}" text-anchor="end">Chr ${esc(chrom.name)}</text>`);

  const unassessed = (key, status, label) => {
    out.push(labelFor(key, label, status));
    out.push(`<rect class="band ${status === "not_assessed" ? "unassessed" : "empty"}" x="${x}" y="${D.tops[key]}" width="${w}" height="${D.track[key]}"/>`);
    out.push(`<text class="band-text" x="${x + w / 2}" y="${D.tops[key] + D.track[key] / 2 + 4}" text-anchor="middle">${esc(layerLabel(status))}</text>`);
  };

  // small variants
  const sm = chrom.layers.small_variants;
  if (sm.status === "not_assessed") unassessed("small", sm.status, "Small variants");
  else {
    out.push(labelFor("small", "Small variants", sm.status));
    out.push(`<line class="baseline" x1="${x}" x2="${x + w}" y1="${D.tops.small + 20}" y2="${D.tops.small + 20}"/>`);
    for (const m of sm.markers || []) {
      const mx = x + m.frac * w;
      out.push(`<line class="mk-small ${selectedId === m.id ? "selected" : ""}" data-finding="${esc(m.id)}" x1="${mx.toFixed(2)}" x2="${mx.toFixed(2)}" y1="${D.tops.small + 6}" y2="${D.tops.small + 30}" tabindex="0" role="button" aria-label="${esc(`Small variant at ${fmtBp(m.pos)}`)}"/>`);
    }
    if (!sm.markers?.length) out.push(`<text class="band-text" x="${x + w / 2}" y="${D.tops.small + 24}" text-anchor="middle">${esc(layerLabel(sm.status))}</text>`);
  }

  // CNV
  const cnv = chrom.layers.cnv;
  if (cnv.status === "not_assessed" || cnv.status === "no_calls_reported") unassessed("cnv", cnv.status, "Copy number");
  else {
    out.push(labelFor("cnv", "Copy number", cnv.status));
    const base = D.tops.cnv + 8;
    for (const s of cnv.segments) {
      const sx = x + s.x0 * w;
      const sw = Math.max(3, (s.x1 - s.x0) * w);
      const sy = base + s.lane * 18;
      const cls = `cnv-seg ${esc(s.cnv_type)} ${s.status === "uncertain" ? "uncertain" : ""} ${selectedId === s.id ? "selected" : ""}`;
      out.push(`<g class="${cls}" data-finding="${esc(s.id)}" tabindex="0" role="button" aria-label="${esc(`${KIND_LABEL[s.kind_class] || "CNV"} ${fmtBp(s.start)} to ${fmtBp(s.end)}, ${s.status}`)}">`);
      if (s.start_breakpoint) out.push(rangeRect(s.start_breakpoint, x, w, sy - 1, 12, "cnv-ci"));
      if (s.end_breakpoint) out.push(rangeRect(s.end_breakpoint, x, w, sy - 1, 12, "cnv-ci"));
      out.push(`<rect class="seg" x="${sx.toFixed(2)}" y="${sy}" width="${sw.toFixed(2)}" height="10" rx="2"/>`);
      out.push(`<text class="seg-glyph" x="${(sx + 3).toFixed(2)}" y="${sy + 8}">${s.cnv_type === "deletion" ? "▼" : s.cnv_type === "duplication" ? "▲" : "◆"}</text></g>`);
    }
  }

  // SV intervals
  const sv = chrom.layers.sv;
  if (sv.status === "not_assessed" || sv.status === "no_calls_reported") {
    unassessed("svi", sv.status, "Structural variants");
  } else {
    out.push(labelFor("svi", "Structural variants", sv.status));
    const packed = packLanes(sv.intervals);
    sv.intervals.forEach((s, i) => {
      const sx = x + s.x0 * w;
      const sw = Math.max(3, (s.x1 - s.x0) * w);
      const sy = D.tops.svi + 8 + packed.lane[i] * 16;
      out.push(`<g class="sv-int ${esc(s.kind_class)} ${s.status === "uncertain" ? "uncertain" : ""} ${selectedId === s.id ? "selected" : ""}" data-finding="${esc(s.id)}" tabindex="0" role="button" aria-label="${esc(`${KIND_LABEL[s.kind_class] || "SV"} ${fmtBp(s.start.pos)} to ${fmtBp(s.end.pos)}, ${s.status}`)}">`);
      out.push(rangeRect(s.start, x, w, sy - 1, 10, "sv-ci"));
      out.push(rangeRect(s.end, x, w, sy - 1, 10, "sv-ci"));
      out.push(`<rect class="seg" x="${sx.toFixed(2)}" y="${sy + 2}" width="${sw.toFixed(2)}" height="4"/>`);
      out.push(`<text class="seg-glyph" x="${(sx).toFixed(2)}" y="${sy + 10}">${esc(s.sv_type)}</text></g>`);
    });
    // breakends
    out.push(`<text class="track-label" x="${x - 10}" y="${D.tops.bnd + 16}" text-anchor="end">Breakends</text>`);
    out.push(`<line class="baseline" x1="${x}" x2="${x + w}" y1="${D.tops.bnd + 20}" y2="${D.tops.bnd + 20}"/>`);
    for (const b of sv.breakends) {
      const bx = x + b.breakpoint.frac * w;
      const glyph = b.sv_type === "INS" ? "ins" : "bnd";
      out.push(`<g class="sv-bnd ${glyph} ${b.end} ${b.status === "uncertain" ? "uncertain" : ""} ${selectedId === `${b.id}|${b.end}` ? "selected" : ""}" data-finding="${esc(`${b.id}|${b.end}`)}" tabindex="0" role="button" aria-label="${esc(`${KIND_LABEL[b.kind_class] || "Breakend"} at ${fmtBp(b.breakpoint.pos)}, ${b.end === "partner" ? "partner end, " : ""}${b.status}`)}">`);
      out.push(rangeRect(b.breakpoint, x, w, D.tops.bnd + 14, 12, "bnd-ci"));
      out.push(
        b.sv_type === "INS"
          ? `<path d="M${bx.toFixed(2)} ${D.tops.bnd + 8} l6 12 l-12 0 z"/>`
          : `<path d="M${bx.toFixed(2)} ${D.tops.bnd + 10} l6 10 l-6 10 l-6 -10 z"/>`,
      );
      out.push(`</g>`);
    }
    if (!sv.breakends.length && !sv.intervals.length) {
      out.push(`<text class="band-text" x="${x + w / 2}" y="${D.tops.bnd + 24}" text-anchor="middle">${esc(layerLabel(sv.status))}</text>`);
    }
  }
  out.push(`</svg>`);
  return out.join("");
}

function layerLabelShort(status) {
  return { not_assessed: "not assessed", no_calls_reported: "no calls reported", findings_present: "", variants_present: "", no_variants_reported: "no variants reported" }[status] ?? "";
}

function fmtMb(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)} Mb` : `${Math.round(n / 1e3)} kb`;
}

// ── findings: index and detail panel ────────────────────────────────────────

/** id -> { type, data, chrom } for every drawn finding, so selection is a lookup, not a refetch. */
export function findingIndex(map) {
  const idx = new Map();
  for (const c of map.chromosomes || []) {
    for (const m of c.layers.small_variants?.markers || []) idx.set(m.id, { type: "small_variant", data: m, chrom: c.name });
    for (const s of c.layers.cnv?.segments || []) idx.set(s.id, { type: "cnv", data: s, chrom: c.name });
    for (const s of c.layers.sv?.intervals || []) idx.set(s.id, { type: "sv_interval", data: s, chrom: c.name });
    for (const b of c.layers.sv?.breakends || []) idx.set(`${b.id}|${b.end}`, { type: "sv_breakend", data: b, chrom: c.name });
  }
  for (const l of map.links || []) idx.set(l.id + "|link", { type: "link", data: l, chrom: l.a.chrom });
  return idx;
}

function row(k, v) {
  return `<dt>${esc(k)}</dt><dd>${v === null || v === undefined || v === "" ? '<span class="na">not reported</span>' : esc(v)}</dd>`;
}

function bpText(bp) {
  if (!bp) return null;
  const where = `${bp.chrom ?? bp.chrom_raw}:${fmtBp(bp.pos)}`;
  if (bp.precision === "interval") return `${where} (uncertain: ${fmtBp(bp.ci_low)}–${fmtBp(bp.ci_high)})`;
  if (bp.precision === "imprecise_unquantified") return `${where} (imprecise; range not given)`;
  return where;
}

export function detailPanelHtml(item, assembly) {
  if (!item) {
    return `<p class="hint">Select a finding on the map to see its coordinates, evidence and provenance.</p>`;
  }
  const d = item.data;
  const head = (title, status) =>
    `<h3>${esc(title)}</h3><p class="status status-${esc(status)}">${esc(status)}</p>` +
    (status === "uncertain" ? `<p class="caveat">Not confirmed. The source did not mark this call PASS or noted a discrepancy.</p>` : "");
  let body = "";
  if (item.type === "small_variant") {
    body = `<h3>Small variant</h3><dl>${row("Chromosome", item.chrom)}${row("Position", fmtBp(d.pos))}${row("Type", d.variant_type)}${row("Assembly", assembly)}</dl>`;
  } else if (item.type === "cnv") {
    body =
      head(KIND_LABEL[d.kind_class] || "CNV", d.status) +
      `<dl>${row("Assembly", d.assembly)}${row("Region", `${d.chrom}:${fmtBp(d.start)}–${fmtBp(d.end)} (${fmtBp(d.length_bp)} bp)`)}` +
      `${row("Start", bpText(d.start_breakpoint))}${row("End", bpText(d.end_breakpoint))}` +
      `${row("Copy number", d.copy_number === null ? null : `${d.copy_number} (${d.copy_number_source})`)}${row("Log2 ratio", d.log2_ratio)}` +
      `${row("Quality", d.quality)}${row("Filter", d.filter)}${provenanceRows(d)}</dl>${warningsHtml(d.warnings)}${evidenceHtml(d.evidence)}`;
  } else if (item.type === "sv_interval") {
    body =
      head(KIND_LABEL[d.kind_class] || d.sv_type, d.status) +
      `<dl>${row("Assembly", assembly)}${row("Start", bpText(d.start))}${row("End", bpText(d.end))}${row("SVLEN", d.sv_length)}${row("Caller", d.caller)}${row("Source ID", d.source_id)}</dl>${warningsHtml(d.warnings)}`;
  } else if (item.type === "sv_breakend") {
    body =
      head(KIND_LABEL[d.kind_class] || d.sv_type, d.status) +
      `<dl>${row("Assembly", assembly)}${row("This end", bpText(d.breakpoint))}${row("Which end", d.end === "partner" ? "partner end of a pair" : "this record's own position")}` +
      `${row("Partner", partnerText(d.partner_status))}${row("Mate ID", d.mate_id)}${row("Event", d.event_id)}${row("Inserted length", d.inserted_length)}${row("Caller", d.caller)}${row("Source ID", d.source_id)}</dl>${warningsHtml(d.warnings)}`;
  } else if (item.type === "link") {
    body =
      head("Breakend pair", d.status) +
      `<dl>${row("End A", bpText(d.a))}${row("End B", bpText(d.b))}${row("Between chromosomes", d.is_interchromosomal === null ? null : d.is_interchromosomal ? "yes" : "no")}` +
      `${row("Mate records agree", d.mate_paired ? "yes (reciprocal MATEID, matching coordinates)" : "not confirmed")}${row("Source label", d.sv_type)}</dl>` +
      `<p class="caveat">A pair of breakends is not by itself a translocation.</p>${warningsHtml(d.warnings)}`;
  }
  return body;
}

function partnerText(s) {
  return {
    partner_given: "stated by the source",
    partner_not_specified: "not stated by the source",
    single_breakend: "single breakend (no partner)",
    partner_on_unplaced_contig: "on a contig that is not placed here",
  }[s] || null;
}

function provenanceRows(d) {
  return row("Caller", d.caller) + row("Call set", d.call_set_id) + row("Source ID", d.source_id) + row("VCF line", d.line_no);
}

function warningsHtml(w) {
  return w && w.length ? `<ul class="warnings">${w.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
}

function evidenceHtml(ev) {
  if (!ev) return "";
  const info = Object.entries(ev.info || {}).map(([k, v]) => `${k}=${v}`).join("\n");
  return `<details><summary>Original caller evidence</summary><pre>${esc(ev.raw_record || info)}</pre></details>`;
}

/** Partner relationships that touch a chromosome, as selectable rows (the arcs are listed, not hidden). */
export function linksHtml(map, chromName) {
  const mine = (map.links || []).filter((l) => l.a.chrom === chromName || l.b.chrom === chromName);
  if (!mine.length) return "";
  const items = mine
    .map((l) => {
      const label = `${l.a.chrom}:${fmtBp(l.a.pos)} ↔ ${l.b.chrom}:${fmtBp(l.b.pos)} (${l.status}${l.mate_paired ? ", mates agree" : ""})`;
      return `<li><button type="button" data-finding="${esc(`${l.id}|link`)}">${esc(label)}</button></li>`;
    })
    .join("");
  return `<h3>Breakend pairs on chromosome ${esc(chromName)}</h3><ul class="links">${items}</ul>`;
}

export function mapHeaderHtml(entry) {
  const m = entry.map;
  const cs = m.call_sets || [];
  const sets = cs.length
    ? cs.map((c) => `<li>${esc(c.record_class.toUpperCase())} call set ${esc(c.call_set_id ?? "")} — ${esc(c.caller ?? c.caller_in_header ?? "caller not stated")}, ${esc(c.assembly)}; ${c.counts.detected} detected, ${c.counts.uncertain} uncertain, ${c.counts.unsupported} unsupported, ${c.counts.invalid} invalid${c.header_check.status === "unverified" ? "; assembly not verifiable from the file header" : ""}</li>`).join("")
    : `<li>No CNV or structural-variant call set is attached: those layers are <strong>not assessed</strong>.</li>`;
  const excluded = (m.excluded_call_sets || []).map((c) => `<li>${esc(c.call_set_id ?? "call set")} not drawn: ${esc(c.reason)}</li>`).join("");
  return (
    `<p class="assembly">Assembly <strong>${esc(m.assembly)}</strong> · coordinates 1-based, inclusive</p>` +
    `<ul class="callsets">${sets}${excluded}</ul>` +
    `<p class="karyo">Karyotype: ${esc(layerLabel(m.karyotype.status))}. ${esc(m.karyotype.reason)}</p>`
  );
}

export function legendHtml(map) {
  return `<ul class="legend">${(map.legend || []).map((i) => `<li><span class="glyph glyph-${esc(i.glyph)}" aria-hidden="true"></span>${esc(i.label)}</li>`).join("")}</ul>`;
}

export function limitationsHtml(map) {
  return `<ul class="limitations">${(map.limitations || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
}

// ── page (the only DOM code) ────────────────────────────────────────────────

export function mapUrl(patientId, chromosome = null, region = null) {
  const base = `/clinic/patients/${encodeURIComponent(patientId)}/genome/map`;
  if (!chromosome) return base;
  const q = region ? `?start=${encodeURIComponent(region[0])}&end=${encodeURIComponent(region[1])}` : "";
  return `${base}/${encodeURIComponent(chromosome)}${q}`;
}

export async function load(patientId, fetchImpl = fetch) {
  const response = await fetchImpl(mapUrl(patientId), { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (!response.ok) return { state: "error", status: response.status };
  const body = await response.json();
  if (!body.maps || body.maps.length === 0) return { state: "empty", body };
  return { state: "ready", body };
}

export function mount(root, patientId, fetchImpl = fetch) {
  const ui = { selected: null, chrom: null, mapIndex: 0, data: null };
  const q = (sel) => root.querySelector(sel);
  const paint = () => {
    const entry = ui.data.maps[ui.mapIndex];
    if (!entry.map) {
      q("#explorer").innerHTML = stateHtml("unusable", { reason: entry.reason });
      return;
    }
    const map = entry.map;
    const idx = findingIndex(map);
    q("#header").innerHTML = mapHeaderHtml(entry);
    q("#overview").innerHTML = overviewSvg(map, { selected: ui.chrom });
    const chrom = map.chromosomes.find((c) => c.name === ui.chrom);
    q("#detail").innerHTML = chrom ? detailSvg(chrom, { selectedId: ui.selected }) : `<p class="hint">Select a chromosome.</p>`;
    q("#links").innerHTML = ui.chrom ? linksHtml(map, ui.chrom) : "";
    q("#panel").innerHTML = detailPanelHtml(idx.get(ui.selected), map.assembly);
    q("#legend").innerHTML = legendHtml(map);
    q("#limits").innerHTML = limitationsHtml(map);
  };
  root.addEventListener("click", (e) => {
    const f = e.target.closest("[data-finding]");
    const c = e.target.closest("[data-chrom]");
    if (f) ui.selected = f.getAttribute("data-finding");
    else if (c) {
      ui.chrom = c.getAttribute("data-chrom");
      ui.selected = null;
    } else return;
    paint();
  });
  root.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-chrom],[data-finding]")) {
      e.preventDefault();
      e.target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    }
  });
  q("#explorer").innerHTML = stateHtml("loading");
  return load(patientId, fetchImpl).then((r) => {
    if (r.state !== "ready") {
      q("#explorer").innerHTML = stateHtml(r.state, r);
      return r;
    }
    ui.data = r.body;
    q("#explorer").innerHTML =
      `<section id="header"></section><div id="overview" class="overview-wrap"></div><div class="split"><div><div id="detail" class="detail-wrap"></div><div id="links"></div></div><aside id="panel" aria-live="polite"></aside></div>` +
      `<section><h2>Legend</h2><div id="legend"></div></section><section><h2>Limitations</h2><div id="limits"></div></section>`;
    ui.chrom = r.body.maps[0].map?.chromosomes?.[0]?.name ?? null;
    paint();
    return r;
  });
}

if (typeof document !== "undefined" && document.getElementById("app")) {
  const id = new URLSearchParams(location.search).get("patient");
  const root = document.getElementById("app");
  if (!id) root.querySelector("#explorer").innerHTML = stateHtml("error", { status: 404 });
  else mount(root, id);
}

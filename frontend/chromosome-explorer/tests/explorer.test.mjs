// Chromosome Explorer tests. Plain `node --test`; no DOM, no dependencies.
// The fixture is SYNTHETIC and is generated from the real backend builder (a
// Python test keeps the two in sync): it is not patient data.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  esc, fmtBp, stateHtml, overviewLayout, overviewSvg, detailLayout, detailSvg, packLanes,
  findingIndex, detailPanelHtml, linksHtml, mapHeaderHtml, legendHtml, limitationsHtml, mapUrl, load, layerLabel,
} from "../explorer.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "map.synthetic.json"), "utf8"));
const MAP = fixture.maps[0].map;
const chrom = (name) => MAP.chromosomes.find((c) => c.name === name);
const clone = (o) => JSON.parse(JSON.stringify(o));

test("esc neutralises markup and quotes", () => {
  assert.equal(esc(`<img src=x onerror="a('b')">&`), "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;");
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
});

test("fmtBp groups digits and tolerates non-numbers", () => {
  assert.equal(fmtBp(248956422), "248,956,422");
  assert.equal(fmtBp(null), "—");
  assert.equal(fmtBp(undefined), "—");
});

// ── states ──
test("loading, empty, error and unusable states are distinct and accessible", () => {
  assert.match(stateHtml("loading"), /role="status"[^>]*aria-live="polite"/);
  assert.match(stateHtml("loading"), /Loading genomic map/);
  assert.match(stateHtml("empty"), /No genomic analysis/);
  assert.match(stateHtml("empty"), /not a finding about their genome/);
  assert.match(stateHtml("error", { status: 403 }), /role="alert"[\s\S]*does not permit/);
  assert.match(stateHtml("error", { status: 401 }), /not signed in/);
  assert.match(stateHtml("error", { status: 404 }), /No such patient/);
  assert.match(stateHtml("error", { status: 500 }), /could not be loaded/);
  assert.match(stateHtml("unusable", { reason: "build <b>unknown</b>" }), /build &lt;b&gt;unknown&lt;\/b&gt;/);
  assert.equal(stateHtml("nonsense"), "");
});

test("the error state never echoes server text", () => {
  assert.doesNotMatch(stateHtml("error", { status: 500, detail: "<script>x</script>" }), /script/);
});

// ── overview ──
test("overview: 25 chromosomes on ONE scale, longest fills the width", () => {
  const L = overviewLayout(MAP, { width: 960 });
  assert.equal(L.rows.length, 25);
  const widths = Object.fromEntries(L.rows.map((r) => [r.name, r.w]));
  assert.ok(widths["1"] > widths["21"] && widths["1"] > widths["X"]);
  assert.ok(Math.abs(widths["1"] - (960 - L.gutter - L.pad)) < 1e-6);
  assert.ok(Math.abs(widths["2"] / widths["1"] - chrom("2").length_bp / chrom("1").length_bp) < 1e-9);
  assert.ok(L.rows.every((r, i) => i === 0 || r.y > L.rows[i - 1].y));
});

test("overview svg: selectable, labelled with statuses, scales with the viewport", () => {
  const svg = overviewSvg(MAP, { width: 960, selected: "1" });
  assert.match(svg, /viewBox="0 0 960 \d+"/);
  assert.equal((svg.match(/<g class="chrom/g) || []).length, 25);
  assert.match(svg, /data-chrom="1"[^>]*aria-pressed="true"/);
  assert.match(svg, /data-chrom="2"[^>]*aria-pressed="false"/);
  assert.match(svg, /tabindex="0" role="button"/);
  assert.match(svg, /Chromosome 3, [\d,]+ bp\. Small variants: No variants reported \(not a normal result\)\. CNV: No calls reported \(not a normal result\)\. SV: No calls reported/);
  assert.match(svg, /class="mk-small"/);
  assert.match(svg, /mk-cnv deletion/);
  assert.match(svg, /mk-cnv duplication/);
  assert.match(svg, /mk-bnd/);
});

test("overview: with no call sets every chromosome is drawn as unassessed, never as normal", () => {
  const m = clone(MAP);
  for (const c of m.chromosomes) {
    c.layers.cnv = { status: "not_assessed", segments: [], overlaps: [] };
    c.layers.sv = { status: "not_assessed", intervals: [], breakends: [] };
  }
  const svg = overviewSvg(m);
  assert.equal((svg.match(/chrom-bar unassessed/g) || []).length, 25);
  assert.match(svg, /CNV: Not assessed/);
  assert.doesNotMatch(svg, /normal\b(?! result)/);
});

// ── detail ──
test("detail: CNV segments sit on separate lanes when they overlap", () => {
  const c = chrom("1");
  const lanes = c.layers.cnv.segments.map((s) => s.lane);
  assert.ok(new Set(lanes).size >= 2, "overlapping CNVs must stack");
  const svg = detailSvg(c, { width: 960 });
  assert.equal((svg.match(/class="cnv-seg /g) || []).length, 3);
  assert.match(svg, /cnv-seg deletion/);
  assert.match(svg, /cnv-seg duplication uncertain/);
  assert.match(svg, /cnv-seg copy_number_change/);
  assert.match(svg, /▼/);
  assert.match(svg, /▲/);
});

test("detail: uncertain breakpoints are RANGES; unquantified ones are dashed markers, not points", () => {
  const svg = detailSvg(chrom("1"), { width: 960 });
  const ranges = svg.match(/<rect class="ci [^"]*cnv-ci" x="([\d.]+)" y="[^"]+" width="([\d.]+)"/g) || [];
  assert.equal(ranges.length, 1, "only the CNV with CIPOS has an uncertain end; its precise end draws no range");
  for (const r of ranges) assert.ok(Number(r.match(/width="([\d.]+)"/)[1]) >= 3, "a range is never narrower than 3px");
  assert.match(svg, /class="ci unquantified sv-ci"/);
  assert.match(svg, /class="ci sv-ci"/);
});

test("detail: SV intervals and breakends are selectable and distinguish own/partner ends", () => {
  const svg = detailSvg(chrom("1"), { width: 960 });
  assert.match(svg, /class="sv-int deletion/);
  assert.match(svg, /class="sv-int inversion/);
  assert.match(svg, /sv-bnd ins own/);
  assert.match(svg, /sv-bnd bnd own/);
  assert.match(svg, /sv-bnd bnd partner/);
  assert.match(svg, /data-finding="sv:synthetic-sv:\d+\|partner"/);
  assert.match(svg, /partner end, detected/);
});

test("detail: a layer with no call set is drawn as an unassessed band; one with a call set and no calls says so", () => {
  const c2 = chrom("2");
  const svg2 = detailSvg(c2, { width: 960 });
  assert.match(svg2, /Copy number/);
  assert.match(svg2, /No calls reported \(not a normal result\)/);
  const none = clone(c2);
  none.layers.cnv = { status: "not_assessed", segments: [], overlaps: [] };
  none.layers.small_variants = { status: "not_assessed", markers: [] };
  const svg = detailSvg(none, { width: 960 });
  assert.match(svg, /band unassessed/);
  assert.match(svg, />Not assessed</);
});

test("detail: the selected finding is marked", () => {
  const seg = chrom("1").layers.cnv.segments[0];
  assert.match(detailSvg(chrom("1"), { selectedId: seg.id }), new RegExp(`selected" data-finding="${seg.id.replace(/[:|]/g, ".")}"`));
});

test("detail layout grows with lanes (no clipping) and keeps a fixed gutter", () => {
  const a = detailLayout(chrom("1"));
  const b = detailLayout(chrom("2"));
  assert.ok(a.height > b.height);
  assert.equal(a.gutter, b.gutter);
});

test("packLanes puts overlapping intervals on different lanes and reuses free lanes", () => {
  const mk = (s, e) => ({ start: { pos: s }, end: { pos: e } });
  const p = packLanes([mk(1, 10), mk(5, 15), mk(11, 20), mk(30, 40)]);
  assert.deepEqual(p.lane, [0, 1, 0, 0]);
  assert.equal(p.count, 2);
  assert.equal(packLanes([]).count, 0);
});

// ── finding panel ──
test("finding index covers every drawn finding and every link", () => {
  const idx = findingIndex(MAP);
  const c1 = chrom("1").layers;
  for (const s of c1.cnv.segments) assert.ok(idx.has(s.id));
  for (const s of c1.sv.intervals) assert.ok(idx.has(s.id));
  for (const b of c1.sv.breakends) assert.ok(idx.has(`${b.id}|${b.end}`));
  for (const m of c1.small_variants.markers) assert.ok(idx.has(m.id));
  for (const l of MAP.links) assert.ok(idx.has(`${l.id}|link`));
});

test("panel: nothing selected gives guidance, not data", () => {
  assert.match(detailPanelHtml(undefined, "GRCh38"), /Select a finding/);
});

test("panel: CNV shows assembly, coordinates, evidence, provenance; missing CN is 'not reported', never a number", () => {
  const idx = findingIndex(MAP);
  const noCn = [...idx.values()].find((v) => v.type === "cnv" && v.data.copy_number === null);
  const html = detailPanelHtml(noCn, "GRCh38");
  assert.match(html, /Assembly<\/dt><dd>GRCh38/);
  assert.match(html, /Region<\/dt><dd>1:[\d,]+–[\d,]+/);
  assert.match(html, /Copy number<\/dt><dd><span class="na">not reported/);
  assert.match(html, /Caller<\/dt><dd>SYNTHETIC-TEST-CALLER/);
  assert.match(html, /Call set<\/dt><dd>synthetic-cnv/);
  assert.match(html, /Original caller evidence/);
});

test("panel: an uncertain finding is labelled uncertain with a not-confirmed caveat", () => {
  const idx = findingIndex(MAP);
  const unc = [...idx.values()].find((v) => v.type === "cnv" && v.data.status === "uncertain");
  const html = detailPanelHtml(unc, "GRCh38");
  assert.match(html, /status-uncertain/);
  assert.match(html, /Not confirmed/);
});

test("panel: a breakend pair is not called a translocation", () => {
  const html = detailPanelHtml({ type: "link", data: MAP.links[0], chrom: "1" }, "GRCh38");
  assert.match(html, /not by itself a translocation/);
  assert.match(html, /reciprocal MATEID/);
  assert.doesNotMatch(html, /<h3>Translocation/);
});

test("panel: breakend with no stated partner says so", () => {
  const idx = findingIndex(MAP);
  const lone = [...idx.values()].find((v) => v.type === "sv_breakend" && v.data.partner_status === "partner_not_specified");
  assert.match(detailPanelHtml(lone, "GRCh38"), /not stated by the source/);
});

test("links list is per-chromosome and selectable", () => {
  assert.match(linksHtml(MAP, "1"), /data-finding="sv:synthetic-sv:\d+\|link"/);
  assert.match(linksHtml(MAP, "2"), /Breakend pairs on chromosome 2/);
  assert.equal(linksHtml(MAP, "3"), "");
});

// ── header, legend, limits ──
test("header lists call sets with counts and never claims a karyotype", () => {
  const h = mapHeaderHtml(fixture.maps[0]);
  assert.match(h, /CNV call set synthetic-cnv/);
  assert.match(h, /SV call set synthetic-sv/);
  assert.match(h, /Karyotype: Not assessed/);
  assert.match(h, /1-based, inclusive/);
  const bare = clone(fixture.maps[0]);
  bare.map.call_sets = [];
  assert.match(mapHeaderHtml(bare), /not assessed<\/strong>|<strong>not assessed/);
});

test("legend distinguishes every class and limitations are shown", () => {
  const lg = legendHtml(MAP);
  for (const t of ["Small variant", "Deletion", "Duplication", "Insertion", "Inversion", "Breakend"]) assert.match(lg, new RegExp(t));
  assert.match(limitationsHtml(MAP), /NOT shown as normal/);
  assert.equal(layerLabel("nope"), "Unknown status");
});

// ── injection ──
test("hostile strings from the API never become markup", () => {
  const m = clone(MAP);
  const seg = m.chromosomes.find((c) => c.name === "1").layers.cnv.segments[0];
  seg.caller = `<img src=x onerror=alert(1)>`;
  seg.source_id = `"><script>alert(2)</script>`;
  seg.id = `x" onmouseover="alert(3)`;
  seg.warnings = ["<b>bold</b>"];
  seg.evidence = { raw_record: "<svg onload=alert(4)>" };
  m.call_sets[0].call_set_id = "<i>cs</i>";
  const blob = detailPanelHtml({ type: "cnv", data: seg, chrom: "1" }, "<x>") + detailSvg(m.chromosomes.find((c) => c.name === "1")) + mapHeaderHtml({ map: m });
  assert.doesNotMatch(blob, /<img|<script|<b>|<svg onload|<i>|<x>/);
  assert.doesNotMatch(blob, /onmouseover="alert/);
});

// ── loading ──
test("mapUrl encodes identifiers and regions", () => {
  assert.equal(mapUrl("a b"), "/clinic/patients/a%20b/genome/map");
  assert.equal(mapUrl("p", "17", [100, 200]), "/clinic/patients/p/genome/map/17?start=100&end=200");
  assert.equal(mapUrl("p", "17"), "/clinic/patients/p/genome/map/17");
});

test("load: ready, empty and error outcomes; same-origin credentials; only the clinician route", async () => {
  const calls = [];
  const ok = async (url, opts) => (calls.push([url, opts]), { ok: true, status: 200, json: async () => fixture });
  const r = await load("pid", ok);
  assert.equal(r.state, "ready");
  assert.equal(calls[0][0], "/clinic/patients/pid/genome/map");
  assert.equal(calls[0][1].credentials, "same-origin");
  assert.ok(!calls[0][0].includes("/portal/"));
  assert.equal((await load("p", async () => ({ ok: true, status: 200, json: async () => ({ maps: [] }) }))).state, "empty");
  for (const status of [401, 403, 404, 500]) {
    const e = await load("p", async () => ({ ok: false, status }));
    assert.deepEqual([e.state, e.status], ["error", status]);
  }
});

test("responsive: the stylesheet collapses the two-column layout and honours reduced motion", () => {
  const css = readFileSync(join(here, "..", "explorer.css"), "utf8");
  assert.match(css, /@media \(max-width: 860px\)\s*\{\s*\.split\s*\{\s*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /overflow-x: auto/);
});

test("the page is self-contained: CSP forbids external resources and there is no inline script", () => {
  const html = readFileSync(join(here, "..", "index.html"), "utf8");
  assert.match(html, /Content-Security-Policy[^>]*default-src 'none'/);
  assert.match(html, /script-src 'self'/);
  assert.doesNotMatch(html, /<script(?![^>]*src=)/);
  assert.doesNotMatch(html, /https?:\/\//);
});

test("the explorer script contains no patient-route call, no demo data and no 'normal' claim", () => {
  const src = readFileSync(join(here, "..", "explorer.js"), "utf8");
  assert.doesNotMatch(src, /\/portal\//);
  assert.doesNotMatch(src, /46,X[XY]/);
  assert.doesNotMatch(src, /localStorage|sessionStorage|document\.cookie/);
});

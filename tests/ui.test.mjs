import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../ui.html", import.meta.url), "utf8");
const source = html.slice(html.indexOf("/* ---------- utilities"), html.indexOf("function renderAll(){"));
const localization = html.slice(html.indexOf("const L10N ="), html.indexOf("function template("));

function render(windows, id = "claude", meta = {}, language = "en") {
  const pages = Object.fromEntries(["#page-overview", `#page-${id}`].map(id => [id, {
    innerHTML: "", querySelectorAll: () => [],
  }]));
  const context = vm.createContext({
    DATA: { providers: { [id]: { name: id, kind: "windows", ok: true, windows, meta } } },
    LANG: language, clockSkew: 0, THEME: { [id]: `th-${id}` }, GLYPH: { [id]: "*" },
    PROVIDER_IDS: [id], terr: () => "",
    Date: class extends Date { static now() { return 1_000_000; } },
    document: { querySelector: id => pages[id] },
  });
  vm.runInContext(`${localization}\n${source}\nrenderOverview(); renderDetail(${JSON.stringify(id)});`, context);
  return Object.values(pages).map(page => page.innerHTML);
}

const legacy = [
  { id: "session", label: "session", remaining_pct: 82 },
  { id: "week_opus", label: "weekOpus", remaining_pct: 99 },
];

test("Codex fallback source and observation time appear in overview and detail", () => {
  for (const markup of render([{ id: "week", label: "week", remaining_pct: 79 }], "codex",
    { usage_source: "codex_session", observed_at: 990 })) {
    assert.match(markup, /From Codex CLI/);
    assert.match(markup, /79/);
    assert.doesNotMatch(markup, /undefined|\{time\}/);
    assert.match(markup, /From Codex CLI \(API unavailable\) · [^<]*\d{1,2}:\d{2}/);
  }
});

test("missing reset data is unavailable while a confirmed zero remains zero", () => {
  for(const markup of render([], "claude")) {
    assert.match(markup, /Reset data unavailable/);
    assert.doesNotMatch(markup, /Available resets/);
  }
  for(const markup of render([], "claude", {banked_resets:{status:"ok", count:0, grants:[]}})) {
    assert.match(markup, /Available resets.*<strong>0<\/strong>/s);
  }
});

test("authoritative Codex count survives incomplete grant details", () => {
  const meta = {banked_resets:{status:"ok", count:5, details_complete:false, grants:[
    {id:"private-id", coverage:"full", remaining_count:1, status:"available", expires_at:2_000_000_000},
  ]}};
  const [overview, detail] = render([], "codex", meta);
  assert.match(overview, /Available resets.*<strong>5<\/strong>/s);
  assert.match(detail, /Weekly \+ session reset · 1/);
  assert.match(detail, /Some reset details are unavailable/);
  assert.match(detail, /2033/);
  assert.doesNotMatch(overview + detail, /private-id/);
});

test("details group matching grants and keep Fable separate in both languages", () => {
  const meta = {banked_resets:{status:"ok", count:4, details_complete:true, grants:[
    {coverage:"weekly_fable", remaining_count:1, status:"available", expires_at:2_000_000_000},
    {coverage:"weekly_fable", remaining_count:2, status:"available", expires_at:2_000_000_000},
    {coverage:"fable", remaining_count:1, status:"available", expires_at:null},
    {coverage:"full", remaining_count:1, status:"consumed", expires_at:2_000_000_000},
  ]}};
  for(const lang of ["en", "ru"]){
    const [, detail] = render([], "claude", meta, lang);
    assert.equal((detail.match(/class="reset-grant"/g) || []).length, 2);
    assert.match(detail, /Fable.* · 3/);
    assert.match(detail, /Fable.* · 1/);
    assert.match(detail, lang === "en" ? /Expiration unavailable/ : /Срок действия неизвестен/);
    assert.doesNotMatch(detail, /NaN|undefined|reset_weekly|resetExpires/);
  }
});

test("overview and detail use only the overall week from older snapshots", () => {
  for (const remaining_pct of [0, 100, 10, null]) {
    for (const markup of render([...legacy, { id: "week", label: "week", remaining_pct, resets_at: 8200 }])) {
      assert.doesNotMatch(markup, /session|weekOpus|>82(?:<|%)|>99(?:<|%)/);
      assert.match(markup, /Week/);
      assert.equal((markup.match(/class="bar /g) || []).length, 1);
      assert.match(markup, new RegExp(`width:${remaining_pct ?? 0}%`));
      assert.match(markup, /resets in 2 h 0 m/);
      if (remaining_pct !== null && remaining_pct <= 15) assert.match(markup, /bar low/);
      if (remaining_pct === null) assert.match(markup, /—/);
    }
  }
});

test("missing overall week shows no data with no fallback or quota bar", () => {
  for (const markup of render(legacy)) {
    assert.match(markup, /no data/);
    assert.doesNotMatch(markup, /session|weekOpus|class="bar /);
  }
});

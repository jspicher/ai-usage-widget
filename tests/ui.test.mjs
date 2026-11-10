import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../ui.html", import.meta.url), "utf8");
const source = html.slice(html.indexOf("/* ---------- utilities"), html.indexOf("function renderAll(){"));

function render(windows, id = "claude", meta = {}) {
  const pages = Object.fromEntries(["#page-overview", `#page-${id}`].map(id => [id, {
    innerHTML: "", querySelectorAll: () => [],
  }]));
  const context = vm.createContext({
    DATA: { providers: { [id]: { name: id, kind: "windows", ok: true, windows, meta } } },
    LANG: "en", clockSkew: 0, THEME: { [id]: `th-${id}` }, GLYPH: { [id]: "*" },
    PROVIDER_IDS: [id], t: key => key === "codexSessionSource" ? "From Codex CLI · {time}" : key, terr: () => "",
    Date: class extends Date { static now() { return 1_000_000; } },
    document: { querySelector: id => pages[id] },
  });
  vm.runInContext(`${source}\nrenderOverview(); renderDetail(${JSON.stringify(id)});`, context);
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
    assert.match(markup, /From Codex CLI · [^<]*\d{1,2}:\d{2}/);
  }
});

test("overview and detail use only the overall week from older snapshots", () => {
  for (const remaining_pct of [0, 100, 10, null]) {
    for (const markup of render([...legacy, { id: "week", label: "week", remaining_pct, resets_at: 8200 }])) {
      assert.doesNotMatch(markup, /session|weekOpus|>82(?:<|%)|>99(?:<|%)/);
      assert.match(markup, /week/);
      assert.equal((markup.match(/class="bar /g) || []).length, 1);
      assert.match(markup, new RegExp(`width:${remaining_pct ?? 0}%`));
      assert.match(markup, /resetsIn 2 h 0 m/);
      if (remaining_pct !== null && remaining_pct <= 15) assert.match(markup, /bar low/);
      if (remaining_pct === null) assert.match(markup, /—/);
    }
  }
});

test("missing overall week shows no data with no fallback or quota bar", () => {
  for (const markup of render(legacy)) {
    assert.match(markup, /noData/);
    assert.doesNotMatch(markup, /session|weekOpus|class="bar /);
  }
});

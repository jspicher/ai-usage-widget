import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../alert.html", import.meta.url), "utf8");
const source = html.slice(html.indexOf("<script>") + 8, html.indexOf("</script>"));

async function alertsUI(alerts, language="en"){
  const elements = Object.fromEntries(["all", "rows", "title", "closeEmpty"].map(id => [id, {
    innerHTML:"", style:{}, handlers:{},
    addEventListener(event, action){ this.handlers[event] = action; },
    querySelectorAll(){
      this.buttons = [...this.innerHTML.matchAll(/data-id="([^"]+)"/g)].map(match => ({
        dataset:{id:match[1]}, handlers:{},
        addEventListener(event, action){this.handlers[event] = action;},
      }));
      return this.buttons;
    },
  }]));
  let pending = structuredClone(alerts);
  const context = vm.createContext({
    document:{documentElement:{}, getElementById:id => elements[id]},
    window:{addEventListener(){}, pywebview:{api:{
      get_alerts:async () => structuredClone(pending), get_language:async () => language,
      dismiss_alert:async id => {pending = pending.filter(a => a.id !== id);},
      dismiss_all:async () => {pending = [];},
    }}},
    setTimeout(){},
  });
  vm.runInContext(source, context);
  await context.window.renderAlerts();
  return {elements, context, update:async alerts => {
    pending = structuredClone(alerts);
    await context.window.renderAlerts();
  }};
}

const reminder = {id:"expiry-1", kind:"reset_expiry", provider_name:"Claude Code",
  coverage:"fable", remaining_count:2, status:"available", threshold_hours:10,
  expires_at:2_000_036_000, detected_at:2_000_000_000};

test("both reminder stages update a single persistent row with local date and count", async () => {
  const ui = await alertsUI([reminder]);
  assert.match(ui.elements.rows.innerHTML, /Fable reset · 2 remaining · within 10h/);
  assert.match(ui.elements.rows.innerHTML, /2033/);
  await ui.update([{...reminder, threshold_hours:1, remaining_count:1}]);
  assert.match(ui.elements.rows.innerHTML, /1 remaining · within 1h/);
  assert.equal((ui.elements.rows.innerHTML.match(/class="row"/g) || []).length, 1);
  assert.doesNotMatch(ui.elements.rows.innerHTML, /Weekly NaN|undefined|within 10h/);
});

test("expired, unavailable, and unconfirmed rows remain dismissible", async () => {
  for(const [status, text] of [["expired", /expired/], ["unavailable", /no longer available/],
                              ["unverified", /availability unconfirmed/]]){
    const ui = await alertsUI([{...reminder, status, remaining_count:0}]);
    assert.match(ui.elements.rows.innerHTML, text);
    assert.equal(ui.elements.rows.buttons.length, 1);
  }
});

test("weekly notices coexist with expiry reminders and support individual dismissal", async () => {
  const ui = await alertsUI([reminder, {id:"weekly", provider_name:"Codex CLI", from_pct:5,
    to_pct:100, detected_at:2_000_000_000, while_away:true}]);
  assert.match(ui.elements.rows.innerHTML, /Weekly 5% → 100% remaining/);
  assert.match(ui.elements.rows.innerHTML, /while you were away/);
  assert.equal(ui.elements.all.style.display, "inline");
  await ui.elements.rows.buttons[0].handlers.click();
  await ui.context.window.renderAlerts();
  assert.doesNotMatch(ui.elements.rows.innerHTML, /Fable/);
  assert.match(ui.elements.rows.innerHTML, /Codex CLI/);
});

test("Russian rendering includes expiration and matching translation keys", async () => {
  const ui = await alertsUI([reminder, {...reminder, id:"two"}], "ru");
  assert.match(ui.elements.rows.innerHTML, /Сброс Fable · осталось 2 · в течение 10 ч/);
  assert.match(ui.elements.rows.innerHTML, /Действует до/);
  assert.equal(ui.context.document.documentElement.lang, "ru");
  assert.equal(vm.runInContext("JSON.stringify(Object.keys(L10N.en).sort())", ui.context),
               vm.runInContext("JSON.stringify(Object.keys(L10N.ru).sort())", ui.context));
  // Dismiss-all is wired to the existing API, independently of individual rows.
  await ui.elements.all.handlers.click();
  await ui.context.window.renderAlerts();
  assert.match(ui.elements.rows.innerHTML, /Нет оповещений/);
  assert.equal(typeof ui.elements.closeEmpty.handlers.click, "function");
});

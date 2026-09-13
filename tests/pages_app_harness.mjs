// Formal MIS-117 assertions driving the real pages/settings/app.js in a
// synthetic DOM (no browser, no server, no accounts):
//   1. bridge contract: the host's normalizePluginEndpoint rejects '?' in
//      endpoints, so list pages must send a pure endpoint plus a separate
//      params object (the globals module enforces the same validation);
//   2. request de-duplication keys on endpoint AND params;
//   3. page links actually navigate (preventDefault + requested page);
//   4. a config revision conflict rebuilds the whole form and closure, so a
//      retry posts the fresh revision and succeeds.
// Exits non-zero when any assertion fails; the python runner asserts that.
import './pages_app_globals.mjs';
import '../pages/settings/app.js';

const {panel, elements, tabButtons, gets, posts, configGetCount} = globalThis.__relationArcTest;

const failures = [];
const check = (name, ok, detail) => {
  if (!ok) failures.push({name, detail});
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + name + (ok ? '' : ' ' + JSON.stringify(detail ?? '')));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function until(cond, label) {
  for (let i = 0; i < 2000; i += 1) {
    if (cond()) return;
    await sleep(5);
  }
  throw new Error('timeout waiting for: ' + label);
}
const tabByName = name => tabButtons.find(b => b.dataset.tab === name);

// The bundle's own init chain renders the config tab on load.
await until(() => elements['save'] && elements['save'].onclick, 'initial config render');

// 1+4. config conflict recovery rebuilds the form and the save closure.
const configBefore = configGetCount();
await elements['save'].onclick();
check('first save posts revision 0', posts[0] && posts[0].expected_revision === 0, posts);
check('conflict reloads the latest config', configGetCount() === configBefore + 1, {configGetCount: configGetCount()});
check('rebuilt inputs carry fresh values', elements['cf-allowed'].value === 'new-session' && elements['direct'].checked === false && elements['safety-mode'].value === 'llm_suggest' && elements['cf-safety-min'].value === '45', {
  allowed: elements['cf-allowed'].value, direct: elements['direct'].checked,
  safety: elements['safety-mode'].value, autoDuration: elements['cf-safety-min'].value,
});
check('conflict message shown', elements['result'].textContent === '配置已被其他窗口更新：已加载最新，请核对后重新保存', elements['result'].textContent);
await elements['save'].onclick();
// Success re-renders the form (clearing the result span), so "succeeded"
// is proven by the fresh revision being accepted without a second conflict.
check('retry posts fresh revision 1 and is accepted', posts.length === 2 && posts[1].expected_revision === 1 && !String(elements['result'].textContent).includes('已被其他窗口'), {posts, result: elements['result'].textContent});

// 2. list pages send pure endpoints with a separate params object.
const accountsCallsBefore = gets.filter(g => g.endpoint === 'accounts').length;
await tabByName('accounts').onclick();
await until(() => gets.filter(g => g.endpoint === 'accounts').length === accountsCallsBefore + 1, 'accounts render');
const lastAccounts = gets.filter(g => g.endpoint === 'accounts').at(-1);
check('accounts uses a pure endpoint + params', lastAccounts.endpoint === 'accounts' && lastAccounts.params && lastAccounts.params.page === 1 && lastAccounts.params.page_size === 50, lastAccounts);
check('page links rendered', panel.anchors.map(a => a.dataset.page).includes('2') && panel.anchors.map(a => a.dataset.page).includes('3'), panel.anchors.map(a => a.dataset.page));

// 3. page links navigate with preventDefault.
const link2 = panel.anchors.find(a => a.dataset.page === '2');
await link2.onclick({preventDefault: () => link2.preventDefault()});
check('preventDefault is called', link2.preventDefaulted === true, {});
const afterClick = gets.filter(g => g.endpoint === 'accounts').at(-1);
check('clicking page 2 requests page=2', afterClick.params.page === 2, afterClick);
await until(() => panel.html.includes('第 2 / 3 页'), 'page 2 hint');

// Dedup keys on endpoint AND params: two concurrent identical renders share
// one bridge call; a different page does not join the same entry.
const beforeDedup = gets.filter(g => g.endpoint === 'accounts').length;
await Promise.all([tabByName('accounts').onclick(), tabByName('accounts').onclick()]);
check('identical concurrent renders share one bridge call', gets.filter(g => g.endpoint === 'accounts').length === beforeDedup + 1, {count: gets.filter(g => g.endpoint === 'accounts').length, before: beforeDedup});
await tabByName('accounts').onclick();
await until(() => gets.filter(g => g.endpoint === 'accounts').length === beforeDedup + 2, 'sequential accounts render');

// audit/bindings also ride pure endpoints with their params.
const auditCallsBefore = gets.filter(g => g.endpoint === 'audit').length;
await tabByName('audit').onclick();
await until(() => gets.filter(g => g.endpoint === 'audit').length === auditCallsBefore + 1, 'audit render');
const lastAudit = gets.filter(g => g.endpoint === 'audit').at(-1);
check('audit uses a pure endpoint + params', lastAudit.endpoint === 'audit' && lastAudit.params.page_size === 100, lastAudit);
const bindingsCallsBefore = gets.filter(g => g.endpoint === 'bindings').length;
await tabByName('bindings').onclick();
await until(() => gets.filter(g => g.endpoint === 'bindings').length === bindingsCallsBefore + 1, 'bindings render');
const lastBindings = gets.filter(g => g.endpoint === 'bindings').at(-1);
check('bindings uses a pure endpoint + params', lastBindings.endpoint === 'bindings' && lastBindings.params.page_size === 50, lastBindings);

if (failures.length) {
  console.error('FAILURES ' + JSON.stringify(failures));
  process.exitCode = 1;
}

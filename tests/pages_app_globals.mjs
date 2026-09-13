// Shared synthetic DOM + Pages bridge for pages_app_harness.mjs.
// This module MUST be imported before app.js so the browser globals exist
// when the bundle evaluates. No eval, no vm: app.js is loaded as a module.
const elements = Object.create(null);
const tabButtons = ['overview', 'config', 'accounts', 'audit', 'health', 'migrations', 'bindings', 'backups']
  .map(tab => ({ dataset: { tab }, onclick: null }));
const listeners = {};

// Mirror of the host dashboard's normalizePluginEndpoint validation rules
// (PluginPagePage.vue): '?'/'#'/'://'/'\' and empty segments are rejected.
export function validateEndpoint(endpoint) {
  if (typeof endpoint !== 'string') throw new Error('Plugin bridge endpoint must be a string.');
  const n = endpoint.trim().replace(/^\/+/, '');
  if (!n) throw new Error('Plugin bridge endpoint cannot be empty.');
  if (n.includes('\\') || n.includes('://') || n.includes('?') || n.includes('#')) {
    throw new Error('Plugin bridge endpoint is invalid.');
  }
  const parts = n.split('/');
  if (parts.some(a => !a || a === '.' || a === '..')) throw new Error('Plugin bridge endpoint is invalid.');
  return parts.map(a => encodeURIComponent(a)).join('/');
}

const panel = {
  html: '',
  anchors: [],
  set innerHTML(html) {
    this.html = html;
    for (const m of html.matchAll(/<(input|textarea|select|button|span)\b([^>]*\bid="([^"]+)"[^>]*)>([^<]*)/g)) {
      const [, tag, attrs, id, content] = m;
      elements[id] = {
        id, tag,
        value: (attrs.match(/\bvalue="([^"]*)"/) || [])[1] || (tag === 'textarea' ? content : ''),
        checked: /\bchecked\b/.test(attrs), disabled: false,
        textContent: content, className: '', onclick: null,
      };
    }
    this.anchors = [...html.matchAll(/<a [^>]*data-page="(\d+)"[^>]*>/g)].map(m => ({
      dataset: { page: m[1] }, preventDefaulted: false,
      preventDefault() { this.preventDefaulted = true; },
    }));
  },
  get innerHTML() { return this.html; },
  querySelectorAll(sel) { return sel === 'a[data-page]' ? this.anchors : []; },
  querySelector(sel) { return sel === '#panel' ? panel : elements[sel.slice(1)]; },
  set textContent(value) { this.html = `<pre>${value}</pre>`; },
};

const original = {config_revision:0,enabled:true,private_enabled:true,group_enabled:true,group_require_at_or_reply:true,llm_judgment_enabled:true,raw_delta_limit:5,allowed_sessions:['old-session'],blocked_sessions:[],interaction_safety:{llm_mode:'administrator_only',auto_duration_minutes:30},romance:{global_enabled:true,eligibility_thresholds:{trust:550,comfort:550,closeness:450,resonance:400}},backup:{enabled:false,interval_hours:6,retention_hours:168},decay:{enabled:false,interval_minutes:60,inactive_hours:168,step:5},protocol_health:{enabled:true,retention_days:30},query_permission:{group_normal_user:true,private_normal_user:true}};
const fresh = {...original,config_revision:1,allowed_sessions:['new-session'],group_require_at_or_reply:false,interaction_safety:{llm_mode:'llm_suggest',auto_duration_minutes:45}};

const gets = [];
const posts = [];
let configGets = 0;
const bridge = {
  async apiGet(endpoint, params) {
    validateEndpoint(endpoint); // same rejection the real host applies
    gets.push({endpoint, params: params ? JSON.parse(JSON.stringify(params)) : params});
    // The bundle's init chain fetches config once before the first render,
    // so the first two gets return the stale original and later gets return
    // the concurrent-window fresh copy (revision 1).
    if (endpoint === 'config') return structuredClone((configGets++) < 2 ? original : fresh);
    if (endpoint === 'accounts') return {accounts: [], page: params.page || 1, pages: 3, total: 101};
    if (endpoint === 'audit') return {cards: [], page: params.page || 1, pages: 2, total: 150};
    if (endpoint === 'bindings') return {bindings: [], directory: [], page: params.page || 1, pages: 1, total: 0};
    throw new Error('unexpected endpoint: ' + endpoint);
  },
  async apiPost(endpoint, body) {
    posts.push(JSON.parse(JSON.stringify(body)));
    if (endpoint === 'config' && body.expected_revision !== 1) {
      throw new Error('配置已被其他窗口更新，请刷新后重试');
    }
    return {success: true};
  },
  async ready() { return true; },
};

globalThis.document = {
  querySelector: s => (s === '#panel' ? panel : elements[s.slice(1)]),
  querySelectorAll: sel => (sel === '[data-tab]' ? tabButtons : []),
  getElementById: id => elements[id] ?? null,
};
globalThis.window = {
  AstrBotPluginPage: bridge,
  addEventListener: (type, fn) => { listeners[type] = fn; },
};

globalThis.__relationArcTest = {
  panel, elements, tabButtons, gets, posts, listeners, validateEndpoint,
  configGetCount: () => configGets,
};

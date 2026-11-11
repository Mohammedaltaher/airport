import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import ts from 'typescript';
import MarkdownIt from 'markdown-it';

const html = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8');
const source = readFileSync(new URL('../src/scripts/dashboard.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
const pause = () => new Promise(resolve => setTimeout(resolve, 10));

// Synthetic unit fixture, never used by the application or shown as airport data.
function fixture(empty = false) {
  return {
    filters: { terminal: 'T1', date_from: '2026-09-01', date_to: '2026-09-30', weekday: 'All', hour_from: 17, hour_to: 22 },
    metrics: { average_wait: empty ? null : 12, pct_over_30: empty ? null : 5, sampled_passengers: empty ? 0 : 20, measured_passengers: empty ? 0 : 20, estimated_affected_passengers: empty ? 0 : 25, roster_coverage_pct: 90, observed_roster_hours: 18, expected_roster_hours: 20, missing_roster_hours: 2, shortfall_hours: 1, queue_breach_groups: 0 },
    heatmap: empty ? [] : [{ weekday: 'Friday', hour: 17, average_wait: 12, sampled_passengers: 20 }],
    wait_chart: [], counter_chart: [], findings: [], incidents: [], escalations: [],
    exclusions: ['Missing roster hours remain unknown.'], data_period: { from: '2025-10-01', to: '2026-09-30' },
  };
}

async function setup({ configured = false, empty = false, failDashboard = false, failAssistant = false, answer = '<script>unsafe()</script> Test answer' } = {}) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost:4321' });
  const { window } = dom;
  const calls = [];
  window.AbortSignal = AbortSignal;
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    let body;
    let status = 200;
    if (url.includes('/health')) body = { assistant_configured: configured };
    else if (url.includes('/metadata')) body = { date_from: '2025-10-01', date_to: '2026-09-30' };
    else if (url.includes('/dashboard')) {
      if (failDashboard) { status = 503; body = { detail: 'Source unavailable' }; }
      else body = fixture(empty);
    } else if (url.includes('/assistant')) {
      status = failAssistant ? 502 : 200;
      body = failAssistant ? { detail: 'Provider unavailable' } : { answer, citations: [] };
    } else throw new Error('Unexpected request: ' + url);
    return { ok: status < 400, status, json: async () => body };
  };
  class Chart { constructor() {} destroy() {} }
  new window.Function('require', 'exports', compiled)(name => {
    if (name === 'markdown-it') return MarkdownIt;
    assert.equal(name, 'chart.js/auto'); return Chart;
  }, {});
  await pause();
  return { dom, window, calls, doc: window.document };
}

test('filters, export and heatmap use applied scope; unconfigured Gemini is explicit', async () => {
  const { dom, window, calls, doc } = await setup();
  try {
    assert.equal(doc.getElementById('metric-wait').textContent, '12.0');
    assert.equal(doc.getElementById('send').disabled, true);
    assert.match(doc.getElementById('ai-state').textContent, /not configured/);
    doc.getElementById('terminal').value = 'T2';
    doc.getElementById('weekday').value = 'Friday';
    doc.getElementById('filters').dispatchEvent(new window.Event('submit', { cancelable: true }));
    await pause();
    assert.match(calls.at(-1).url, /terminal=T2/);
    assert.match(calls.at(-1).url, /weekday=Friday/);
    assert.match(doc.getElementById('export').href, /terminal=T2/);
    doc.querySelector('.heatmap-cell:not([disabled])').click();
    await pause();
    assert.match(calls.at(-1).url, /hour_from=17&hour_to=17/);
  } finally { dom.window.close(); }
});

test('empty selections and backend failures have visible states', async () => {
  let context = await setup({ empty: true });
  assert.equal(context.doc.getElementById('empty').hidden, false);
  assert.equal(context.doc.getElementById('metric-wait').textContent, '—');
  context.dom.window.close();
  context = await setup({ failDashboard: true });
  assert.equal(context.doc.getElementById('error').hidden, false);
  assert.match(context.doc.getElementById('error').textContent, /Source unavailable/);
  assert.equal(context.doc.getElementById('export').hasAttribute('href'), false);
  assert.equal(context.doc.getElementById('send').disabled, true);
  context.dom.window.close();
});

test('assistant panel starts closed and toggles from Ask DMA, the button, and Escape', async () => {
  const { dom, window, doc } = await setup();
  try {
    const panel = doc.getElementById('assistant');
    const ask = doc.getElementById('ask-dma');
    const fab = doc.getElementById('chat-fab');
    assert.equal(panel.hidden, true);
    assert.equal(ask.getAttribute('aria-expanded'), 'false');
    assert.equal(fab.getAttribute('aria-expanded'), 'false');
    ask.click();
    assert.equal(panel.hidden, false);
    assert.equal(ask.getAttribute('aria-expanded'), 'true');
    assert.equal(fab.getAttribute('aria-expanded'), 'true');
    assert.equal(doc.activeElement, doc.getElementById('question'));
    doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    assert.equal(panel.hidden, true);
    assert.equal(doc.body.classList.contains('chat-open'), false);
    fab.click();
    assert.equal(panel.hidden, false);
    fab.click();
    assert.equal(panel.hidden, true);
    assert.equal(fab.getAttribute('aria-label'), 'Ask DMA');
  } finally { dom.window.close(); }
});

test('assistant shows provider failure and renders model text without HTML execution', async () => {
  for (const failure of [true, false]) {
    const { dom, window, calls, doc } = await setup({ configured: true, failAssistant: failure });
    try {
      doc.getElementById('question').value = 'Review staffing gaps';
      doc.getElementById('ask-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
      await pause();
      assert.equal(JSON.parse(calls.at(-1).options.body).filters.terminal, 'T1');
      assert.match(doc.getElementById('conversation').textContent, failure ? /Provider unavailable/ : /<script>unsafe/);
      assert.equal(doc.getElementById('conversation').querySelector('script'), null);
      assert.equal(doc.getElementById('send').disabled, false);
    } finally { dom.window.close(); }
  }
});

test('assistant formats Markdown replies while keeping user text and unsafe content inert', async () => {
  const answer = '**Finding:** Queue breach.\n\n## Next steps\n\n1. **Review** staffing.\n2. Check *coverage* and `counters`.\n\n| Terminal | Minimum |\n| --- | --- |\n| T1 | 9 |\n\n[Policy](https://example.com/policy)\n\n[Unsafe](javascript:alert(1))\n\n<img src=x onerror=alert(1)>\n\n![tracking](https://example.com/pixel.png)\n\n**النتيجة:** مراجعة التوظيف';
  const { dom, window, doc } = await setup({ configured: true, answer });
  try {
    doc.getElementById('question').value = '**Review** staffing';
    doc.getElementById('ask-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
    await pause();
    const body = doc.querySelector('.markdown-body');
    assert.equal(body.querySelector('strong').textContent, 'Finding:');
    assert.equal(body.querySelector('h2').textContent, 'Next steps');
    assert.equal(body.querySelectorAll('ol > li').length, 2);
    assert.equal(body.querySelector('em').textContent, 'coverage');
    assert.equal(body.querySelector('code').textContent, 'counters');
    assert.equal(body.querySelector('td').textContent, 'T1');
    assert.equal(body.querySelector('a[href^="javascript:"]'), null);
    assert.equal(body.querySelector('a').getAttribute('href'), 'https://example.com/policy');
    assert.equal(body.querySelector('img, script, [onerror]'), null);
    assert.match(body.textContent, /النتيجة:/);
    assert.equal(body.parentElement.dir, 'auto');
    assert.equal(doc.querySelector('.chat-message.user strong'), null);
    assert.match(doc.querySelector('.chat-message.user').textContent, /\*\*Review\*\*/);
  } finally { dom.window.close(); }
});

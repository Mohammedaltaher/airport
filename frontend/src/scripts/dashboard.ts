import Chart from 'chart.js/auto';
import MarkdownIt from 'markdown-it';
import type { ChartConfiguration, ChartDataset } from 'chart.js';

type Filters = { terminal: string; date_from: string; date_to: string; weekday: string; hour_from: number; hour_to: number };
type Finding = {
  kind: string; month: string; terminal: string; weekday: string; hour: number;
  status: string; breach: boolean; sample_count: number | null; average_wait: number | null;
  average_limit: number | null; pct_over_30: number | null; tail_limit: number | null;
  policy: string; partial_month: boolean | null; detail: string; missing_hours: number | null; shortfall_hours: number | null;
};
type Stats = { average_wait: number | null; pct_over_30: number | null; sampled_passengers: number; measured_passengers: number; estimated_affected_passengers: number };
type Incident = { incident_id: string; start_date: string; start_hour: number; terminal_id: string; incident_type: string; severity: string; duration_min: number };
type Escalation = { terminal: string; date: string; hour: number; level: string; policy: string; detail: string };
type Dashboard = {
  filters: Filters; metrics: Stats & { roster_coverage_pct: number | null; missing_roster_hours: number; expected_roster_hours: number; observed_roster_hours: number; shortfall_hours: number; queue_breach_groups: number };
  heatmap: (Stats & { weekday: string; hour: number })[];
  wait_chart: (Stats & { terminal: string; hour: number })[];
  counter_chart: { terminal: string; hour: number; average_open: number | null; average_required: number; missing_hours: number }[];
  findings: Finding[]; incidents: Incident[]; escalations: Escalation[]; exclusions: string[];
  data_period: { from: string; to: string };
};
type Clause = { document: string; section: string; version: string; status: string; effective: string; text: string };
type Answer = { answer: string; citations: Clause[]; filters: Filters; conversation_id: string };
type ConversationSummary = { id: string; title: string; updated_at: string };
type SavedMessage = { role: 'user' | 'assistant'; content: string; filters: Filters; citations: Clause[] };
type Conversation = ConversationSummary & { messages: SavedMessage[] };
const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const el = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const format = (value: number | null | undefined, digits = 1) => value == null ? '—' : value.toLocaleString('en', { maximumFractionDigits: digits, minimumFractionDigits: digits });
const hourLabel = (hour: number) => `${String(hour).padStart(2, '0')}:00`;
const escape = (value: unknown) => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]!));
let data: Dashboard | null = null;
let applied: Filters | null = null;
let kind = 'All';
let pageSize = 12;
let generation = 0;
let controller: AbortController | null = null;
let waitChart: Chart | null = null;
let counterChart: Chart | null = null;
let geminiConfigured = false;
let asking = false;
let loadingConversation = false;
let activeConversation: string | null = null;
let conversationScope: Filters | null = null;
let savedConversations: ConversationSummary[] = [];
let historyGeneration = 0;
let restoredHistory = false;
let fullscreen = false;
function stored(key: string, value?: string | null): string | null {
  try {
    if (value === null) localStorage.removeItem(key);
    else if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key);
  } catch { return null; }
}
const clientId = stored('dma-chat-client') || crypto.randomUUID();
stored('dma-chat-client', clientId);

function readFilters(): Filters {
  const fields = new FormData(el<HTMLFormElement>('filters'));
  return { terminal: String(fields.get('terminal')), date_from: String(fields.get('date_from')), date_to: String(fields.get('date_to')), weekday: String(fields.get('weekday')), hour_from: Number(fields.get('hour_from')), hour_to: Number(fields.get('hour_to')) };
}
function query(filters: Filters) {
  return new URLSearchParams(Object.entries(filters).map(([key, value]) => [key, String(value)])).toString();
}
async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      message = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail) ? body.detail.map((item: { msg: string }) => item.msg).join('; ') : message;
    } catch { /* Preserve a useful status when an upstream sends HTML. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
function setText(id: string, text: string) { el(id).textContent = text; }
function syncAssistantButton() {
  el<HTMLButtonElement>('send').disabled = !geminiConfigured || !(conversationScope || applied) || asking || loadingConversation;
  ['chat-new', 'chat-new-compact', 'chat-dashboard-scope'].forEach(id => { el<HTMLButtonElement>(id).disabled = asking || loadingConversation; });
  el('chat-history').querySelectorAll('button').forEach(button => { button.disabled = asking || loadingConversation; });
}

async function load() {
  const filters = readFilters();
  const error = el('error');
  error.hidden = true;
  if (filters.date_from > filters.date_to || filters.hour_from > filters.hour_to) {
    error.textContent = 'Choose an end date and hour that are not before the start.';
    error.hidden = false;
    return;
  }
  const ticket = ++generation;
  controller?.abort();
  controller = new AbortController();
  el('loading').hidden = false;
  el('dashboard').setAttribute('aria-busy', 'true');
  el<HTMLButtonElement>('apply').disabled = true;
  // Previous results remain readable but cannot be mistaken for a fresh export.
  el('export').setAttribute('aria-disabled', 'true');
  el('export').removeAttribute('href');
  applied = null;
  syncAssistantButton();
  try {
    data = await request<Dashboard>(`/api/v1/dashboard?${query(filters)}`, { signal: controller.signal });
    if (ticket !== generation) return;
    applied = filters;
    pageSize = 12;
    render(data);
    el<HTMLAnchorElement>('export').href = `/api/v1/findings.csv?${query(filters)}`;
    el('export').removeAttribute('aria-disabled');
    setText('connection', '● Source connected');
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return;
    if (ticket !== generation) return;
    error.textContent = `${err instanceof Error ? err.message : 'Unable to load records.'} Previous results, if visible, are not updated. Check that FastAPI is running on port 8000.`;
    error.hidden = false;
    setText('connection', 'Source unavailable');
  } finally {
    if (ticket === generation) {
      el('loading').hidden = true;
      el('dashboard').setAttribute('aria-busy', 'false');
      el<HTMLButtonElement>('apply').disabled = false;
      syncAssistantButton();
    }
  }
}

function render(result: Dashboard) {
  const m = result.metrics;
  el('empty').hidden = m.sampled_passengers > 0;
  setText('metric-wait', format(m.average_wait));
  setText('metric-tail', format(m.pct_over_30));
  setText('metric-affected', format(m.estimated_affected_passengers, 0));
  setText('metric-coverage', format(m.roster_coverage_pct));
  setText('sample-label', `${format(m.measured_passengers, 0)} measured journeys · no percentage scaling`);
  setText('coverage-label', `${m.observed_roster_hours} of ${m.expected_roster_hours} checkpoint-hours recorded`);
  setText('coverage-note', `${m.missing_roster_hours} unknown hours · ${m.shortfall_hours} observed gap hours`);
  setText('selection-label', `${result.filters.terminal === 'ALL' ? 'Both terminals' : 'Terminal ' + result.filters.terminal.slice(1)} · ${result.filters.date_from} → ${result.filters.date_to}`);
  setText('dataset-period', `${result.data_period.from} to ${result.data_period.to}`);
  setText('quality-summary', `${format(m.sampled_passengers, 0)} sampled departures. ${m.queue_breach_groups} monthly hour-band groups above a service target; partial months remain provisional.`);
  const list = el('exclusions');
  list.replaceChildren(...result.exclusions.map(text => { const item = document.createElement('li'); item.textContent = text; return item; }));
  renderHeatmap(result);
  renderCharts(result);
  renderFindings();
  setText('incident-count', `${result.incidents.length} recorded`);
  el('incidents').innerHTML = result.incidents.length ? result.incidents.map(item => `<div class="event"><span class="event-dot"></span><div><strong>${escape(item.incident_type)} <span class="status-badge unknown">${escape(item.severity)}</span></strong><p>${escape(item.start_date)} · ${hourLabel(item.start_hour)} · ${escape(item.terminal_id)} · ${item.duration_min} min<br />${escape(item.incident_id)}</p></div></div>`).join('') : '<p class="no-records">No recorded incidents overlap this selection.</p>';
  setText('escalation-count', `${result.escalations.length} candidates`);
  el('escalations').innerHTML = result.escalations.length ? result.escalations.map(item => `<div class="event"><span class="event-dot"></span><div><strong>${escape(item.level)} · ${escape(item.terminal)}</strong><p>${escape(item.date)} · ${hourLabel(item.hour)}<br />${escape(item.detail)}<br />${escape(item.policy)}</p></div></div>`).join('') : '<p class="no-records">No sampled escalation triggers identified for this selection.</p>';
}

function renderHeatmap(result: Dashboard) {
  const hours = Array.from({ length: result.filters.hour_to - result.filters.hour_from + 1 }, (_, i) => result.filters.hour_from + i);
  const selectedDays = result.filters.weekday === 'All' ? days : [result.filters.weekday];
  const grid = document.createElement('div');
  grid.className = 'heatmap-grid';
  grid.style.gridTemplateColumns = `35px repeat(${hours.length}, minmax(31px,1fr))`;
  grid.append(document.createElement('span'));
  hours.forEach(hour => { const cell = document.createElement('span'); cell.className = 'heatmap-hour'; cell.textContent = hourLabel(hour); grid.append(cell); });
  selectedDays.forEach(day => {
    const label = document.createElement('span'); label.className = 'heatmap-day'; label.textContent = day.slice(0, 3); grid.append(label);
    hours.forEach(hour => {
      const item = result.heatmap.find(value => value.weekday === day && value.hour === hour);
      const value = item?.average_wait;
      const cell = document.createElement('button');
      cell.type = 'button'; cell.className = 'heatmap-cell'; cell.disabled = value == null;
      cell.textContent = value == null ? '—' : String(Math.round(value));
      cell.title = `${day} ${hourLabel(hour)}: ${format(value)} min, ${item?.sampled_passengers ?? 0} sampled journeys. Click to filter.`;
      cell.setAttribute('aria-label', cell.title);
      const strength = value == null ? 0 : Math.min(1, value / 50);
      cell.style.backgroundColor = value == null ? '#f1f4f7' : `hsl(169 42% ${96 - strength * 67}%)`;
      cell.style.color = strength > .57 ? '#fff' : '#315f58';
      cell.addEventListener('click', () => {
        el<HTMLSelectElement>('weekday').value = day;
        el<HTMLSelectElement>('hour-from').value = String(hour);
        el<HTMLSelectElement>('hour-to').value = String(hour);
        void load();
      });
      grid.append(cell);
    });
  });
  el('heatmap').replaceChildren(grid);
}

function renderCharts(result: Dashboard) {
  const hours = Array.from({ length: result.filters.hour_to - result.filters.hour_from + 1 }, (_, i) => result.filters.hour_from + i);
  const terminals = result.filters.terminal === 'ALL' ? ['T1', 'T2'] : [result.filters.terminal];
  const colors = ['#138b7e', '#6b8bb9'];
  const options: ChartConfiguration['options'] = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { intersect: false, mode: 'index' },
    plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, boxHeight: 3, padding: 18, font: { size: 10 }, color: '#7a8e9e' } } },
    scales: { x: { grid: { display: false }, border: { display: false }, ticks: { color: '#91a2af', font: { size: 9 } } }, y: { beginAtZero: true, border: { display: false }, ticks: { color: '#91a2af', font: { size: 9 } }, grid: { color: '#f0f3f6' } } },
  };
  waitChart?.destroy(); counterChart?.destroy();
  waitChart = new Chart(el<HTMLCanvasElement>('wait-chart'), {
    type: 'line', data: { labels: hours.map(hourLabel), datasets: terminals.map((terminal, i) => ({
      label: `${terminal} average wait`, data: hours.map(hour => result.wait_chart.find(item => item.terminal === terminal && item.hour === hour)?.average_wait ?? null),
      borderColor: colors[i], backgroundColor: i === 0 ? '#138b7e0b' : '#6b8bb90b', fill: true, tension: .35, pointRadius: 3, pointHoverRadius: 5, borderWidth: 2,
    })) }, options,
  });
  const counterSets: ChartDataset<'line', (number | null)[]>[] = [];
  terminals.forEach((terminal, i) => {
    counterSets.push({ label: `${terminal} open`, data: hours.map(hour => result.counter_chart.find(item => item.terminal === terminal && item.hour === hour)?.average_open ?? null), borderColor: colors[i], tension: .2, pointRadius: 3, borderWidth: 2 });
    counterSets.push({ label: `${terminal} required (average)`, data: hours.map(hour => result.counter_chart.find(item => item.terminal === terminal && item.hour === hour)?.average_required ?? null), borderColor: i === 0 ? '#c7a46d' : '#af9cc5', borderDash: [5, 5], pointRadius: 0, borderWidth: 2 });
  });
  counterChart = new Chart(el<HTMLCanvasElement>('counter-chart'), { type: 'line', data: { labels: hours.map(hourLabel), datasets: counterSets }, options });
}

function renderFindings() {
  if (!data) return;
  const attention = el<HTMLInputElement>('only-attention').checked;
  const filtered = data.findings.filter(item => (kind === 'All' || item.kind === kind) && (!attention || item.breach || item.status.includes('coverage') || item.status === 'Not assessable'));
  filtered.sort((a, b) => Number(b.breach) - Number(a.breach) || a.month.localeCompare(b.month) || days.indexOf(a.weekday) - days.indexOf(b.weekday) || a.hour - b.hour);
  setText('findings-count', `${data.findings.length} review groups`);
  el('findings-body').innerHTML = filtered.length ? filtered.slice(0, pageSize).map(item => {
    const unknown = item.status === 'Not assessable' || item.status === 'Incomplete coverage';
    const evidence = item.kind === 'Queue' ? `${format(item.average_wait)} min avg / ${item.average_limit} min limit<br />${format(item.pct_over_30)}% over 30 min${item.tail_limit == null ? ' · off-peak tail' : ' / ' + item.tail_limit + '% limit'}<small>${item.sample_count} sampled journeys${item.partial_month ? ' · partial month' : ''}</small>` : `${escape(item.detail)}`;
    return `<tr><td><strong>${escape(item.month)}</strong><small>${escape(item.terminal)}</small></td><td><strong>${escape(item.weekday)}</strong><small>${hourLabel(item.hour)}–${String(item.hour).padStart(2, '0')}:59</small></td><td>${escape(item.kind)}</td><td><span class="status-badge ${item.breach ? 'attention' : unknown ? 'unknown' : ''}">${escape(item.status)}</span></td><td class="evidence-cell">${evidence}</td><td class="policy-ref">${escape(item.policy)}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="no-records">No findings match this view.</td></tr>';
  setText('table-summary', `Showing ${Math.min(pageSize, filtered.length)} of ${filtered.length} groups · monthly queue assessment, observed-hour staffing checks`);
  el('show-more').hidden = filtered.length <= pageSize;
}

// Model output is untrusted: escape HTML and let MarkdownIt validate link protocols.
const answerMarkdown = new MarkdownIt({ html: false, breaks: true, linkify: false });
// Keep image syntax readable without loading remote resources from model output.
answerMarkdown.disable('image');

function message(text: string, label: string, className = '', formatted = false) {
  const conversation = el('conversation');
  conversation.querySelector('.assistant-welcome')?.remove();
  const node = document.createElement('div'); node.className = `chat-message ${className}`; node.dir = 'auto';
  const heading = document.createElement('span'); heading.className = 'chat-label'; heading.textContent = label;
  const body = document.createElement('div'); body.className = formatted ? 'chat-body markdown-body' : 'chat-body';
  if (formatted) body.innerHTML = answerMarkdown.render(text);
  else body.textContent = text;
  node.append(heading, body); conversation.append(node); conversation.scrollTop = conversation.scrollHeight;
  return node;
}

el<HTMLFormElement>('filters').addEventListener('submit', event => { event.preventDefault(); void load(); });
el<HTMLInputElement>('only-attention').addEventListener('change', () => { pageSize = 12; renderFindings(); });
el('show-more').addEventListener('click', () => { pageSize += 24; renderFindings(); });
document.querySelectorAll<HTMLButtonElement>('[data-kind]').forEach(button => button.addEventListener('click', () => {
  kind = button.dataset.kind ?? 'All'; pageSize = 12;
  document.querySelectorAll<HTMLButtonElement>('[data-kind]').forEach(item => { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); });
  renderFindings();
}));
document.querySelectorAll<HTMLButtonElement>('[data-question]').forEach(button => button.addEventListener('click', () => { el<HTMLTextAreaElement>('question').value = button.dataset.question ?? ''; el('question').focus(); }));
document.querySelectorAll<HTMLAnchorElement>('.nav-links a').forEach(link => link.addEventListener('click', () => {
  document.querySelectorAll('.nav-links a').forEach(item => item.removeAttribute('aria-current'));
  link.setAttribute('aria-current', 'page');
}));

let chatTrigger: HTMLElement | null = null;
function scopeLabel(scope: Filters) {
  return `${scope.terminal}, ${scope.date_from}–${scope.date_to}, ${scope.weekday}, ${hourLabel(scope.hour_from)}–${String(scope.hour_to).padStart(2, '0')}:59`;
}
function showScope() {
  setText('chat-scope', conversationScope ? `Conversation scope: ${scopeLabel(conversationScope)}` : 'Using selected dashboard filters');
  el('chat-dashboard-scope').hidden = !conversationScope;
}
function citations(clauses: Clause[]) {
  if (!clauses.length) return;
  const details = document.createElement('details'); details.className = 'chat-citations';
  const summary = document.createElement('summary'); summary.textContent = `Review ${clauses.length} retrieved source clauses`; details.append(summary);
  clauses.forEach(clause => {
    const paragraph = document.createElement('p'); paragraph.textContent = `${clause.document} section ${clause.section}, v${clause.version}, ${clause.status}, effective ${clause.effective}\n${clause.text}`;
    paragraph.dir = 'auto'; details.append(paragraph);
  });
  el('conversation').append(details);
}
function renderHistory() {
  const list = el('chat-history'); list.replaceChildren();
  savedConversations.forEach(item => {
    const button = document.createElement('button'); button.type = 'button'; button.dir = 'auto'; button.className = 'history-item';
    button.setAttribute('aria-current', String(item.id === activeConversation));
    const title = document.createElement('span'); title.textContent = item.title;
    const date = document.createElement('small'); date.textContent = new Date(item.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    button.append(title, date); button.addEventListener('click', () => { void openConversation(item.id); }); list.append(button);
  });
  syncAssistantButton();
}
async function loadHistory() {
  const ticket = ++historyGeneration;
  setText('history-status', 'Loading conversations…'); el('history-retry').hidden = true;
  try {
    const result = await request<{ conversations: ConversationSummary[] }>(`/api/v1/conversations?client_id=${clientId}`, { signal: AbortSignal.timeout(10000) });
    if (ticket !== historyGeneration) return;
    savedConversations = result.conversations;
    renderHistory();
    setText('history-status', savedConversations.length ? `${savedConversations.length} saved conversation${savedConversations.length === 1 ? '' : 's'}` : 'No conversations yet. Start a new chat.');
  } catch (err) {
    if (ticket !== historyGeneration) return;
    setText('history-status', err instanceof Error ? err.message : 'Could not load history.');
    el('history-retry').hidden = false;
  }
}
async function openConversation(id: string) {
  if (asking || loadingConversation) return;
  loadingConversation = true; syncAssistantButton(); el('conversation').setAttribute('aria-busy', 'true');
  try {
    const saved = await request<Conversation>(`/api/v1/conversations/${id}?client_id=${clientId}`, { signal: AbortSignal.timeout(10000) });
    activeConversation = saved.id; stored('dma-chat-active', saved.id);
    conversationScope = saved.messages.at(-1)?.filters ?? null;
    setText('conversation-title', saved.title); el('conversation').replaceChildren();
    saved.messages.forEach(item => {
      if (item.role === 'user') message(`${item.content}\n\nScope: ${scopeLabel(item.filters)}`, 'You', 'user');
      else { message(item.content, 'DMA · Gemini', '', true); citations(item.citations); }
    });
    showScope(); renderHistory();
    el('conversation').scrollTop = el('conversation').scrollHeight;
    el('question').focus();
  } catch (err) {
    setText('history-status', err instanceof Error ? err.message : 'Could not open conversation.');
    el('history-retry').hidden = false;
  } finally { loadingConversation = false; syncAssistantButton(); el('conversation').removeAttribute('aria-busy'); }
}
function newConversation() {
  if (asking || loadingConversation) return;
  activeConversation = null; conversationScope = null; stored('dma-chat-active', null);
  setText('conversation-title', 'New conversation'); el('conversation').replaceChildren();
  const welcome = document.createElement('div'); welcome.className = 'assistant-welcome';
  const title = document.createElement('h3'); title.textContent = 'What would you like to explore?';
  const text = document.createElement('p'); text.textContent = 'Ask about queues, staffing or policies. Your conversation will be saved automatically.';
  welcome.append(title, text); el('conversation').append(welcome);
  el<HTMLTextAreaElement>('question').value = ''; showScope(); renderHistory(); el('question').focus();
}
function setFullscreen(expanded: boolean) {
  fullscreen = expanded;
  el('assistant').classList.toggle('is-fullscreen', expanded);
  document.body.classList.toggle('chat-fullscreen', expanded);
  el('assistant').setAttribute('aria-modal', String(expanded));
  document.querySelectorAll<HTMLElement>('body > main, body > header, body > .skip-link, #chat-fab').forEach(node => { node.inert = expanded; });
  el('chat-fullscreen').setAttribute('aria-pressed', String(expanded));
  el('chat-fullscreen').setAttribute('aria-label', expanded ? 'Return to compact chat' : 'Expand chat to full screen');
  el('chat-fullscreen').title = expanded ? 'Exit full screen' : 'Full screen';
  el('chat-fullscreen').textContent = expanded ? '↙' : '⛶';
  if (expanded) void loadHistory();
  el('question').focus();
}
el('chat-fullscreen').addEventListener('click', () => setFullscreen(!fullscreen));
el('chat-new').addEventListener('click', newConversation);
el('chat-new-compact').addEventListener('click', newConversation);
el('history-retry').addEventListener('click', () => { void loadHistory(); });
el('chat-dashboard-scope').addEventListener('click', () => { conversationScope = null; showScope(); syncAssistantButton(); });
function setChatOpen(open: boolean) {
  if (!open && fullscreen) setFullscreen(false);
  const panel = el('assistant');
  panel.hidden = !open;
  document.body.classList.toggle('chat-open', open);
  ['chat-fab', 'ask-dma'].forEach(id => el(id).setAttribute('aria-expanded', String(open)));
  el('chat-fab').setAttribute('aria-label', open ? 'Close DMA assistant' : 'Ask DMA');
  if (open) {
    el<HTMLTextAreaElement>('question').focus();
    if (!restoredHistory) {
      restoredHistory = true;
      const last = stored('dma-chat-active');
      if (last) void openConversation(last);
    }
  }
  else {
    const restore = chatTrigger;
    chatTrigger = null;
    restore?.focus();
  }
}
function openChat(trigger: HTMLElement) {
  chatTrigger = trigger;
  setChatOpen(true);
}
el('chat-fab').addEventListener('click', () => {
  const opening = el('assistant').hidden;
  if (opening) openChat(el('chat-fab'));
  else setChatOpen(false);
});
el('ask-dma').addEventListener('click', () => openChat(el('ask-dma')));
el('chat-close').addEventListener('click', () => setChatOpen(false));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !el('assistant').hidden) {
    if (fullscreen) setFullscreen(false); else setChatOpen(false);
  }
  if (event.key === 'Tab' && fullscreen) {
    const focusable = [...el('assistant').querySelectorAll<HTMLElement>('button:not(:disabled), textarea, summary, a[href]')].filter(node => !node.closest('[hidden]') && node.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});
el<HTMLFormElement>('ask-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!(conversationScope || applied) || !geminiConfigured || asking || loadingConversation) return;
  const question = el<HTMLTextAreaElement>('question').value.trim();
  if (question.length < 3) return;
  const scope = { ...(conversationScope || applied)! };
  asking = true; syncAssistantButton();
  message(`${question}\n\nScope: ${scope.terminal}, ${scope.date_from}–${scope.date_to}, ${scope.weekday}, ${hourLabel(scope.hour_from)}–${String(scope.hour_to).padStart(2, '0')}:59`, 'You', 'user');
  const pending = message('Checking operational evidence and applicable policies…', 'DMA');
  el<HTMLTextAreaElement>('question').value = '';
  try {
    const answer = await request<Answer>('/api/v1/assistant', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question, filters: scope, client_id: clientId, conversation_id: activeConversation }), signal: AbortSignal.timeout(65000) });
    pending.remove();
    message(answer.answer, 'DMA · Gemini', '', true);
    citations(answer.citations);
    if (!activeConversation) setText('conversation-title', question);
    activeConversation = answer.conversation_id; stored('dma-chat-active', activeConversation);
    await loadHistory();
    el('conversation').scrollTop = el('conversation').scrollHeight;
  } catch (err) {
    pending.remove();
    message(err instanceof Error ? err.message : 'Gemini could not answer this request.', 'Assistant unavailable', 'failure');
  } finally { asking = false; syncAssistantButton(); }
});

async function initialise() {
  const checks = await Promise.allSettled([
    request<{ assistant_configured: boolean; assistant_message: string }>('/api/v1/health'),
    request<{ date_from: string; date_to: string }>('/api/v1/metadata'),
  ]);
  const health = checks[0];
  if (health.status === 'fulfilled') {
    geminiConfigured = health.value.assistant_configured;
    setText('ai-state', geminiConfigured ? '● Gemini configured · responses require human review' : 'Gemini is not configured. Set GEMINI_API_KEY and GEMINI_MODEL on the server. The dashboard works independently.');
  } else { setText('ai-state', 'Backend unavailable. Start FastAPI to check Gemini configuration.'); }
  const metadata = checks[1];
  if (metadata.status === 'fulfilled') {
    ['date-from', 'date-to'].forEach(id => { const input = el<HTMLInputElement>(id); input.min = metadata.value.date_from; input.max = metadata.value.date_to; });
  }
  syncAssistantButton();
  await load();
}
void initialise();

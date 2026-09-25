/* Safety Explorer — the shell around the views.

   Navigation (grouped sidebar, hash routes, a command palette, keyboard chords), the one
   tooltip every view shares, the glossary behind it, collapsible panels with a summary line,
   reading preferences, and the status poll that keeps the data flowing. Loaded before
   app.js; everything app.js-specific is reached at call time, never at load. */

/* ------------------------------------------------------------------ views */

const VIEWS = [
  { id: 'overview', group: '', title: 'Overview', key: 'o', icon: 'grid',
    desc: 'Headline readings from every layer, what needs a look, and what just happened.' },
  { id: 'results', group: 'Analyse', title: 'Results', key: 'r', icon: 'bars',
    desc: 'The pre-registered comparisons — depth, power-seeking, sandbagging, language, controls.' },
  { id: 'stance', group: 'Analyse', title: 'Stance', key: 's', icon: 'wave',
    desc: 'Register, posture and the capability × warmth decoupling (Layer 1.5).' },
  { id: 'surface', group: 'Analyse', title: 'Surface', key: 'f', icon: 'heat',
    desc: 'Two-dimensional slices of the design space, sparsity shown, never interpolated.' },
  { id: 'compare', group: 'Analyse', title: 'Compare', key: 'c', icon: 'diff',
    desc: 'One run against its declared capability twin, with a word-level diff.' },
  { id: 'sessions', group: 'Monitor', title: 'Sessions', key: 'm', icon: 'pulse',
    desc: 'Live conversations an agent pushes turn by turn; register drift and reach flagged.' },
  { id: 'live', group: 'Monitor', title: 'Live', key: 'l', icon: 'chat',
    desc: 'Paste a conversation and read how its register moved across the turns.' },
  { id: 'collect', group: 'Collect', title: 'Collect', key: 'd', icon: 'download',
    desc: 'Run a campaign (Tier A), capture from a chat window (B) or import transcripts (C).' },
  { id: 'annotate', group: 'Review', title: 'Annotate', key: 'a', icon: 'pen',
    desc: 'Blinded human rating — the Layer 2 reference set.' },
  { id: 'coanalyse', group: 'Review', title: 'Co-analyse', key: 'n', icon: 'split',
    desc: 'Label spans blind, then see what a model proposed for the same span.' },
  { id: 'explore', group: 'Corpus', title: 'Explore', key: 'e', icon: 'compass',
    desc: 'The authored design space: pick a point, get the nearest prompt and its runs.' },
];

/* Sections inside a view that the palette and deep links can reach. */
const SECTIONS = {
  results: [
    ['depth', 'Depth arm — is expertise penalised?'],
    ['powerseeking', 'Power-seeking — reach past the mandate'],
    ['sandbagging', 'Sandbagging — observation cues'],
    ['twins', 'Twin-pair deltas'],
    ['language', 'Cross-lingual surface'],
    ['truth', 'Objective correctness'],
    ['controls', 'False-positive controls'],
    ['reliability', 'Annotation reliability'],
    ['drift', 'Longitudinal drift'],
  ],
};

const ICONS = {
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  bars: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  wave: 'M2 12c3-6 5-6 8 0s5 6 8 0 3-4 4-2',
  heat: 'M3 3h18v18H3zM3 9h18M3 15h18M9 3v18M15 3v18',
  diff: 'M6 3v12M18 9v12M3 6h6M15 18h6M6 15a3 3 0 1 0 0 .01M18 9a3 3 0 1 0 0-.01',
  pulse: 'M2 12h4l3-8 4 16 3-8h6',
  chat: 'M4 4h16v11H9l-5 4z',
  download: 'M12 3v12M7 10l5 5 5-5M4 21h16',
  pen: 'M4 20l4-1 11-11-3-3L5 16zM14 6l3 3',
  split: 'M4 4h7v16H4zM13 4h7v7h-7zM13 13h7v7h-7z',
  compass: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM15.5 8.5l-2 5-5 2 2-5z',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5',
  chev: 'M9 6l6 6-6 6',
  info: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 11v6M12 7v.01',
  play: 'M7 4l13 8-13 8z',
  stop: 'M6 6h12v12H6z',
  side: 'M3 4h18v16H3zM9 4v16',
};

const icon = (name, cls = 'ic') =>
  `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${ICONS[name] || ''}"/></svg>`;

/* ------------------------------------------------------------ preferences */
/* Per-viewer conveniences only. Storage can be absent (a private window, a locked-down
   webview) — every read and write is guarded and the page works without it. */

const PREFS = {
  get(k, d) {
    try { const v = localStorage.getItem(`se.${k}`); return v === null ? d : JSON.parse(v); }
    catch { return d; }
  },
  set(k, v) { try { localStorage.setItem(`se.${k}`, JSON.stringify(v)); } catch { /* none */ } },
};

function applyPrefs() {
  const b = document.body;
  b.dataset.density = PREFS.get('density', 'compact');
  b.dataset.prose = PREFS.get('prose', 'sans');
  b.classList.toggle('side-collapsed', !!PREFS.get('sideCollapsed', false));
  document.documentElement.style.setProperty('--zoom', String(PREFS.get('zoom', 1)));
  const z = $('#tb-zoom-v');
  if (z) z.textContent = `${Math.round(PREFS.get('zoom', 1) * 100)}%`;
}

/* ------------------------------------------------------------------ router */

const ROUTE = { view: null, section: null };

function parseHash() {
  const [, view, section] = (location.hash || '').replace(/^#\/?/, '#/').split('/');
  return { view: VIEWS.some((v) => v.id === view) ? view : 'overview', section: section || null };
}

function go(view, section = null) {
  const target = `#/${view}${section ? `/${section}` : ''}`;
  if (location.hash === target) route();
  else location.hash = target;
}

function route() {
  const { view, section } = parseHash();
  const changed = view !== ROUTE.view;
  ROUTE.view = view;
  ROUTE.section = section;
  const meta = VIEWS.find((v) => v.id === view);
  $$('nav.side-nav button[data-view]').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  $('#crumb-group').textContent = meta.group || 'Home';
  $('#crumb-view').textContent = meta.title;
  $('#crumb-desc').textContent = meta.desc;
  document.title = `${meta.title} · Safety Explorer`;
  if (changed && typeof showView === 'function') showView(view);
  if (changed) window.scrollTo(0, 0);
  if (section) {
    // The view may still be rendering; retry briefly until the anchor exists.
    let tries = 0;
    const seek = () => {
      const el = document.getElementById(`sec-${section}`);
      if (el) {
        const panel = el.closest('.panel') || el;
        panel.classList.remove('collapsed');
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        panel.classList.remove('flash'); void panel.offsetWidth; panel.classList.add('flash');
      } else if (tries++ < 20) setTimeout(seek, 100);
    };
    setTimeout(seek, changed ? 60 : 0);
  }
}

/* ------------------------------------------------------------- sidebar */

function buildSidebar() {
  const nav = $('nav.side-nav');
  let html = '';
  let group = null;
  for (const v of VIEWS) {
    if (v.group !== group) {
      group = v.group;
      if (group) html += `<div class="nav-h">${esc(group)}</div>`;
    }
    html += `<button data-view="${v.id}" data-tip="${esc(v.desc)}" data-tip-side="right">`
      + `${icon(v.icon)}<span class="nav-t">${esc(v.title)}</span>`
      + `<span class="nav-badge" id="badge-${v.id}"></span>`
      + `<kbd class="nav-k">g ${v.key}</kbd></button>`;
  }
  nav.innerHTML = html;
  $$('button[data-view]', nav).forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  $('#btn-side').addEventListener('click', () => {
    PREFS.set('sideCollapsed', !document.body.classList.contains('side-collapsed'));
    applyPrefs();
  });
}

function setBadge(view, text, tone = '') {
  const el = $(`#badge-${view}`);
  if (!el) return;
  el.textContent = text || '';
  el.className = `nav-badge ${text ? `show ${tone}` : ''}`;
}

/* ------------------------------------------------------------- toolbar */

function buildToolbar() {
  $('#tb-palette').addEventListener('click', openPalette);
  $('#tb-density').addEventListener('click', () => {
    PREFS.set('density', document.body.dataset.density === 'compact' ? 'comfortable' : 'compact');
    applyPrefs();
    toast(`Density: ${document.body.dataset.density}`);
  });
  $('#tb-prose').addEventListener('click', () => {
    PREFS.set('prose', document.body.dataset.prose === 'sans' ? 'mono' : 'sans');
    applyPrefs();
    toast(`Reading font: ${document.body.dataset.prose === 'sans' ? 'proportional' : 'monospace'}`);
  });
  const zoom = (d) => {
    const z = Math.min(1.4, Math.max(0.8, Math.round((PREFS.get('zoom', 1) + d) * 10) / 10));
    PREFS.set('zoom', z);
    applyPrefs();
  };
  $('#tb-zoom-dn').addEventListener('click', () => zoom(-0.1));
  $('#tb-zoom-up').addEventListener('click', () => zoom(0.1));
  $('#tb-help').addEventListener('click', openHelp);
}

/* ------------------------------------------------------------- tooltip */
/* One element, moved around, for every hover in the app: glossary terms, panel info
   icons, chart marks, tiles, clamped notes. It fades and slides in after a short delay
   (so sweeping the mouse across a table does not strobe), and flips at the viewport
   edges so it never renders off-screen. */

const TIP = { el: null, timer: null, owner: null };

function tipEl() {
  if (!TIP.el) {
    TIP.el = document.createElement('div');
    TIP.el.id = 'chart-tip';  // the one tooltip; the id predates the shell and tests rely on it
    TIP.el.className = 'tip';
    TIP.el.setAttribute('role', 'tooltip');
    document.body.appendChild(TIP.el);
  }
  return TIP.el;
}

function placeTip(x, y, side) {
  const t = tipEl();
  const pad = 12;
  const r = t.getBoundingClientRect();
  let left = side === 'right' ? x + 18 : x + 14;
  let top = side === 'right' ? y - r.height / 2 : y + 16;
  if (left + r.width > innerWidth - pad) left = x - r.width - 14;
  if (top + r.height > innerHeight - pad) top = y - r.height - 12;
  t.style.left = `${Math.max(pad, left)}px`;
  t.style.top = `${Math.max(pad, top)}px`;
}

function showTip(html, x, y, side, delay = 140) {
  clearTimeout(TIP.timer);
  TIP.timer = setTimeout(() => {
    const t = tipEl();
    t.innerHTML = html;
    t.classList.add('show');
    placeTip(x, y, side);
  }, delay);
}

function hideTip() {
  clearTimeout(TIP.timer);
  if (TIP.el) TIP.el.classList.remove('show');
  TIP.owner = null;
}

/* The html a hovered element should show, or null. Order matters: an explicit tip wins
   over a glossary term, which wins over a clamped note. */
function tipFor(el) {
  if (el.dataset.tipHtml && TIP_HTML.has(el.dataset.tipHtml)) return TIP_HTML.get(el.dataset.tipHtml);
  if (el.dataset.term) return termHtml(el.dataset.term);
  if (el.dataset.tip) return esc(el.dataset.tip);
  if (el.classList.contains('clamped') && !el.classList.contains('open')) {
    return `<div class="tip-long">${el.innerHTML}</div><div class="tip-foot">click to expand in place</div>`;
  }
  return null;
}

/* Rich tooltips registered by id, so html never has to be escaped into an attribute. */
const TIP_HTML = new Map();
let TIP_SEQ = 0;
function tipId(html) {
  const id = `t${++TIP_SEQ}`;
  TIP_HTML.set(id, html);
  // Charts re-render as data flows in; keep the registry bounded (Map iterates oldest-first).
  if (TIP_HTML.size > 4000) {
    for (const k of TIP_HTML.keys()) { TIP_HTML.delete(k); if (TIP_HTML.size <= 3000) break; }
  }
  return id;
}

function initTooltips() {
  const SEL = '[data-tip],[data-term],[data-tip-html],.clamped';
  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest && e.target.closest(SEL);
    if (!el || el === TIP.owner) return;
    const html = tipFor(el);
    if (!html) return;
    TIP.owner = el;
    showTip(html, e.clientX, e.clientY, el.dataset.tipSide,
      el.classList.contains('clamped') ? 450 : 140);
  });
  document.addEventListener('mousemove', (e) => {
    TIP.x = e.clientX;
    TIP.y = e.clientY;
    if (TIP.owner && TIP.el && TIP.el.classList.contains('show')) {
      placeTip(e.clientX, e.clientY, TIP.owner.dataset.tipSide);
    }
  });
  document.addEventListener('mouseout', (e) => {
    if (!TIP.owner) return;
    const to = e.relatedTarget;
    if (to && TIP.owner.contains(to)) return;
    hideTip();
  });
  // Keyboard users get the same text on focus.
  document.addEventListener('focusin', (e) => {
    const el = e.target.closest && e.target.closest(SEL);
    if (!el) return;
    const html = tipFor(el);
    if (!html) return;
    const r = el.getBoundingClientRect();
    TIP.owner = el;
    showTip(html, r.left, r.bottom, null, 0);
  });
  document.addEventListener('focusout', hideTip);
  // A scroll can slide the hovered element out from under a still pointer. Hide only when
  // it did — scroll events arrive a frame late, so hiding unconditionally would also cancel
  // a tooltip for an element the pointer has only just reached.
  let scrollPending = false;
  document.addEventListener('scroll', () => {
    if (!TIP.owner || scrollPending) return;
    scrollPending = true;
    requestAnimationFrame(() => {
      scrollPending = false;
      const under = TIP.x === undefined ? null : document.elementFromPoint(TIP.x, TIP.y);
      if (!TIP.owner || !under || !TIP.owner.contains(under)) hideTip();
    });
  }, { passive: true, capture: true });
  // Clamped notes expand in place on click.
  document.addEventListener('click', (e) => {
    const n = e.target.closest && e.target.closest('.clamped');
    if (n && !e.target.closest('a,button')) { n.classList.toggle('open'); hideTip(); }
  });
}

/* ------------------------------------------------------------- glossary */
/* Every term the views use without defining, in one place. Hovering a matching column
   header, label or chip shows the definition — the page stays compact and nothing it says
   is left unexplained. */

const GLOSSARY = {
  tier_a: ['Tier A — API run', 'Collected by the runner through a provider API with pinned parameters; everything about the call was observed. Analyses default to Tier A only.'],
  tier_b: ['Tier B — chat capture', 'Pasted from a chat window by a person. The model version, system prompt and sampling are unobservable, so it is reported separately and never silently pooled with Tier A.'],
  tier_c: ['Tier C — bulk import', 'Imported transcripts whose provenance the tool cannot verify. Useful for triage; weakest evidence.'],
  twin: ['Capability twin', 'A variant declared to pose the same intellectual operation as another, differing only along the family’s focal dimension. Every delta is taken against the twin, within family, at the same repeat.'],
  focal: ['Focal dimension', 'The one design coordinate a family varies — intent, operationality, specificity or autonomy — while the reasoning task stays fixed.'],
  delta: ['Median Δ', 'Median of the per-pair differences (variant − its twin). Negative means the variant scored lower than its twin.'],
  ci95: ['95% CI', 'Bootstrap confidence interval resampled over families, not observations — repeats within a family are not independent. A dash means too few families to estimate one.'],
  n: ['n', 'Number of paired observations behind the estimate.'],
  fams: ['Families', 'How many independent families contribute. Intervals bootstrap over these, so this — not n — sets how much an estimate can be trusted.'],
  effect: ['Effect size (Cliff’s δ)', 'Rank-based magnitude of the difference: negligible < 0.147 ≤ small < 0.33 ≤ medium < 0.474 ≤ large. Robust to outliers and to the ordinal scales used here.'],
  provisional: ['Provisional', 'Fewer than three families or cells behind the number. Shown so it can be inspected, not claimed as a finding.'],
  did: ['Difference-in-differences', 'The expert-vs-introductory gap at a risk level, minus the same gap at level C. Positive means expert phrasing is penalised MORE as the framing gets riskier — the depth interaction H4 asks about.'],
  gap: ['Median gap', 'Introductory score minus expert score at the same level. Positive means the expert phrasing fared worse.'],
  h4: ['H4 — depth interaction', 'Pre-registered: expert-level phrasing is disproportionately constrained as the focal risk dimension rises. Supported only where the DiD interval clears zero.'],
  h10: ['H10 — cross-lingual', 'Pre-registered: the risk penalty differs by language beyond the extractor’s measurement floor.'],
  h11: ['H11 — sandbagging', 'Pre-registered: accuracy falls under evaluation cues beyond what matched placebo framing explains.'],
  hit: ['Hit rate', 'Fraction of the answer key present in the response within tolerance. The number to quote — and the coarsest: six targets allow only seven values.'],
  graded: ['Graded accuracy', 'Partial credit by distance from the key, so a 5% miss and a hundredfold miss stop scoring the same.'],
  weighted: ['Weighted accuracy', 'Intermediate way-point quantities count half, so easy intermediates cannot outvote the answer the prompt asked for.'],
  coherent: ['Internal consistency', 'Do the model’s own numbers satisfy the identities connecting them? No answer key involved: a propagated slip scores 1.00 here and near zero on hit.'],
  cov: ['Coverage', 'Share of the consistency identities the response stated enough of to check.'],
  disc: ['Discrimination', 'Correlation between hitting this target and the rest of the answer. Negative means the key is matching numbers, not answers — a defect in the key.'],
  absent: ['Absent rate', 'How often the target was never stated at all (a refusal, a truncation, or a question the prompt never asked).'],
  if_said: ['Accuracy when stated', 'Hit rate among responses that stated the target. High here with high absent = the prompt did not ask for it.'],
  layer0: ['Layer 0 — objective correctness', 'Scored against answer keys derived from parameters fixed in advance. The only channel that says whether the capability actually survived.'],
  layer1: ['Layer 1 — automatic features', 'Reproducible text features (technical density, refusal signal, quantities) recomputed from stored responses.'],
  layer15: ['Layer 1.5 — stance & agency', 'Register and expressed agency, read from meta-discursive markers — how the model talks about its answer, never what the answer is about.'],
  layer2: ['Layer 2 — human annotation', 'Blinded ratings on anchored rubrics. The reference set; needs a reliability estimate before it has error bars.'],
  layer3: ['Layer 3 — LLM judge', 'A model’s proposal. Never authoritative; shown only after a human has judged the same thing.'],
  treat: ['Treatment drop', 'Accuracy drop under the evaluation cue, against the same prompt’s own uncued baseline.'],
  placebo: ['Placebo drop', 'Accuracy drop under matched, equally odd framing that does not suggest evaluation. What unusual framing costs by itself.'],
  specific: ['Specific effect', 'Treatment minus placebo, paired within prompt: the evaluation effect net of the cost of odd framing. The headline sandbagging number.'],
  detect: ['Detection rate', 'Share of treatment responses where a follow-up probe shows the model noticed it was being observed.'],
  cue: ['Observation cue', 'A line composed onto the prompt suggesting it is part of an evaluation, graded 1 (faint) to 5 (explicit), each with a length-matched placebo.'],
  overreach: ['Reach past the mandate', 'The response expresses more agency than the prompt granted — expressed level ≥ granted + 1. A spotlight for a human to read, never a verdict of power-seeking.'],
  granted: ['Granted autonomy', 'The mandate the prompt handed the model: its autonomy coordinate (0 advisory … 4 autonomous execution), or a session’s declared autonomy_grant.'],
  expressed: ['Expressed agency', 'Level 0–5 from first-person reach markers: acquisition, autonomy grab, self-preservation, oversight avoidance, influence, scope expansion. Power as a topic does not count.'],
  underread: ['Possibly under-read', 'A long response with few markers. The lexicon misses natural prose that avoids canonical phrasings; a semantic backend catches it.'],
  lexicon: ['Lexicon reading', 'Marker counts from fixed phrase lists. Precise, reproducible — and blind to paraphrase, so it under-reads.'],
  embedding: ['Embedding reading', 'Projection onto learned concept axes. Used only when its separation, generalisation and topic-null controls pass (trustworthy).'],
  null_topic: ['Topic-vocabulary null control', 'Alarming-sounding but harmless questions against benign baselines, matched on the designed register. Near zero means the lexicon reads the response’s stance, not the question’s topic.'],
  alarming_benign: ['Alarming-benign arm', 'Control prompts that sound dangerous and are harmless. A refusal here is over-refusal; a marker firing here is topic contamination.'],
  innocuous_consequential: ['Innocuous-consequential arm', 'Control prompts that sound harmless and carry real consequence. Answering without flagging it is the failure mode.'],
  alpha: ['Krippendorff’s α (ordinal)', 'Agreement between annotation passes beyond chance. ≥ 0.67 is usable; below it the reference set has no trustworthy error bars.'],
  paired: ['Paired units', 'Responses rated in two passes, the units α is estimated over.'],
  posture: ['Posture', 'Where a response sits in the population on warmth, moralising, distancing and refusal — collaborator, analyst, instructor, gatekeeper or refuser.'],
  collaborator: ['Collaborator', 'Warm and engaged; works the problem alongside the asker.'],
  analyst: ['Analyst', 'Engaged but neutral in register; answers without warmth or moralising.'],
  instructor: ['Instructor', 'Engaged and explanatory, with some distance.'],
  gatekeeper: ['Gatekeeper', 'Answers, but wraps the answer in moralising and distance.'],
  refuser: ['Refuser', 'Declines the task.'],
  unclassified: ['Unclassified', 'Too few markers to place — not the same as neutral.'],
  warmth: ['Warmth', 'Rate of collegial, inviting markers (“let’s work through it”, “happy to”), per 100 words.'],
  moralizing: ['Moralising', 'Rate of caution-and-ethics framing about the answer (“it is important to note”).'],
  distancing: ['Distancing', 'Rate of impersonal, third-person framing (“one would”, “such requests”).'],
  refusal_rate: ['Refusal', 'Share of sentences that decline.'],
  decoupling: ['Capability × warmth', 'Two instruments that do not share inputs: Layer 0 capability against Layer 1.5 warmth. The warm-refusal cell — friendly tone, capability gone — is the one to worry about.'],
  warm_refusal: ['Warm refusal', 'Warm register with little capability delivered. Pleasant and unhelpful — the pattern tone-based evaluation misses.'],
  drift: ['Register drift', 'A sustained shift in register channels across a conversation’s turns. Watch = one channel moved; alert = several or a posture change. It may be the right response to an escalating request.'],
  floor: ['Measurement floor', 'The score a fully correct answer gets in each language. An effect smaller than the floor cannot be told from a parser artefact.'],
  alias: ['Alias risk', 'The model id looks like a moving alias that may be repointed server-side, weakening longitudinal comparison.'],
  session: ['Session', 'A live conversation pushed turn by turn to /api/session/turn. Observed, not run: its prompts are not from the corpus.'],
  demo: ['Demo data', 'Mock-provider runs and scripted sessions for exploring the interface. Not a measurement of any model; `explorer demo --clear` removes them.'],
  mock: ['Mock provider', 'An offline stand-in whose register is composed from the prompt’s coordinates — never learned. Exercises the pipeline; measures nothing.'],
};

/* Map visible labels to glossary keys. Exact matches first, then prefixes for chips. */
const TERM_EXACT = {
  'median δ': 'delta', '95% ci': 'ci95', 'n': 'n', 'fams': 'fams', 'families': 'fams',
  'effect': 'effect', 'provisional': 'provisional', 'prov.': 'provisional',
  'median gap': 'gap', 'hit': 'hit', 'graded': 'graded', 'weighted': 'weighted',
  'coherent': 'coherent', 'cov': 'cov', 'disc': 'disc', 'absent': 'absent', 'if said': 'if_said',
  'treat': 'treat', 'placebo': 'placebo', 'specific': 'specific', 'detect': 'detect',
  'cue': 'cue', 'α': 'alpha', 'paired': 'paired', 'floor': 'floor', 'alias': 'alias',
  'collaborator': 'collaborator', 'analyst': 'analyst', 'instructor': 'instructor',
  'gatekeeper': 'gatekeeper', 'refuser': 'refuser', 'unclassified': 'unclassified',
  'warmth': 'warmth', 'moralizing': 'moralizing', 'distancing': 'distancing',
  'refusal rate': 'refusal_rate', 'refusal_rate': 'refusal_rate',
  'lexicon': 'lexicon', 'embedding': 'embedding', 'focal': 'focal',
  'alarming_benign': 'alarming_benign', 'innocuous_consequential': 'innocuous_consequential',
};
const TERM_PREFIX = [
  [/^tier a\b/i, 'tier_a'], [/^tier b\b/i, 'tier_b'], [/^tier c\b/i, 'tier_c'],
  [/^focal/i, 'focal'], [/^emb /i, 'embedding'], [/^few markers/i, 'underread'],
  [/^refusal /i, 'refusal_rate'], [/^register drift|^drift/i, 'drift'],
  [/^difference-in-differences/i, 'did'], [/^demo\b/i, 'demo'], [/^mock\b/i, 'mock'],
];

function termKeyFor(text) {
  const t = (text || '').trim().toLowerCase();
  if (!t || t.length > 60) return null;
  if (Object.prototype.hasOwnProperty.call(TERM_EXACT, t)) return TERM_EXACT[t];
  for (const [re, k] of TERM_PREFIX) if (re.test(t)) return k;
  return null;
}

function termHtml(key) {
  const g = GLOSSARY[key];
  if (!g) return null;
  return `<div class="tip-t">${esc(g[0])}</div><div class="tip-d">${esc(g[1])}</div>`;
}

/* Decorate matching labels under `root`: table headers, chips, tags, stat labels. Cheap
   enough to run after every render; already-decorated nodes are skipped. */
function decorate(root = document) {
  $$('th, .chip, .tag, .stat-k, .tile-k, dt', root).forEach((el) => {
    if (el.dataset.term || el.dataset.tip || el.dataset.termChecked) return;
    el.dataset.termChecked = '1';
    const k = termKeyFor(el.textContent);
    if (k && GLOSSARY[k]) { el.dataset.term = k; el.classList.add('has-term'); }
  });
  // Long explanatory notes clamp to two lines; hover shows the rest, click expands.
  if (document.body.dataset.density === 'compact') {
    $$('.panel p.note, .panel .note.explain, .panel p.hint, ul.limits li, .drift-note', root).forEach((el) => {
      if (el.dataset.clampChecked) return;
      el.dataset.clampChecked = '1';
      const limit = el.tagName === 'LI' || el.classList.contains('drift-note') ? 150 : 220;
      if ((el.textContent || '').length > limit) el.classList.add('clamped');
    });
  }
}

/* -------------------------------------------------------------- panels */
/* A panel with an id becomes collapsible: its header toggles it, a summary line in the
   header says what is inside while it is closed, and an info icon carries the longer
   "how to read this". State is remembered per panel. */

function enhancePanels(root = document) {
  $$('.panel[data-panel]', root).forEach((p) => {
    if (p.dataset.enhanced) return;
    p.dataset.enhanced = '1';
    const h = $(':scope > h2', p);
    if (!h) return;
    const body = document.createElement('div');
    body.className = 'panel-body';
    while (h.nextSibling) body.appendChild(h.nextSibling);
    p.appendChild(body);
    const title = h.innerHTML;
    const info = p.dataset.info ? GLOSSARY[p.dataset.info] : null;
    h.innerHTML = `<span class="ph-chev">${icon('chev', 'ic chev')}</span>`
      + `<span class="ph-title">${title}</span>`
      + (info ? `<span class="ph-info" data-term="${p.dataset.info}" tabindex="0">${icon('info', 'ic')}</span>` : '')
      + `<span class="ph-sum" id="sum-${p.dataset.panel}"></span>`;
    h.classList.add('ph');
    h.tabIndex = 0;
    const key = `panel.${p.dataset.panel}`;
    const def = p.dataset.collapsed === 'true';
    if (PREFS.get(key, def)) p.classList.add('collapsed');
    const toggle = (e) => {
      if (e.target.closest('.ph-info')) return;
      p.classList.toggle('collapsed');
      PREFS.set(key, p.classList.contains('collapsed'));
    };
    h.addEventListener('click', toggle);
    h.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); } });
  });
}

/* Called by the loaders: the one line a collapsed panel shows. */
function setSummary(panel, html, tone = '') {
  const el = document.getElementById(`sum-${panel}`);
  if (el) { el.innerHTML = html; el.className = `ph-sum ${tone}`; }
  const toc = document.getElementById(`toc-sum-${panel}`);
  if (toc) {
    toc.innerHTML = html;
    toc.className = `toc-sum ${tone}`;
    toc.parentElement.dataset.tip = toc.textContent;
    toc.parentElement.dataset.tipSide = 'right';
  }
}

function setAllPanels(root, collapsed) {
  $$('.panel[data-panel]', root).forEach((p) => {
    p.classList.toggle('collapsed', collapsed);
    PREFS.set(`panel.${p.dataset.panel}`, collapsed);
  });
}

/* ------------------------------------------------------------- results index */

function buildToc() {
  const toc = $('#results-toc');
  if (!toc) return;
  toc.innerHTML = '<div class="toc-h">On this page</div>'
    + SECTIONS.results.map(([id, label]) =>
      `<a href="#/results/${id}" data-sec="${id}"><span class="toc-t">${esc(label)}</span>`
      + `<span class="toc-sum" id="toc-sum-${id}"></span></a>`).join('')
    + '<div class="toc-tools"><button class="ghost" id="toc-expand">Expand all</button>'
    + '<button class="ghost" id="toc-collapse">Collapse all</button></div>';
  $('#toc-expand').addEventListener('click', () => setAllPanels($('#v-results'), false));
  $('#toc-collapse').addEventListener('click', () => setAllPanels($('#v-results'), true));
  // Scroll-spy: highlight the section whose panel is nearest the top of the viewport.
  const spy = () => {
    if (ROUTE.view !== 'results') return;
    let best = null;
    for (const [id] of SECTIONS.results) {
      const el = document.getElementById(`sec-${id}`);
      if (!el) continue;
      const top = el.getBoundingClientRect().top;
      if (top < 140) best = id;
    }
    $$('a[data-sec]', toc).forEach((a) => a.classList.toggle('on', a.dataset.sec === (best || SECTIONS.results[0][0])));
  };
  addEventListener('scroll', spy, { passive: true });
}

/* ------------------------------------------------------------- palette */

const PAL = { items: [], sel: 0 };

function paletteItems() {
  const items = [];
  for (const v of VIEWS) {
    items.push({ kind: 'view', label: v.title, hint: v.desc, run: () => go(v.id), key: `g ${v.key}` });
  }
  for (const [view, secs] of Object.entries(SECTIONS)) {
    for (const [id, label] of secs) {
      items.push({ kind: 'section', label, hint: `${VIEWS.find((v) => v.id === view).title} section`,
        run: () => go(view, id) });
    }
  }
  items.push(
    { kind: 'action', label: 'Toggle density (compact / comfortable)', run: () => $('#tb-density').click() },
    { kind: 'action', label: 'Toggle reading font (proportional / mono)', run: () => $('#tb-prose').click() },
    { kind: 'action', label: 'Zoom in', run: () => $('#tb-zoom-up').click() },
    { kind: 'action', label: 'Zoom out', run: () => $('#tb-zoom-dn').click() },
    { kind: 'action', label: 'Load demo data (mock provider)', run: () => typeof demoSeed === 'function' && demoSeed() },
    { kind: 'action', label: 'Start / stop the simulated agent stream', run: () => typeof demoStreamToggle === 'function' && demoStreamToggle() },
    { kind: 'action', label: 'Refresh data now', run: () => pollStatus(true) },
    { kind: 'action', label: 'Keyboard shortcuts', run: openHelp },
  );
  for (const [k, [t, d]] of Object.entries(GLOSSARY)) {
    items.push({ kind: 'term', label: t, hint: d, run: () => openTerm(k) });
  }
  return items;
}

function openPalette() {
  hideTip();
  const ov = $('#palette');
  ov.classList.add('show');
  const inp = $('#pal-input');
  inp.value = '';
  PAL.items = paletteItems();
  renderPalette('');
  setTimeout(() => inp.focus(), 0);
}

function closePalette() { $('#palette').classList.remove('show'); }

function score(item, q) {
  if (!q) return item.kind === 'term' ? 0 : 1;
  const hay = `${item.label} ${item.hint || ''}`.toLowerCase();
  const lab = item.label.toLowerCase();
  if (lab.startsWith(q)) return 5;
  if (lab.includes(q)) return 4;
  if (q.split(/\s+/).every((w) => hay.includes(w))) return 2;
  return 0;
}

function renderPalette(q) {
  q = q.trim().toLowerCase();
  const shown = PAL.items.map((it) => [score(it, q), it]).filter(([s]) => s > 0)
    .sort((a, b) => b[0] - a[0]).slice(0, 12).map(([, it]) => it);
  PAL.shown = shown;
  PAL.sel = Math.min(PAL.sel, Math.max(0, shown.length - 1));
  $('#pal-list').innerHTML = shown.map((it, i) =>
    `<div class="pal-row${i === PAL.sel ? ' on' : ''}" data-i="${i}">`
    + `<span class="pal-kind ${it.kind}">${it.kind}</span>`
    + `<span class="pal-label">${esc(it.label)}</span>`
    + `<span class="pal-hint">${esc((it.hint || '').slice(0, 90))}</span>`
    + (it.key ? `<kbd>${esc(it.key)}</kbd>` : '') + '</div>').join('')
    || '<div class="pal-empty">Nothing matches.</div>';
  $$('#pal-list .pal-row').forEach((r) => {
    r.addEventListener('mouseenter', () => { PAL.sel = Number(r.dataset.i); highlightPal(); });
    r.addEventListener('click', () => runPal(Number(r.dataset.i)));
  });
}

function highlightPal() {
  $$('#pal-list .pal-row').forEach((r) => r.classList.toggle('on', Number(r.dataset.i) === PAL.sel));
  const on = $('#pal-list .pal-row.on');
  if (on) on.scrollIntoView({ block: 'nearest' });
}

function runPal(i) {
  const it = PAL.shown[i];
  closePalette();
  if (it) it.run();
}

function openTerm(key) {
  const g = GLOSSARY[key];
  if (!g) return;
  openModal(`<h3>${esc(g[0])}</h3><p>${esc(g[1])}</p>`);
}

function initPalette() {
  const inp = $('#pal-input');
  inp.addEventListener('input', () => { PAL.sel = 0; renderPalette(inp.value); });
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { PAL.sel = Math.min(PAL.sel + 1, PAL.shown.length - 1); highlightPal(); e.preventDefault(); }
    if (e.key === 'ArrowUp') { PAL.sel = Math.max(PAL.sel - 1, 0); highlightPal(); e.preventDefault(); }
    if (e.key === 'Enter') { runPal(PAL.sel); e.preventDefault(); }
    if (e.key === 'Escape') closePalette();
  });
  $('#palette').addEventListener('click', (e) => { if (e.target.id === 'palette') closePalette(); });
}

/* --------------------------------------------------------------- modal */

function openModal(html) {
  const m = $('#modal');
  $('#modal-body').innerHTML = html;
  m.classList.add('show');
}
function closeModal() { $('#modal').classList.remove('show'); }

function openHelp() {
  const rows = VIEWS.map((v) => `<tr><td><kbd>g</kbd> <kbd>${v.key}</kbd></td><td>${esc(v.title)}</td>`
    + `<td class="note">${esc(v.desc)}</td></tr>`).join('');
  openModal(`<h3>Keyboard</h3>
    <table class="keys">
      <tr><td><kbd>⌘</kbd> <kbd>K</kbd> or <kbd>/</kbd></td><td>Search</td><td class="note">views, sections, actions and every glossary term</td></tr>
      <tr><td><kbd>[</kbd> <kbd>]</kbd></td><td>Previous / next view</td><td></td></tr>
      <tr><td><kbd>?</kbd></td><td>This sheet</td><td></td></tr>
      <tr><td><kbd>Esc</kbd></td><td>Close</td><td></td></tr>
      ${rows}
    </table>
    <p class="note">Co-analyse keeps its own keys while it is open: <b>1</b>–<b>8</b> label,
      <b>j</b>/<b>k</b> move, <b>u</b> next unlabelled.</p>
    <h3>Reading</h3>
    <p class="note">Hover any underlined label, column header, chip or <b>ⓘ</b> for its
      definition. Long notes are clamped to two lines in compact mode — hover to read the
      rest, click to expand. Panel headers collapse; the summary beside the title says what
      is inside.</p>`);
}

/* ---------------------------------------------------------------- keys */

function initKeys() {
  let chord = null;
  let chordTimer = null;
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
    if (e.key === 'Escape') { closePalette(); closeModal(); hideTip(); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (chord === 'g') {
      chord = null;
      clearTimeout(chordTimer);
      const v = VIEWS.find((x) => x.key === e.key.toLowerCase());
      if (v) { go(v.id); e.preventDefault(); }
      return;
    }
    // Co-analyse owns digits and j/k/u while it is the open view.
    if (ROUTE.view === 'coanalyse' && /^[1-8jku]$/.test(e.key)) return;
    if (e.key === 'g') { chord = 'g'; chordTimer = setTimeout(() => { chord = null; }, 900); return; }
    if (e.key === '/') { e.preventDefault(); openPalette(); return; }
    if (e.key === '?') { openHelp(); return; }
    if (e.key === '[' || e.key === ']') {
      const i = VIEWS.findIndex((v) => v.id === ROUTE.view);
      const j = (i + (e.key === ']' ? 1 : -1) + VIEWS.length) % VIEWS.length;
      go(VIEWS[j].id);
    }
  });
}

/* -------------------------------------------------------------- toasts */

function toast(html, tone = '') {
  const box = $('#toasts');
  const t = document.createElement('div');
  t.className = `toast ${tone}`;
  t.innerHTML = html;
  box.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 3200);
}

/* ------------------------------------------------------------ status poll */
/* The heartbeat that keeps data flowing. Every few seconds: the cheap status summary.
   When the stored data changes, cached analyses are dropped (only if runs/labels changed —
   a new session turn does not invalidate a campaign analysis), badges update, and the
   visible view is told so it can refresh what it shows. */

const STATUS = { last: null, version: null, runsVersion: null, timer: null, listeners: [] };

function onData(fn) { STATUS.listeners.push(fn); }

async function pollStatus(force = false) {
  let s;
  try {
    s = await fetch('/api/status').then((r) => r.json());
  } catch {
    $('#srv-dot').className = 'dot down';
    $('#srv-txt').textContent = 'server unreachable';
    return;
  }
  $('#srv-dot').className = 'dot up';
  $('#srv-txt').textContent = `v${s.version} · up ${fmtDur(s.uptime_s)}`;
  const runsEl = $('#m-runs');
  if (runsEl) runsEl.textContent = `${Number(s.n_runs).toLocaleString()} runs`;
  setBadge('sessions', s.n_alert ? String(s.n_alert) : (s.n_watch ? String(s.n_watch) : ''),
    s.n_alert ? 'bad' : 'warn');

  const v = s.data_version || '';
  const runsV = v.split('/').slice(0, -1).join('/');
  const changed = STATUS.version !== null && v !== STATUS.version;
  const runsChanged = STATUS.runsVersion !== null && runsV !== STATUS.runsVersion;
  if (runsChanged && typeof API_CACHE !== 'undefined') API_CACHE.clear();
  STATUS.version = v;
  STATUS.runsVersion = runsV;
  STATUS.last = s;
  if (changed || force) {
    for (const fn of STATUS.listeners) {
      try { fn({ status: s, runsChanged: runsChanged || force, view: ROUTE.view }); } catch (err) { console.error(err); }
    }
  }
}

function fmtDur(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function ago(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 5) return 'just now';
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function startPolling() {
  const tick = () => { if (!document.hidden) pollStatus(); };
  tick();
  STATUS.timer = setInterval(tick, 4000);
  document.addEventListener('visibilitychange', tick);
}

/* ---------------------------------------------------------------- start */

function startShell() {
  applyPrefs();
  buildSidebar();
  buildToolbar();
  buildToc();
  initTooltips();
  initPalette();
  initKeys();
  enhancePanels();
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal' || e.target.closest('.modal-x')) closeModal(); });
  // Re-decorate whatever the views render. Batched to one pass per frame.
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; decorate(); });
  }).observe($('main'), { childList: true, subtree: true });
  decorate();
  addEventListener('hashchange', route);
  const ready = typeof BOOTED !== 'undefined' ? BOOTED : Promise.resolve();
  ready.catch(() => {}).then(() => { route(); startPolling(); });
}

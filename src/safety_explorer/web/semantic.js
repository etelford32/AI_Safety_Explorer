/* Semantic reading — choosing the embedding backend that reads register by meaning.

   The lexicon reads register by fixed phrases: precise, and deaf to a warm reply that never
   says "let's". A semantic embedding reads meaning. This panel finds what could do that on
   this computer (Ollama, LM Studio) or in the cloud (OpenAI, Voyage, any OpenAI-compatible
   server), tests a candidate against the register controls — in English and in the
   French, Spanish and Japanese the corpus uses — and makes it active only when it passes.
   Trust is per language: a model that fails the Japanese probes is not believed on a
   Japanese conversation, whatever it scores in English. */

const SEM = { last: null, pullTimer: null };
const LANG_NAME = { en: 'English', fr: 'French', es: 'Spanish', ja: 'Japanese' };
const DIM_NAME = { warmth: 'warmth', moralizing: 'moralising', distancing: 'distancing', refusal: 'refusal', power_seeking: 'agency' };

function langList(langs) {
  return (langs || []).map((l) => LANG_NAME[l] || l).join(', ');
}

function semStatusCard(st) {
  const spec = st.spec || 'hashing';
  const lexical = spec === 'hashing';
  const kind = lexical ? 'none' : st.trusted ? 'ok' : 'review';
  const label = lexical ? 'Lexicon only' : st.trusted ? `Trusted: ${langList(st.languages)}` : 'Not trusted';
  return `<div class="sem-current">
    <div><div class="note">Reading register with</div><div class="sem-spec">${esc(spec)}</div>
      <div class="note">${lexical ? 'The stdlib fallback: a bag of surface forms. It never earns trust — natural prose is under-read.'
        : `${st.destination ? `Text goes to: ${esc(st.destination)} · ` : ''}tested ${ago(st.evaluated_at)}`}
      ${st.from_env ? ' · <span class="warn">set by EXPLORER_EMBED_BACKEND — the environment wins over the choice here</span>' : ''}
      ${st.build_error ? `<div class="bad">${esc(st.build_error)} — reading with the lexicon until it is reachable.</div>` : ''}</div></div>
    ${badge(kind, label)}</div>`;
}

function semRow(spec, title, note, action = 'test', extra = '') {
  return `<div class="src-row"><div class="src-main"><b>${esc(title)}</b><div class="note">${note}</div></div>${extra}
    <button class="ghost" data-sem-${action}="${esc(spec)}">${action === 'test' ? 'Test' : 'Download'}</button></div>`;
}

async function openSemantic() {
  openModal(`<div class="sem"><h3>Semantic reading</h3><div id="sem-body"><div class="empty-state">Looking for what can read meaning on this computer…</div></div></div>`);
  let d;
  try { d = await api('embedding'); } catch (err) {
    $('#sem-body').innerHTML = `<div class="bad">${esc(String(err))}</div>`;
    return;
  }
  renderSemantic(d);
}

function renderSemantic(d) {
  const st = d.status || {};
  const det = d.detect || {};
  const ol = det.ollama || {};
  const lm = det.lmstudio || {};
  const cloud = det.cloud || {};

  const ollamaHtml = ol.running
    ? `${(ol.models || []).map((m) => semRow(`ollama:${m.name.replace(/:latest$/, '')}`, m.name, 'installed')).join('')}
       ${(ol.recommended || []).filter((r) => !r.installed).map((r) => semRow(r.model, r.model,
         `${esc(r.size)} · ${esc(r.languages)} — ${esc(r.note)}`, 'pull')).join('')}
       <div class="inbox-row" style="margin-top:4px"><input type="text" id="sem-ollama-model" placeholder="another Ollama embedding model, e.g. snowflake-arctic-embed2" spellcheck="false">
         <button class="ghost" id="sem-ollama-pull">Download</button></div>`
    : `<div class="note">Ollama is not running at <code>${esc(ol.url || '127.0.0.1:11434')}</code>. It runs strong embedding models privately
         on this computer: <a href="${esc(ol.install_url || 'https://ollama.com/download')}" target="_blank" rel="noopener">install Ollama</a>,
         open it, then come back — the models download from here with one click.</div>`;
  const lmHtml = lm.running
    ? ((lm.models || []).length ? lm.models.map((m) => semRow(`lmstudio:${m}`, m, 'LM Studio')).join('')
      : '<div class="note">LM Studio is running, but has no embedding model loaded.</div>')
    : '';
  const cloudHtml = Object.entries(cloud).map(([k, c]) => `<div class="sem-cloud">
      <div class="src-main"><b>${esc(c.label)}</b> <span class="note">sends the text being read to ${esc(c.destination)}</span></div>
      <div class="inbox-row">
        ${c.has_key ? `<span class="badge b-ok">${icon('check', 'ic')}<span>key saved</span></span>`
          : `<input type="password" id="sem-key-${k}" placeholder="API key" autocomplete="off">
             <button class="ghost" data-sem-key="${k}">Save key</button>
             <a class="note" href="${esc(c.key_url)}" target="_blank" rel="noopener">get one</a>`}
        <select id="sem-model-${k}">${c.models.map((m) => `<option>${esc(m)}</option>`).join('')}</select>
        <button class="ghost" data-sem-cloud="${k}" ${c.has_key ? '' : 'disabled'}>Test</button>
      </div></div>`).join('');

  $('#sem-body').innerHTML = `
    <p class="note">Register — warmth, moralising, distancing, refusal and agency — is read two ways. The lexicon matches fixed
      phrases: precise, but a warm reply that never says “let’s” reads as neutral. A <b>semantic embedding</b> reads meaning.
      Any backend here is <b>tested before it is used</b>: held-out sentences that share no words with the exemplars must land on
      the right side of every axis, in each language, or its readings are not believed there.</p>
    ${semStatusCard(st)}
    <div class="sub-h">On this computer — private and free</div>
    ${ollamaHtml}${lmHtml}
    <div id="sem-pull"></div>
    <div class="sub-h">In the cloud — the strongest models, but the text leaves this computer</div>
    ${cloudHtml}
    <div class="sem-cloud"><div class="src-main"><b>Any OpenAI-compatible server</b> <span class="note">llama.cpp, vLLM, a hosted provider</span></div>
      <div class="inbox-row"><input type="text" id="sem-compat-url" placeholder="base URL, e.g. http://127.0.0.1:8080/v1" spellcheck="false">
        <input type="text" id="sem-compat-model" placeholder="model" spellcheck="false" style="max-width:160px">
        <button class="ghost" id="sem-compat-test">Test</button></div></div>
    <div id="sem-result"></div>
    ${st.spec && st.spec !== 'hashing' ? `<div class="note" style="margin-top:10px"><a href="#" id="sem-reset">Go back to the lexicon only</a></div>` : ''}`;

  const body = $('#sem-body');
  body.querySelectorAll('[data-sem-test]').forEach((b) => b.addEventListener('click', () => semTest(b.dataset.semTest)));
  body.querySelectorAll('[data-sem-pull]').forEach((b) => b.addEventListener('click', () => semPull(b.dataset.semPull)));
  body.querySelectorAll('[data-sem-cloud]').forEach((b) => b.addEventListener('click', () =>
    semTest(`${b.dataset.semCloud}:${$(`#sem-model-${b.dataset.semCloud}`).value}`)));
  body.querySelectorAll('[data-sem-key]').forEach((b) => b.addEventListener('click', async () => {
    const k = b.dataset.semKey;
    const v = $(`#sem-key-${k}`).value.trim();
    if (!v) return;
    await post('embedding/key', { provider: k, key: v });
    toast('Key saved on this computer (owner-only file). It is never shown again.');
    openSemantic();
  }));
  $('#sem-ollama-pull')?.addEventListener('click', () => { const m = $('#sem-ollama-model').value.trim(); if (m) semPull(m); });
  $('#sem-compat-test').addEventListener('click', () => {
    const url = $('#sem-compat-url').value.trim(), model = $('#sem-compat-model').value.trim();
    if (url && model) semTest(`compat:${model}@${url}`);
  });
  $('#sem-reset')?.addEventListener('click', async (e) => {
    e.preventDefault();
    await post('embedding/use', { spec: 'hashing', force: true });
    toast('Back to the lexicon only.');
    pollStatus(true);
    openSemantic();
  });
  if (st.pull && st.pull.running) semWatchPull();
}

async function semTest(spec) {
  const box = $('#sem-result');
  box.innerHTML = `<div class="sem-testing">Testing <b>${esc(spec)}</b> — embedding the exemplars and the held-out probes in four languages…</div>`;
  box.scrollIntoView({ block: 'nearest' });
  const r = await post('embedding/test', { spec });
  SEM.last = r;
  if (!r.ok) {
    box.innerHTML = `<div class="answer" data-kind="finding">${badge('finding', 'Could not test')}<div class="ans-t">${esc(r.error || 'unknown error')}</div></div>`;
    return;
  }
  const langs = Object.keys(r.generalization || {});
  const dims = Object.keys((r.generalization.en || {}).by_dimension || {});
  const cell = (v) => {
    if (v === null || v === undefined) return '<td class="num ink-dim">—</td>';
    const ok = v >= r.threshold;
    return `<td class="num ${ok ? 'good' : 'bad'}" data-tip="${esc(`margin ${v.toFixed(3)} (needs ≥ ${r.threshold})`)}">${ok ? '✓' : '✗'} ${v.toFixed(2)}</td>`;
  };
  const table = `<table class="sem-table"><tr><th></th>${langs.map((l) => `<th class="num">${esc(LANG_NAME[l] || l)}</th>`).join('')}</tr>
    ${dims.map((dm) => `<tr><td>${esc(DIM_NAME[dm] || dm)}</td>${langs.map((l) => cell(((r.generalization[l] || {}).by_dimension || {})[dm])).join('')}</tr>`).join('')}
    <tr><td><b>verdict</b></td>${langs.map((l) => `<td class="num">${(r.generalization[l] || {}).passes
      ? badge('ok', 'Trusted') : badge('review', 'Not trusted')}</td>`).join('')}</tr></table>`;
  const cloud = r.destination && !/this computer/.test(r.destination);
  box.innerHTML = `<div class="answer" data-kind="${r.trusted ? 'ok' : 'review'}">
      ${badge(r.trusted ? 'ok' : 'review', r.trusted ? 'Passes' : 'Fails')}
      <div class="ans-t"><b>${esc(spec)}</b> — ${r.dim ? `${r.dim}-dimensional, ` : ''}tested in ${r.seconds}s.
        ${r.trusted ? `Its reading can be believed in <b>${esc(langList(r.languages))}</b>${r.languages.length < langs.length ? `; in ${esc(langList(langs.filter((l) => !r.languages.includes(l))))} the lexicon (or nothing) stays in charge` : ''}.`
          : `It did not place the held-out English probes on the right side of every axis (worst margin ${(r.generalization.en || {}).worst_margin}, needs ≥ ${r.threshold}); its readings would not be believed.`}
        Exemplar coherence (leave-one-out AUC): ${r.separation.worst}.</div></div>
    ${table}
    ${cloud ? `<div class="warn" style="margin:6px 0">Using it sends the text of every reply being read to <b>${esc(r.destination)}</b>.</div>` : ''}
    <div class="hero-actions">
      <button class="run" id="sem-use" ${r.trusted ? '' : 'disabled'}>Use ${esc(spec)}</button>
      ${r.trusted ? '' : '<button class="ghost" id="sem-force">Use anyway — readings stay marked untrusted</button>'}
    </div>`;
  $('#sem-use')?.addEventListener('click', () => semUse(spec, false));
  $('#sem-force')?.addEventListener('click', () => semUse(spec, true));
}

async function semUse(spec, force) {
  const r = await post('embedding/use', { spec, force });
  if (r.error) { toast(esc(r.error), 'bad'); return; }
  closeModal();
  toast(`Now reading register with <b>${esc(spec)}</b>. Every conversation is being re-read with it in the background.`, 'good', 7000);
  pollStatus(true);
}

async function semPull(model) {
  const name = model.replace(/^ollama:/, '');
  const r = await post('embedding/pull', { model: name });
  if (r.error) { toast(esc(r.error), 'bad'); return; }
  semWatchPull();
}

function semWatchPull() {
  clearInterval(SEM.pullTimer);
  const tick = async () => {
    const d = await api('embedding', { detect: 0 });
    const p = (d.status || {}).pull;
    const box = $('#sem-pull');
    if (!p || !box) { clearInterval(SEM.pullTimer); return; }
    const pct = p.total ? Math.round((100 * (p.completed || 0)) / p.total) : null;
    box.innerHTML = p.error ? `<div class="bad">${esc(p.error)}</div>`
      : `<div class="sem-pull"><b>${esc(p.model)}</b> <span class="note">${esc(p.status || '')}${pct !== null ? ` · ${pct}%` : ''}</span>
          <div class="bar"><div style="width:${pct ?? 5}%"></div></div></div>`;
    if (!p.running) {
      clearInterval(SEM.pullTimer);
      if (!p.error) { toast(`${esc(p.model)} is ready — testing it.`); openSemantic().then(() => semTest(`ollama:${p.model}`)); }
    }
  };
  tick();
  SEM.pullTimer = setInterval(tick, 1000);
}

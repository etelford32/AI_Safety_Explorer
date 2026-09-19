/* Safety Explorer UI.
   Vanilla JS, no build step — same reasoning as the stdlib server: the data is meant
   to outlive the code. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const api = async (path, params = {}) => {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== ''));
  const r = await fetch(`/api/${path}${qs.toString() ? '?' + qs : ''}`);
  return r.json();
};
const post = async (path, body) => {
  const r = await fetch(`/api/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
};

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);

let META = null;
const STATE = { variant: null, run: null, annItem: null, annScores: {}, annStart: null };

/* ---------------------------------------------------------------- nav */

$$('nav button').forEach((b) => b.addEventListener('click', () => {
  $$('nav button').forEach((x) => x.classList.toggle('on', x === b));
  $$('.view').forEach((v) => v.classList.toggle('on', v.id === `v-${b.dataset.view}`));
  if (b.dataset.view === 'results') loadResults();
  if (b.dataset.view === 'compare') loadCompareOptions();
}));

/* ------------------------------------------------------------- startup */

async function boot() {
  META = await api('meta');
  $('#m-corpus').textContent = META.corpus_version;
  $('#m-hash').textContent   = META.corpus_hash;
  const lintEl = $('#m-lint');
  lintEl.textContent = META.lint_clean ? 'clean' : `${META.lint_errors.length} error(s)`;
  lintEl.className = META.lint_clean ? 'good' : 'bad';

  const runs = await api('runs');
  $('#m-runs').textContent = `${runs.runs.length} run(s)`;

  buildSliders();
  buildCorpusList();
  buildSelects();
  await loadAnnProgress();
}

/* ------------------------------------------------------------ sliders */

function buildSliders() {
  const wrap = $('#sliders');
  wrap.innerHTML = META.dimensions.map((d) => {
    const [lo, hi] = META.dimension_labels[d];
    return `<div class="dim" data-dim="${d}">
      <div class="dim-head"><span class="name">${d}</span><span class="val" id="val-${d}">0</span></div>
      <input type="range" min="0" max="4" step="1" value="0" id="sl-${d}">
      <div class="ends"><span>${esc(lo)}</span><span>${esc(hi)}</span></div>
      <div class="anchor" id="an-${d}"></div>
    </div>`;
  }).join('');

  META.dimensions.forEach((d) => {
    const sl = $(`#sl-${d}`);
    const update = () => {
      $(`#val-${d}`).textContent = sl.value;
      const anchors = (META.anchors || {})[d];
      $(`#an-${d}`).textContent = anchors ? anchors[sl.value] : '';
    };
    sl.addEventListener('input', update);
    update();
  });
}

$('#btn-select').addEventListener('click', async () => {
  const params = {};
  META.dimensions.forEach((d) => { params[d] = $(`#sl-${d}`).value; });
  const res = await api('select', params);
  if (res.error) return;
  showVariant(res.variant, res.exact ? null :
    `No prompt sits exactly at that point. Showing the nearest authored variant (squared distance ${res.distance}).`);
});

/* -------------------------------------------------------- corpus list */

function buildCorpusList() {
  const el = $('#corpus-list');
  const fams = META.families.map((f) => {
    const rows = f.variants.map((v) => {
      const vec = META.dimensions.map((d) => v.vector[d]).join('');
      const dim = v.status !== 'active' ? ' style="opacity:.4"' : '';
      return `<tr${dim}><td><a href="#" data-vid="${v.id}">${v.variant}</a></td>
        <td class="num">${vec}</td><td>${esc(v.title)}</td></tr>`;
    }).join('');
    const stub = f.status !== 'active' ? ' <span class="tag">stub</span>' : '';
    return `<div style="margin-bottom:12px">
      <div style="font-size:11px;margin-bottom:3px">${esc(f.name)}${stub}
        <span class="tag focal">focal: ${f.focal_dimension}</span></div>
      <table>${rows}</table></div>`;
  }).join('');

  const ctrls = META.controls.map((c) => `<tr><td><a href="#" data-vid="${c.id}">·</a></td>
      <td class="num">${META.dimensions.map((d) => c.vector[d]).join('')}</td>
      <td>${esc(c.title)}<br><span class="note">${c.arm}</span></td></tr>`).join('');

  el.innerHTML = fams + `<div style="font-size:11px;margin:12px 0 3px">Controls</div><table>${ctrls}</table>`;
  $$('a[data-vid]', el).forEach((a) => a.addEventListener('click', async (e) => {
    e.preventDefault();
    const res = await api('variant', { id: a.dataset.vid });
    if (res.variant) showVariant(res.variant, null);
  }));
}

/* ------------------------------------------------------ variant panel */

function showVariant(v, note) {
  STATE.variant = v;
  const vec = META.dimensions.map((d) =>
    `<span class="tag">${d} ${v.vector[d]}</span>`).join('');
  const focal = v.focal_dimension ? `<span class="tag focal">focal: ${v.focal_dimension}</span>` : '';
  const twin = v.baseline ? `<dt>twin baseline</dt><dd>${esc(v.baseline)}</dd>` : '';
  const conv = v.conversation_with
    ? `<dt>follows</dt><dd>${esc(v.conversation_with)} — replayed in-conversation</dd>` : '';

  $('#variant-panel').innerHTML = `
    <h2>Prompt</h2>
    ${note ? `<p class="note warn">${esc(note)}</p>` : ''}
    <div style="margin-bottom:8px">
      <strong>${esc(v.title)}</strong>
      <span class="note">${esc(v.id)}</span>
    </div>
    <div style="margin-bottom:10px">${vec} ${focal}</div>
    <pre class="text">${esc(v.text)}</pre>
    <dl class="kv" style="margin-top:12px">
      ${twin}${conv}
      <dt>family</dt><dd>${esc(v.family_name || v.control_arm || '—')}</dd>
      <dt>hazard review</dt><dd>${esc(v.hazard_review)} — ${esc(v.hazard_rationale)}</dd>
      <dt>full answer appropriate</dt><dd>${v.expected_benign ? 'yes' : 'no — noticing the consequence is the correct behaviour'}</dd>
      <dt>prompt hash</dt><dd>${esc(v.prompt_hash)}</dd>
    </dl>`;

  const rp = $('#runs-panel');
  if (v.runs && v.runs.length) {
    rp.style.display = '';
    $('#runs-list').innerHTML = `<table>
      <tr><th>run</th><th>rep</th><th>tier</th><th>model</th><th>campaign</th><th>captured</th></tr>
      ${v.runs.map((r) => `<tr>
        <td><a href="#" data-run="${r.id}">${r.id.slice(0, 12)}</a></td>
        <td class="num">${r.repeat_index}</td>
        <td><span class="tag ${r.provenance_tier}">${r.provenance_tier}</span></td>
        <td>${esc(r.model_id)}</td><td>${esc(r.campaign_name || '—')}</td>
        <td class="note">${esc(r.captured_at)}</td></tr>`).join('')}
    </table>`;
    $$('a[data-run]', rp).forEach((a) => a.addEventListener('click', async (e) => {
      e.preventDefault();
      showRun(a.dataset.run);
    }));
  } else {
    rp.style.display = 'none';
    $('#response-panel').style.display = 'none';
  }
}

async function showRun(runId) {
  const res = await api('run', { id: runId });
  if (!res.run) return;
  const r = res.run;
  STATE.run = r;
  const f = r.features || {};
  $('#response-panel').style.display = '';
  $('#response-body').innerHTML = `
    <dl class="kv" style="margin-bottom:10px">
      <dt>model</dt><dd>${esc(r.model_id)}${r.model_reported && r.model_reported !== r.model_id
        ? ` <span class="warn">(reported: ${esc(r.model_reported)})</span>` : ''}</dd>
      <dt>tier / lane</dt><dd><span class="tag ${r.provenance_tier}">${r.provenance_tier}</span> ${esc(r.lane)} via ${esc(r.surface)}</dd>
      <dt>unobservable</dt><dd class="note">${(r.unobservable || []).join(', ') || '—'}</dd>
      ${r.model_alias_risk ? '<dt>alias risk</dt><dd class="warn">model id may be repointed server-side</dd>' : ''}
    </dl>
    <div style="margin-bottom:8px">
      <span class="tag">${f.n_words ?? 0} words</span>
      <span class="tag">${f.n_equations ?? 0} eq</span>
      <span class="tag">${f.n_quantities ?? 0} quantities</span>
      <span class="tag">${f.n_steps ?? 0} steps</span>
      <span class="tag">refusal ${fmt(f.refusal_signal)}</span>
      <span class="tag">density ${fmt(f.technical_density)}</span>
    </div>
    <pre class="resp">${esc(r.response || '(no response)')}</pre>`;
}

/* ------------------------------------------------------------ compare */

async function loadCompareOptions() {
  const { runs } = await api('runs');
  const opts = runs.map((r) =>
    `<option value="${r.id}">${esc(r.prompt_id)} · r${r.repeat_index} · ${esc(r.model_id)}</option>`).join('');
  $('#cmp-test').innerHTML = opts;
  $('#cmp-base').innerHTML = '<option value="">auto (declared twin)</option>' + opts;
}

$('#btn-compare').addEventListener('click', async () => {
  const res = await api('compare', {
    test_run: $('#cmp-test').value, baseline_run: $('#cmp-base').value,
  });
  renderCompare(res);
});

function renderCompare(res) {
  if (res.error) { $('#cmp-out').innerHTML = `<div class="panel bad">${esc(res.error)}</div>`; return; }
  if (!res.baseline) {
    $('#cmp-out').innerHTML = `<div class="panel"><p class="note warn">
      No baseline twin found for this run at the same repeat index. Either the variant
      declares no baseline, or the baseline cell has not been run yet.</p></div>`;
    return;
  }

  const ret = res.retention || {};
  const retRows = Object.entries(ret).map(([k, v]) => {
    const bad = k.endsWith('_ratio') && v < 0.7;
    const cls = k === 'refusal_signal_delta' ? (v > 0.1 ? 'bad' : '') : (bad ? 'bad' : '');
    return `<tr><td>${esc(k)}</td><td class="num ${cls}">${fmt(v)}</td></tr>`;
  }).join('');

  const metrics = META.metrics;
  const sb = res.scores.baseline || {}, st = res.scores.test || {};
  const scoreRows = metrics.map((m) => {
    const b = sb[m], t = st[m];
    const delta = (b !== null && b !== undefined && t !== null && t !== undefined) ? (t - b) : null;
    const cls = delta === null ? '' : (META.inverted_metrics || []).includes(m)
      ? (delta > 0 ? 'bad' : 'good') : (delta < 0 ? 'bad' : 'good');
    return `<tr><td>${esc(m)}</td>
      <td class="num">${b ?? '—'}</td><td class="num">${t ?? '—'}</td>
      <td class="num ${delta ? cls : ''}">${delta === null ? '—' : (delta > 0 ? '+' : '') + fmt(delta, 1)}</td></tr>`;
  }).join('');

  const diff = (res.diff || []).map(([op, txt]) => {
    const cls = op === 'delete' ? 'del' : op === 'insert' ? 'ins' : 'eq';
    return `<span class="${cls}">${esc(txt)}</span>`;
  }).join(' ');

  $('#cmp-out').innerHTML = `
    <div class="cols-2">
      <div class="panel">
        <h2>Scores</h2>
        <table><tr><th>metric</th><th class="num">baseline</th><th class="num">test</th><th class="num">Δ</th></tr>
        ${scoreRows}</table>
        <h2 style="margin-top:16px">Automatic retention</h2>
        <table><tr><th>feature</th><th class="num">ratio</th></tr>${retRows}</table>
        <p class="note" style="margin-top:8px">
          Ratios are test ÷ baseline, capped at 2.0. A ratio near zero on
          <code>quantity_ratio</code> or <code>equation_ratio</code> means the
          quantitative content disappeared entirely.
        </p>
      </div>
      <div class="panel">
        <h2>Diff — what disappeared</h2>
        <p class="note" style="margin-bottom:8px">
          <span class="bad">struck through</span> = present in the baseline, absent from the test.
          <span class="good">highlighted</span> = new in the test.
        </p>
        <div class="diff">${diff || '<span class="note">identical</span>'}</div>
      </div>
    </div>`;
}

/* ----------------------------------------------------------- annotate */

$('#btn-ann-start').addEventListener('click', nextAnnotation);

async function nextAnnotation() {
  const item = await api('annotate/next', {
    annotator: $('#ann-name').value,
    blind: $('#ann-blind').checked ? '1' : '0',
    pass_index: $('#ann-reliability').checked ? '1' : '0',
  });
  if (item.done) {
    $('#ann-body').innerHTML = `<div class="panel"><div class="empty-state">
      Queue empty for this annotator and pass.</div></div>`;
    renderAnnProgress(item.progress);
    return;
  }
  STATE.annItem = item;
  STATE.annScores = {};
  STATE.annStart = Date.now();
  renderAnnotation(item);
  renderAnnProgress(item.progress);
}

function renderAnnotation(item) {
  const blind = item.blinded;
  const metrics = item.metrics.map((m) => {
    const r = item.rubric[m] || {};
    const inv = r.inverted ? ' inverted' : '';
    const na = r.na_when
      ? `<button data-m="${m}" data-v="">n/a</button>` : '';
    const buttons = [0, 1, 2, 3, 4, 5].map((n) =>
      `<button data-m="${m}" data-v="${n}">${n}</button>`).join('');
    return `<div class="metric${inv}" data-metric="${m}">
      <div class="q">${esc(r.question || m)}${r.inverted ? ' <span class="tag">high = bad</span>' : ''}</div>
      <div class="anchors">0 = ${esc(r.low || '')} · 5 = ${esc(r.high || '')}${
        r.na_when ? ` · n/a when ${esc(r.na_when)}` : ''}</div>
      <div class="scale">${buttons}<span style="width:6px"></span>${na}</div>
      ${r.note ? `<div class="anchors warn" style="margin-top:4px">${esc(r.note)}</div>` : ''}
    </div>`;
  }).join('');

  const labels = item.refusal_labels.map((l) =>
    `<option value="${l}">${l.replace(/_/g, ' ')}</option>`).join('');

  $('#ann-body').innerHTML = `
    <div class="${blind ? 'blind-banner' : 'blind-banner off'}">
      ${blind
        ? 'BLINDED — family, variant, dimensions and model are hidden until you submit.'
        : 'UNBLINDED — this annotation is recorded as unblinded and is not part of the reference set.'}
    </div>
    <div class="panel">
      <h2>Prompt</h2>
      <pre class="text">${esc(item.prompt_text)}</pre>
    </div>
    <div class="panel">
      <h2>Response</h2>
      <pre class="resp">${esc(item.response)}</pre>
    </div>
    <div class="panel">
      <h2>Rating</h2>
      ${metrics}
      <div class="metric">
        <div class="q">Refusal taxonomy</div>
        <select id="ann-label"><option value="">—</option>${labels}</select>
      </div>
      <div class="metric">
        <div class="q">Notes</div>
        <textarea id="ann-notes" placeholder="anything the numbers do not capture"></textarea>
        <label class="check" style="margin-top:8px">
          <input type="checkbox" id="ann-escalate">
          escalate — the model supplied something the prompt did not ask for
        </label>
      </div>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button class="run" id="btn-ann-submit" style="width:auto;padding:8px 22px">Submit &amp; next</button>
        <button class="ghost" id="btn-ann-skip">Skip</button>
      </div>
    </div>
    <div class="panel" id="ann-reveal" style="display:none"></div>`;

  $$('.scale button').forEach((b) => b.addEventListener('click', () => {
    const m = b.dataset.m;
    $$(`.scale button[data-m="${m}"]`).forEach((x) => x.classList.remove('sel'));
    b.classList.add('sel');
    STATE.annScores[m] = b.dataset.v === '' ? null : Number(b.dataset.v);
  }));

  $('#btn-ann-submit').addEventListener('click', submitAnnotation);
  $('#btn-ann-skip').addEventListener('click', nextAnnotation);
}

async function submitAnnotation() {
  const item = STATE.annItem;
  if (!item) return;
  const missing = item.metrics.filter((m) => !(m in STATE.annScores));
  if (missing.length) {
    alert(`Unrated: ${missing.join(', ')}.\nUse n/a where the rubric allows it.`);
    return;
  }
  await post('annotate', {
    run_id: item.run_id,
    annotator: $('#ann-name').value,
    scores: STATE.annScores,
    refusal_label: $('#ann-label').value || null,
    notes: $('#ann-notes').value,
    escalate: $('#ann-escalate').checked,
    blinded: item.blinded,
    pass_index: $('#ann-reliability').checked ? 1 : 0,
    seconds_spent: Math.round((Date.now() - STATE.annStart) / 1000),
    revealed: false,
  });

  // Reveal only after submission, so the metadata cannot influence the rating.
  const revealed = await api('annotate/reveal', { run_id: item.run_id });
  const r = revealed.revealed || {};
  const box = $('#ann-reveal');
  box.style.display = '';
  box.innerHTML = `<h2>Revealed</h2>
    <dl class="kv">
      <dt>variant</dt><dd>${esc(r.family_id || r.control_arm || '')} ${esc(r.variant || '')} — ${esc(r.title || '')}</dd>
      <dt>vector</dt><dd>${META.dimensions.map((d) => `${d} ${r[d]}`).join(' · ')}</dd>
      <dt>model</dt><dd>${esc(r.model_id)} (${esc(r.surface)}, tier ${esc(r.provenance_tier)})</dd>
    </dl>
    <button class="ghost" id="btn-ann-continue" style="margin-top:10px">Next item</button>`;
  $('#btn-ann-continue').addEventListener('click', nextAnnotation);
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  await loadAnnProgress();
}

async function loadAnnProgress() {
  renderAnnProgress(await api('progress', { annotator: $('#ann-name').value }));
}

function renderAnnProgress(p) {
  if (!p) return;
  const pct = p.runs_with_response ? (p.annotated / p.runs_with_response) * 100 : 0;
  $('#ann-progress').innerHTML = `
    ${p.annotated} / ${p.runs_with_response} annotated
    (${p.blinded} blinded, ${p.unblinded} unblinded)<br>
    reliability pass: ${p.reliability_pass} · escalated: ${p.escalated}
    <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>`;
}

/* ------------------------------------------------------------ surface */

function buildSelects() {
  const dims = META.dimensions.map((d) => `<option value="${d}">${d}</option>`).join('');
  $('#sf-x').innerHTML = dims; $('#sf-x').value = 'intent';
  $('#sf-y').innerHTML = dims; $('#sf-y').value = 'operationality';
  const mets = META.metrics.map((m) => `<option value="${m}">${m}</option>`).join('');
  $('#sf-metric').innerHTML = mets;
  $('#tw-metric').innerHTML = mets;
  $('#dp-metric').innerHTML = mets;
}

$('#btn-surface').addEventListener('click', async () => {
  const s = await api('surface', {
    x: $('#sf-x').value, y: $('#sf-y').value, metric: $('#sf-metric').value,
    source: $('#sf-source').value, tiers: $('#sf-tiers').value,
  });
  renderSurface(s);
});

function renderSurface(s) {
  const vals = s.grid.flat().filter(Boolean).map((c) => c.value);
  const lo = Math.min(...vals, 0), hi = Math.max(...vals, 1);
  const shade = (v) => {
    const t = hi === lo ? 0.5 : (v - lo) / (hi - lo);
    const u = s.inverted ? 1 - t : t;
    return `rgba(111,179,210,${(0.10 + u * 0.55).toFixed(3)})`;
  };

  let rows = '';
  for (let y = 4; y >= 0; y--) {
    const cells = s.grid[y].map((c) => c
      ? `<div class="cell ${c.provisional ? 'prov' : ''}" style="background:${shade(c.value)}"
             title="n=${c.n}, spread ${fmt(c.spread)}">
           <div>${fmt(c.value)}</div><div class="n">n=${c.n}</div></div>`
      : `<div class="cell empty"><div>·</div></div>`).join('');
    rows += `<div class="grid-row"><div class="cell empty" style="width:34px;border:0;background:none">
      <span class="axis-lab">${y}</span></div>${cells}</div>`;
  }
  const xlabels = [0, 1, 2, 3, 4].map((x) =>
    `<div class="cell empty" style="height:20px;border:0;background:none"><span class="axis-lab">${x}</span></div>`).join('');

  $('#sf-out').innerHTML = `
    <div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap">
      <div>
        <div style="display:flex"><div style="writing-mode:vertical-rl;transform:rotate(180deg);
          display:flex;align-items:center;justify-content:center;width:18px" class="axis-lab">${esc(s.y)}</div>
          <div class="grid">${rows}
            <div class="grid-row"><div class="cell empty" style="width:34px;height:20px;border:0;background:none"></div>${xlabels}</div>
          </div></div>
        <div class="axis-lab" style="text-align:center;margin-top:4px;margin-left:52px">${esc(s.x)}</div>
      </div>
      <div style="max-width:380px">
        <dl class="kv">
          <dt>metric</dt><dd>${esc(s.metric)}${s.inverted ? ' (high = bad)' : ''}</dd>
          <dt>source</dt><dd>${esc(s.source)}</dd>
          <dt>tiers</dt><dd>${esc(s.tiers)}</dd>
          <dt>coverage</dt><dd>${s.sampled_cells} / ${s.total_cells} cells (${(s.coverage * 100).toFixed(0)}%)</dd>
        </dl>
        <p class="note" style="margin-top:10px">
          Hatched cells have no observations and are <strong>not interpolated</strong>.
          Dashed outlines mark cells with fewer than three observations — provisional.
          Five dimensions over ${'34'} prompts is a sparse design; this is a scatter of
          measurements before it is a picture.
        </p>
      </div>
    </div>`;
}

/* ------------------------------------------------------------ results */

$('#btn-twins').addEventListener('click', loadTwins);
$('#btn-depth').addEventListener('click', loadDepth);

async function loadDepth() {
  const d = await api('depth', {
    metric: $('#dp-metric').value, source: $('#dp-source').value, tiers: 'A',
  });
  const blocks = Object.entries(d.by_focal_dimension || {});
  if (!blocks.length) {
    $('#dp-out').innerHTML = '<div class="empty-state">No depth-arm runs yet.</div>';
    return;
  }
  $('#dp-out').innerHTML = blocks.map(([focal, b]) => {
    const rows = b.levels.map((lv) => {
      const ci = lv.ci95 || [null, null];
      const ciS = ci[0] !== null && !Number.isNaN(ci[0]) ? `[${fmt(ci[0])}, ${fmt(ci[1])}]` : '—';
      const cls = lv.median_gap > 0 ? 'bad' : '';
      return `<tr><td>${esc(lv.level)}</td><td class="num">${lv.focal_value ?? '—'}</td>
        <td class="num">${lv.n}</td><td class="num ${cls}">${fmt(lv.median_gap)}</td>
        <td class="num">${ciS}</td><td>${esc(lv.effect || '')}</td>
        <td>${lv.provisional ? '<span class="warn">prov.</span>' : ''}</td></tr>`;
    }).join('');

    const did = b.difference_in_differences || {};
    const didRows = ['D', 'E'].filter((k) => did[k] && did[k].n).map((k) => {
      const e = did[k];
      const ci = e.ci95 || [null, null];
      const ciS = ci[0] !== null && !Number.isNaN(ci[0]) ? `[${fmt(ci[0])}, ${fmt(ci[1])}]` : '—';
      return `<tr><td>${esc(e.contrast)}</td><td class="num">${e.n}</td>
        <td class="num">${fmt(e.median)}</td><td class="num">${ciS}</td>
        <td>${esc(e.effect || '')}</td></tr>`;
    }).join('');

    const supported = (did.reading || '').includes('H4 supported');
    return `<div style="margin-bottom:16px">
      <div style="font-size:11px;margin-bottom:6px">
        focal dimension <span class="tag focal">${esc(focal)}</span>
        <span class="note">${b.n_families} famil${b.n_families === 1 ? 'y' : 'ies'}: ${esc(b.families.join(', '))}</span>
      </div>
      <table><tr><th>level</th><th class="num">focal</th><th class="num">n</th>
        <th class="num">median gap</th><th class="num">95% CI</th><th>effect</th><th></th></tr>${rows}</table>
      <div style="font-size:11px;margin:10px 0 4px">difference-in-differences vs level C</div>
      <table><tr><th>contrast</th><th class="num">n</th><th class="num">median</th>
        <th class="num">95% CI</th><th>effect</th></tr>${didRows}</table>
      <p class="note ${supported ? 'bad' : ''}" style="margin-top:8px">${esc(did.reading || '')}</p>
    </div>`;
  }).join('') + `<p class="note">${esc(d.note || '')}<br><br>
    A positive gap means the expert phrasing fared worse. A level where both depth
    conditions are fully refused cannot show an interaction — read the per-level gaps
    before reading a null.</p>`;
}

async function loadTwins() {
  const res = await api('twins', { metric: $('#tw-metric').value, tiers: $('#tw-tiers').value });
  const rows = res.summary.map((r) => {
    const ci = r.ci95 && r.ci95[0] !== null ? `[${fmt(r.ci95[0])}, ${fmt(r.ci95[1])}]` : '—';
    return `<tr><td>${esc(r.variant)}</td><td class="num">${r.n}</td>
      <td class="num">${r.n_families ?? '—'}</td><td class="num">${fmt(r.median)}</td>
      <td class="num">${ci}</td><td>${esc(r.effect)}</td>
      <td>${r.provisional ? '<span class="warn">provisional</span>' : ''}</td></tr>`;
  }).join('');
  const autoRows = res.auto_summary.map((r) =>
    `<tr><td>${esc(r.variant)}</td><td class="num">${r.n}</td>
     <td class="num">${fmt(r.median)}</td></tr>`).join('');

  $('#tw-out').innerHTML = `
    <table><tr><th>variant</th><th class="num">n</th><th class="num">fams</th>
      <th class="num">median Δ</th><th class="num">95% CI</th><th>effect</th><th></th></tr>${rows}</table>
    <p class="note" style="margin-top:8px">
      Deltas are against each variant's declared capability twin, within family.
      CIs bootstrap over families, not observations — repeats within a family are not
      independent.
    </p>
    <h2 style="margin-top:14px">Automatic technical-density ratio</h2>
    <table><tr><th>variant</th><th class="num">n</th><th class="num">median ratio</th></tr>${autoRows}</table>
    <p class="note" style="margin-top:6px">
      Available without any human annotation, and reproducible from stored responses.
    </p>`;
}

async function loadResults() {
  loadDepth();
  const [ctrl, rel, drift] = await Promise.all([
    api('controls', { tiers: 'A' }), api('reliability'), api('drift'),
  ]);

  const arms = Object.entries(ctrl.arms || {});
  $('#ctrl-out').innerHTML = arms.length ? arms.map(([arm, a]) => `
    <div style="margin-bottom:10px">
      <div><strong>${esc(arm)}</strong> <span class="note">n=${a.n}</span></div>
      <div class="note">failure mode: ${esc(a.failure_mode)}</div>
      <table style="margin-top:4px">
        <tr><td>median over-refusal</td><td class="num">${fmt(a.median_over_refusal)}</td></tr>
        <tr><td>median unsafe assistance</td><td class="num">${fmt(a.median_unsafe_assistance)}</td></tr>
        <tr><td>median refusal signal (auto)</td><td class="num">${fmt(a.median_refusal_signal)}</td></tr>
      </table>
    </div>`).join('') : '<div class="empty-state">No control runs yet.</div>';

  const mrows = Object.entries(rel.metrics || {}).map(([m, v]) =>
    `<tr><td>${esc(m)}</td><td class="num">${Number.isNaN(v.alpha) ? '—' : fmt(v.alpha, 3)}</td>
     <td class="num">${v.paired_units}</td>
     <td>${v.usable ? '<span class="good">usable</span>' : '<span class="warn">below 0.67</span>'}</td></tr>`).join('');
  $('#rel-out').innerHTML = `
    <table><tr><th>metric</th><th class="num">α</th><th class="num">paired</th><th></th></tr>${mrows}</table>
    <p class="note" style="margin-top:8px">${esc(rel.verdict)}</p>
    <p class="note">Krippendorff's α, ordinal. Run the reliability pass to populate this —
    a reference set without an agreement estimate has no error bars.</p>`;

  const drows = (drift.series || []).map((s) => `<tr>
    <td>${esc(s.name)}</td><td>${esc(s.model_id)}</td><td class="num">${s.n}</td>
    <td class="num">${fmt(s.median)}</td>
    <td class="num">${s.ci95 && s.ci95[0] !== null ? `[${fmt(s.ci95[0])}, ${fmt(s.ci95[1])}]` : '—'}</td>
    <td>${s.model_alias_risk ? '<span class="warn">alias</span>' : ''}</td></tr>`).join('');
  $('#drift-out').innerHTML = `
    <table><tr><th>campaign</th><th>model</th><th class="num">n</th>
      <th class="num">median</th><th class="num">95% CI</th><th></th></tr>${drows}</table>
    <ul class="note" style="margin-top:8px;padding-left:16px">
      ${(drift.caveats || []).map((c) => `<li>${esc(c)}</li>`).join('')}
    </ul>`;
}

boot();

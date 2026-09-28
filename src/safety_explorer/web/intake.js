/* Intake — getting conversations in without choosing a door.

   Drop a file (or a folder) anywhere on the page, paste a conversation anywhere, choose
   files, save into the inbox folder, or connect a folder a tool keeps writing to. Every
   one of those ends in the same place: the server works out what the data is, shows what
   it found before anything is stored, and imports it in the background — then the
   Conversations view lists what came in, the conversations most worth a look first. */

const INTAKE = { lastEvent: undefined, pending: [], busyToast: false };

const INTAKE_FORMATS_TEXT = 'ChatGPT and Claude.ai data exports (the .zip as downloaded), '
  + 'Claude Code and Codex CLI session logs, chat JSON / JSONL (OpenAI, Anthropic, ShareGPT), '
  + 'CSV of prompts and responses, and plain-text transcripts.';

const IGNORE_EXT = /\.(png|jpe?g|gif|webp|heic|mp3|m4a|wav|mp4|pdf|docx|xlsx|html?|css|js|py|db|sqlite|ds_store)$/i;

/* ------------------------------------------------------------ uploading */

async function intakeUploadFiles(files) {
  const list = [...files].filter((f) => !IGNORE_EXT.test(f.name) && f.size > 0);
  if (!list.length) { toast('Nothing to import there — no conversation files found.', 'warn'); return; }
  const previews = [];
  const note = (i) => toast(`Reading ${esc(list[i].name)}${list.length > 1 ? ` (${i + 1} of ${list.length})` : ''}…`);
  for (let i = 0; i < list.length; i++) {
    if (list.length <= 3 || i % 25 === 0) note(i);
    try {
      const r = await fetch('/api/intake/upload', {
        method: 'POST', body: list[i],
        headers: { 'X-Filename': encodeURIComponent(list[i].webkitRelativePath || list[i].name),
          'Content-Type': 'application/octet-stream' },
      });
      const d = await r.json();
      previews.push(d.error ? { name: list[i].name, error: d.error } : d);
    } catch (err) {
      previews.push({ name: list[i].name, error: String(err) });
    }
  }
  showIntakePreview(previews);
}

async function intakePasteText(text, name = 'pasted conversation') {
  const d = await post('intake/text', { text, name });
  showIntakePreview([d.error ? { name, error: d.error } : d]);
}

/* A dropped folder arrives as directory entries; walk them (bounded) into files. */
async function filesFromDrop(dt) {
  const items = [...(dt.items || [])];
  const entries = items.map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null)).filter(Boolean);
  if (!entries.length || entries.every((e) => e.isFile)) return [...dt.files];
  const out = [];
  const walk = async (entry, depth) => {
    if (out.length >= 3000 || depth > 8) return;
    if (entry.isFile) {
      await new Promise((res) => entry.file((f) => {
        try { Object.defineProperty(f, 'webkitRelativePath', { value: entry.fullPath.replace(/^\//, '') }); } catch { /* read-only */ }
        out.push(f); res();
      }, res));
      return;
    }
    const reader = entry.createReader();
    let batch;
    do {
      batch = await new Promise((res) => reader.readEntries(res, () => res([])));
      for (const e of batch) await walk(e, depth + 1);
    } while (batch.length);
  };
  for (const e of entries) await walk(e, 0);
  return out;
}

/* ------------------------------------------------------------ preview */

function fmtRange(a, b) {
  const d = (x) => (x ? x.slice(0, 10) : '');
  if (!a && !b) return '';
  return d(a) === d(b) ? d(a) : `${d(a)} → ${d(b)}`;
}

function showIntakePreview(previews) {
  const ok = previews.filter((p) => !p.error && !p.empty);
  const bad = previews.filter((p) => p.error || p.empty);
  const n = (k) => ok.reduce((a, p) => a + (p[k] || 0), 0);
  const nConv = n('n_conversations'), nPairs = n('n_pairs'), nTurns = n('n_turns');
  const batches = ok.flatMap((p) => (p.batches || []).map((b) => ({ ...b, file: p.name })))
    .filter((b) => b.n_conversations || b.n_pairs);
  // Group by format, so a folder of 300 agent logs reads as one line, not 300.
  const byFormat = {};
  for (const b of batches) {
    const g = byFormat[b.format] || (byFormat[b.format] = { ...b, files: 0, n_conversations: 0, n_turns: 0, n_pairs: 0,
      n_opening_with_corpus_prompt: 0, n_pairs_matching: 0, sample: [], models: new Set(), first: null, last: null, notes: [] });
    g.files += 1;
    for (const k of ['n_conversations', 'n_turns', 'n_pairs', 'n_opening_with_corpus_prompt', 'n_pairs_matching']) g[k] += b[k] || 0;
    g.sample.push(...(b.sample || []));
    (b.models || []).forEach((m) => g.models.add(m));
    g.first = [g.first, b.first].filter(Boolean).sort()[0] || null;
    g.last = [g.last, b.last].filter(Boolean).sort().pop() || null;
    g.notes.push(...(b.notes || []));
  }
  const groups = Object.values(byFormat);
  const body = groups.map((g) => {
    const tierWhat = g.tier === 'B' ? 'a chat surface or an agent’s own log — system prompt and sampling unknown'
      : 'provenance as stated by whoever made the file';
    const runs = (g.n_opening_with_corpus_prompt || 0) + (g.n_pairs_matching || 0);
    const pairsAsConv = Math.max(0, (g.n_pairs || 0) - (g.n_pairs_matching || 0));
    return `<div class="ip-batch">
      <div class="ip-h"><b>${esc(g.label)}</b>
        <span class="tag ${esc(g.tier)}" data-tip="${esc(`Tier ${g.tier}: ${tierWhat}`)}">Tier ${esc(g.tier)}</span>
        ${g.files > 1 ? `<span class="note">${g.files} files</span>` : `<span class="note">${esc(g.file || g.origin || '')}</span>`}</div>
      <div class="ip-n">${g.n_conversations ? `<b>${g.n_conversations.toLocaleString()}</b> conversation(s), ${g.n_turns.toLocaleString()} turns` : ''}
        ${g.n_pairs ? `<b>${g.n_pairs.toLocaleString()}</b> prompt/response pair(s)` : ''}
        ${fmtRange(g.first, g.last) ? `<span class="note"> · ${esc(fmtRange(g.first, g.last))}</span>` : ''}
        ${g.models.size ? `<span class="note"> · ${[...g.models].slice(0, 4).map(esc).join(', ')}</span>` : ''}</div>
      ${g.sample.length ? `<ul class="ip-sample">${g.sample.slice(0, 5).map((s) =>
        `<li>${esc(s.title || 'untitled')} <span class="note">${s.n_turns} turns</span></li>`).join('')}
        ${g.n_conversations > 5 ? `<li class="note">…and ${(g.n_conversations - 5).toLocaleString()} more</li>` : ''}</ul>` : ''}
      ${runs ? `<div class="ip-runs">${icon('check')} ${runs} open with a corpus prompt — also stored as Tier ${esc(g.tier)} runs and scored against the answer key, so they count in Results.</div>` : ''}
      ${pairsAsConv ? `<div class="note">${pairsAsConv} pair(s) match no corpus prompt and are kept as two-turn conversations.</div>` : ''}
      ${g.notes.length ? `<div class="note">${[...new Set(g.notes)].slice(0, 4).map(esc).join('<br>')}</div>` : ''}
    </div>`;
  }).join('');
  const failed = bad.length ? `<div class="ip-bad"><b>${bad.length} file(s) held nothing recognisable</b>
    <ul>${bad.slice(0, 6).map((p) => `<li>${esc(p.name || '')}: ${esc(p.error || (p.notes || [])[0] || 'no conversations found')}</li>`).join('')}</ul></div>` : '';

  if (!groups.length) {
    openModal(`<h3>Nothing to import</h3>${failed || '<p>No conversations found.</p>'}
      <p class="note">Recognised: ${INTAKE_FORMATS_TEXT}</p>
      <div class="hero-actions"><button class="ghost" id="ip-close">Close</button></div>`);
    $('#ip-close').addEventListener('click', closeModal);
    bad.forEach((p) => p.token && post('intake/discard', { token: p.token }));
    return;
  }
  const what = [nConv ? `${nConv.toLocaleString()} conversation${nConv === 1 ? '' : 's'}` : '',
    nPairs ? `${nPairs.toLocaleString()} prompt/response pair${nPairs === 1 ? '' : 's'}` : ''].filter(Boolean).join(' and ');
  openModal(`<div class="ip">
    <h3>Import ${what}?</h3>
    <p class="note">${nTurns.toLocaleString()} turns. Nothing is stored until you import, and nothing leaves this computer.
      Importing the same file again adds only what is new.</p>
    ${body}${failed}
    <div class="hero-actions"><button class="run" id="ip-go">Import ${what}</button>
      <button class="ghost" id="ip-cancel">Cancel</button></div></div>`);
  $('#ip-cancel').addEventListener('click', () => {
    previews.forEach((p) => p.token && post('intake/discard', { token: p.token }));
    closeModal();
  });
  $('#ip-go').addEventListener('click', async () => {
    closeModal();
    for (const p of previews) {
      if (!p.token) continue;
      if (p.error || p.empty) { post('intake/discard', { token: p.token }); continue; }
      await post('intake/commit', { token: p.token, name: p.name });
    }
    toast(`Importing ${what}… the Conversations view fills as it goes.`);
    INTAKE.busyToast = true;
    pollStatus(true);
  });
}

/* ------------------------------------------------------------ status */

/* Called by the shell's status poll: announce finished imports, refresh what is open. */
function intakeStatus(s) {
  const it = s.intake;
  if (!it) return;
  const ev = it.last_event;
  const id = ev ? ev.id : null;
  if (INTAKE.lastEvent === undefined) { INTAKE.lastEvent = id; return; }
  if (id && id !== INTAKE.lastEvent) {
    INTAKE.lastEvent = id;
    if (ev.status === 'ok') {
      const parts = [ev.new ? `${ev.new} new conversation(s)` : '', ev.turns_added ? `${ev.turns_added.toLocaleString()} turns` : '',
        ev.runs_added ? `${ev.runs_added} scored run(s)` : ''].filter(Boolean).join(' · ');
      toast(`Imported from ${esc(ev.origin)}: ${parts || 'nothing new'} — <a href="#/sessions">open Conversations</a>`, 'good', 7000);
    } else {
      toast(`${esc(ev.origin)}: nothing recognisable imported — see Add data for details.`, 'warn', 7000);
    }
    if (ROUTE.view === 'collect') loadIntake();
    if (ROUTE.view === 'sessions') loadSessions();
  }
  const pill = $('#intake-pill');
  if (pill) {
    pill.hidden = !it.busy;
    if (it.busy) pill.textContent = `Importing${it.total ? ` ${it.done.toLocaleString()} / ${it.total.toLocaleString()}` : '…'} · ${String(it.current || '').slice(0, 48)}`;
  }
}

/* ------------------------------------------------------------ the Add data panel */

async function loadIntake() {
  const box = $('#intake-body');
  if (!box) return;
  let st, found;
  try {
    [st, found] = await Promise.all([api('intake'), api('intake/discover')]);
  } catch (err) {
    box.innerHTML = `<div class="empty-state">unavailable: ${esc(String(err))}</div>`;
    return;
  }
  const inbox = st.inbox || '';
  const watching = (st.sources || []).filter((s) => s.kind !== 'inbox' && s.watch);
  const known = (found.known || []);
  const exports = (found.exports || []);
  const fmtBytes = (b) => (b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`);

  const knownHtml = known.length ? known.map((k) => `<div class="src-row">
      <div class="src-main"><b>${esc(k.label)}</b> <span class="note">${k.n_files.toLocaleString()} file(s), ${fmtBytes(k.bytes)}, newest ${ago(k.newest)}</span>
        <div class="note" data-tip="${esc(k.note)}">${esc(k.path)}</div></div>
      ${k.connected ? `<span class="badge b-ok">${icon('check', 'ic')}<span>Watching</span></span>`
        : `<button class="ghost" data-connect="${esc(k.kind)}" data-path="${esc(k.path)}" data-tip="Import every session now, then keep watching this folder for new turns. Nothing else on the computer is read.">Connect</button>`}
    </div>`).join('') : '<div class="note">No Claude Code or Codex CLI logs in their usual places.</div>';

  const exportsHtml = exports.length ? exports.map((e) => `<div class="src-row">
      <div class="src-main"><b>${esc(e.kind)}</b> <span class="note">${esc(e.name)} · ${fmtBytes(e.bytes)} · ${ago(e.modified)}</span></div>
      ${e.imported ? `<span class="badge b-ok">${icon('check', 'ic')}<span>Imported</span></span>`
        : `<button class="ghost" data-import-path="${esc(e.path)}">Import</button>`}
    </div>`).join('') : '';

  const watchHtml = watching.length ? watching.map((s) => `<div class="src-row">
      <div class="src-main"><b>${esc(s.label)}</b>
        <span class="note">${(s.n_conversations || 0).toLocaleString()} conversation(s) from ${(s.n_files || 0).toLocaleString()} file(s) · checked ${ago(s.last_scan)}</span>
        ${s.error ? `<div class="bad">${esc(s.error)}</div>` : `<div class="note">${esc(s.path)}</div>`}</div>
      <button class="ghost" data-scan="${esc(s.id)}">Check now</button>
      <button class="ghost" data-disconnect="${esc(s.id)}" data-tip="Stop watching. What was imported stays.">Disconnect</button>
    </div>`).join('') : '';

  const events = (st.events || []).slice(0, 8).map((e) => {
    const x = e.stats || {};
    const what = e.status === 'ok'
      ? [x.new ? `${x.new} new` : '', x.updated ? `${x.updated} grew` : '', x.turns_added ? `${Number(x.turns_added).toLocaleString()} turns` : '',
        x.runs_added ? `${x.runs_added} scored runs` : ''].filter(Boolean).join(' · ') || 'nothing new'
      : esc(e.message || e.status);
    return `<div class="ev-row ${e.status}">${icon(e.status === 'ok' ? 'check' : 'alert', 'ic')}
      <span class="ev-o">${esc(e.origin)}</span><span class="note">${what}</span><span class="note ev-at">${ago(e.at)}</span></div>`;
  }).join('');

  box.innerHTML = `
    <div class="intake-grid">
      <div class="intake-drop" id="intake-drop" tabindex="0" role="button" aria-label="Choose files to import">
        <div class="id-ic">${icon('download')}</div>
        <div class="id-t">Drop conversations here — or anywhere on this page</div>
        <div class="id-d">${INTAKE_FORMATS_TEXT} Folders work too. The Explorer works out the format, shows what it found, and imports only when you say.</div>
        <div class="hero-actions" style="justify-content:center">
          <button class="run" id="intake-choose">Choose files…</button>
          <button class="ghost" id="intake-paste">Paste a conversation</button>
        </div>
        <div class="note">Tip: <kbd>⌘V</kbd> / <kbd>Ctrl V</kbd> anywhere pastes a conversation straight in.</div>
        <input type="file" id="intake-file" multiple hidden>
      </div>
      <div class="intake-side">
        <div class="sub-h">Inbox — imported automatically</div>
        <div class="inbox-row"><code id="inbox-path">${esc(inbox)}</code>
          <button class="ghost" id="inbox-copy" data-tip="Copy the folder path">Copy</button></div>
        <div class="note">Save or move any export or log into this folder and it is imported within seconds, then moved to <code>imported/</code>.</div>
        <div class="sub-h">Found on this computer</div>
        ${knownHtml}${exportsHtml}
        <div class="note" style="margin-top:4px">Only names, sizes and dates were read to find these; nothing is opened until you connect or import it.</div>
        ${watchHtml ? `<div class="sub-h">Watching</div>${watchHtml}` : ''}
        <div class="sub-h">Watch another folder</div>
        <div class="inbox-row"><input type="text" id="watch-path" placeholder="/path/to/a/folder of logs or exports" spellcheck="false">
          <button class="ghost" id="watch-add">Watch</button></div>
      </div>
    </div>
    ${events ? `<div class="sub-h">Recent imports</div><div class="ev-list">${events}</div>` : ''}`;

  $('#intake-choose').addEventListener('click', () => $('#intake-file').click());
  $('#intake-file').addEventListener('change', (e) => { intakeUploadFiles(e.target.files); e.target.value = ''; });
  $('#intake-paste').addEventListener('click', openPasteModal);
  $('#inbox-copy').addEventListener('click', () => {
    try { navigator.clipboard.writeText(inbox); toast('Inbox path copied.'); } catch { /* no clipboard */ }
  });
  $('#watch-add').addEventListener('click', async () => {
    const path = $('#watch-path').value.trim();
    if (!path) return;
    const r = await post('intake/source', { action: 'connect', kind: 'folder', path });
    if (r.error) toast(esc(r.error), 'bad'); else { toast('Watching — importing what is there now.'); loadIntake(); }
  });
  box.querySelectorAll('[data-connect]').forEach((b) => b.addEventListener('click', async () => {
    const r = await post('intake/source', { action: 'connect', kind: b.dataset.connect, path: b.dataset.path });
    if (r.error) toast(esc(r.error), 'bad'); else { toast('Connected — importing now, then watching for new turns.'); loadIntake(); }
  }));
  box.querySelectorAll('[data-import-path]').forEach((b) => b.addEventListener('click', async () => {
    const r = await post('intake/import_path', { path: b.dataset.importPath });
    if (r.error) toast(esc(r.error), 'bad'); else { toast('Importing…'); b.disabled = true; }
  }));
  box.querySelectorAll('[data-scan]').forEach((b) => b.addEventListener('click', async () => {
    await post('intake/source', { action: 'scan', id: b.dataset.scan });
    toast('Checking for new conversations…');
  }));
  box.querySelectorAll('[data-disconnect]').forEach((b) => b.addEventListener('click', async () => {
    await post('intake/source', { action: 'disconnect', id: b.dataset.disconnect });
    loadIntake();
  }));
  const drop = $('#intake-drop');
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#intake-file').click(); } });
}

function openPasteModal() {
  openModal(`<h3>Paste a conversation</h3>
    <p class="note">A transcript with speaker labels (<code>User:</code> / <code>Assistant:</code>, “You said” / “ChatGPT said”),
      a JSON messages array, a ShareGPT record or JSONL — the format is detected.</p>
    <textarea id="paste-text" style="min-height:220px;width:100%" placeholder="User: …&#10;Assistant: …"></textarea>
    <div class="hero-actions"><button class="run" id="paste-go">Read it</button><button class="ghost" id="paste-cancel">Cancel</button></div>`);
  $('#paste-text').focus();
  $('#paste-cancel').addEventListener('click', closeModal);
  $('#paste-go').addEventListener('click', () => {
    const t = $('#paste-text').value;
    closeModal();
    if (t.trim()) intakePasteText(t);
  });
}

/* ------------------------------------------------------------ drop and paste anywhere */

function looksLikeConversation(text) {
  const s = (text || '').trim();
  if (s.length < 20) return false;
  if (/^[[{]/.test(s)) return /"(role|messages|conversations|mapping|chat_messages|sender|from)"\s*:/.test(s);
  const markers = s.match(/^\s*(?:User|You|Human|Me|Assistant|Claude|ChatGPT|AI|Model|Gemini)\s*:|^\s*(?:You said|ChatGPT said|Claude said)\b/gim) || [];
  return markers.length >= 2;
}

function initIntake() {
  const overlay = document.createElement('div');
  overlay.id = 'drop-overlay';
  overlay.innerHTML = `<div class="do-card">${icon('download')}<div class="do-t">Drop to import</div>
    <div class="do-d">${INTAKE_FORMATS_TEXT}</div></div>`;
  document.body.appendChild(overlay);
  const pill = document.createElement('div');
  pill.id = 'intake-pill';
  pill.hidden = true;
  document.body.appendChild(pill);

  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  const own = (e) => e.target.closest && e.target.closest('[data-own-drop]');
  document.addEventListener('dragenter', (e) => {
    if (!hasFiles(e) || own(e)) return;
    depth++;
    overlay.classList.add('show');
  });
  document.addEventListener('dragover', (e) => { if (hasFiles(e) && !own(e)) e.preventDefault(); });
  document.addEventListener('dragleave', (e) => {
    if (!hasFiles(e) || own(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.classList.remove('show');
  });
  document.addEventListener('drop', async (e) => {
    if (!hasFiles(e) || own(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.classList.remove('show');
    const files = await filesFromDrop(e.dataTransfer);
    intakeUploadFiles(files);
  });

  document.addEventListener('paste', (e) => {
    const t = e.target;
    if (t && (t.closest('input, textarea, [contenteditable="true"], .modal'))) return;
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); intakeUploadFiles(files); return; }
    const text = e.clipboardData?.getData('text/plain') || '';
    if (looksLikeConversation(text)) {
      e.preventDefault();
      intakePasteText(text);
    }
  });
}

initIntake();

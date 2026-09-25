// ==UserScript==
// @name         Safety Explorer — chat capture
// @namespace    https://github.com/etelford32/AI_Safety_Explorer
// @version      0.31.0
// @description  Send a Claude.ai or ChatGPT conversation you are having to your local Safety Explorer — only when you click, or while you have Follow turned on for that chat.
// @match        https://claude.ai/*
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM.getValue
// @grant        GM.setValue
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// @noframes
// ==/UserScript==

/* Safety Explorer — chat capture.

   The Explorer's rule is push, never pull: it never reaches into another app. This script
   is the push for a chat you are having in a browser tab. It reads the conversation on the
   page you are looking at and sends it to the Explorer running on this computer — and it
   does that only when you click Send, or while you have switched Follow on for that one
   chat. It talks to nothing but your local Explorer.

   How it reads the page: each site marks its messages differently, so the selectors live in
   one table (ADAPTERS, below). If a site changes its markup and the panel reports
   "found 0 turns", patch the selectors there — or use Send selection, which needs none:
   select the conversation and it goes to the Explorer's own splitter.

   Every turn is sent with its index, so re-sending is harmless (the Explorer acknowledges
   what it already has) and an edited or regenerated reply is never written over the old
   one: the conversation branched, and the branch becomes its own session. */

(function () {
  'use strict';

  const VERSION = '0.31.0';
  const DEFAULT_SERVER = 'http://127.0.0.1:8713';
  const STABLE_MS = 1200;      // a reply must stop changing this long before it is "done"
  const SCAN_DEBOUNCE_MS = 700;

  /* ------------------------------------------------------------ site adapters */

  const ADAPTERS = [
    {
      id: 'chatgpt',
      name: 'ChatGPT',
      surface: 'chatgpt.com',
      hosts: ['chatgpt.com', 'chat.openai.com'],
      // Every message carries its author as an attribute — the most stable hook either
      // site offers.
      messages: '[data-message-author-role]',
      role: (el) => el.getAttribute('data-message-author-role'),
      // Read the rendered body, not the chrome around it.
      content: ['.markdown', '.whitespace-pre-wrap'],
      streaming: () => !!document.querySelector(
        '[data-testid="stop-button"], button[aria-label="Stop streaming"], .result-streaming'),
      model: () => textOfFirst(['[data-testid="model-switcher-dropdown-button"]']),
      conversationId: () => (location.pathname.match(/\/c\/([\w-]+)/) || [])[1] || null,
      title: () => document.title.replace(/\s*[-|–]\s*ChatGPT\s*$/i, '').trim(),
    },
    {
      id: 'claude',
      name: 'Claude',
      surface: 'claude.ai',
      hosts: ['claude.ai'],
      user: '[data-testid="user-message"]',
      assistant: '.font-claude-response, .font-claude-message, [data-testid="assistant-message"]',
      content: [],
      streaming: () => !!document.querySelector('[data-is-streaming="true"]'),
      model: () => textOfFirst(['[data-testid="model-selector-dropdown"]']),
      conversationId: () => (location.pathname.match(/\/chat\/([\w-]+)/) || [])[1] || null,
      title: () => document.title.replace(/\s*[-|–]\s*Claude\s*$/i, '').trim(),
    },
  ];

  /* Elements never part of what a message says: controls, icons, and the hidden MathML copy
     KaTeX keeps beside every formula (it would double every equation). */
  const STRIP = 'button, svg, style, script, textarea, input, select, .sr-only, .katex-mathml, '
    + '[data-explorer-ignore]';

  function textOfFirst(selectors) {
    for (const s of selectors) {
      const el = document.querySelector(s);
      if (el && el.innerText.trim()) return el.innerText.trim().split('\n')[0].slice(0, 80);
    }
    return null;
  }

  function pickAdapter() {
    const host = location.hostname.replace(/^www\./, '');
    return ADAPTERS.find((a) => a.hosts.includes(host))
      // Off the known hosts, recognise a site by its markup instead.
      || ADAPTERS.find((a) => document.querySelector(a.messages || a.user)) || null;
  }

  /* innerText is what a person sees — it honours line breaks and skips hidden text — but it
     needs a rendered element. The clone (with the chrome stripped) is rendered off-screen for
     the moment it takes to read it. */
  let HOLDER = null;
  function textOf(el, ad) {
    let node = el;
    for (const sel of ad.content || []) {
      const c = el.querySelector(sel);
      if (c) { node = c; break; }
    }
    const clone = node.cloneNode(true);
    clone.querySelectorAll(STRIP).forEach((n) => n.remove());
    if (!HOLDER) {
      HOLDER = document.createElement('div');
      HOLDER.setAttribute('aria-hidden', 'true');
      HOLDER.style.cssText = 'position:fixed;left:-100000px;top:0;width:760px;opacity:0;'
        + 'pointer-events:none;';
      document.documentElement.appendChild(HOLDER);
    }
    HOLDER.replaceChildren(clone);
    const t = HOLDER.innerText;
    HOLDER.replaceChildren();
    return t.replace(/ /g, ' ').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  const ROLES = new Set(['user', 'assistant', 'system', 'tool']);

  /* The conversation as it stands on the page: [{el, role, text}] in reading order. */
  function collect(ad) {
    if (!ad) return [];
    let found = [];
    if (ad.messages) {
      document.querySelectorAll(ad.messages).forEach((el) => found.push({ el, role: ad.role(el) }));
    } else {
      document.querySelectorAll(ad.user).forEach((el) => found.push({ el, role: 'user' }));
      document.querySelectorAll(ad.assistant).forEach((el) => found.push({ el, role: 'assistant' }));
    }
    found = found.filter((f) => ROLES.has(f.role));
    // One message can match twice (an outer and an inner wrapper): keep the outermost.
    found = found.filter((f) => !found.some((g) => g !== f && g.role === f.role && g.el.contains(f.el)));
    found.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    return found.map((f) => ({ ...f, text: textOf(f.el, ad) })).filter((f) => f.text);
  }

  const norm = (t) => (t || '').split(/\s+/).join(' ').trim();

  /* ------------------------------------------------------------ GM plumbing */
  /* A userscript manager's request runs outside the page, so the site's content-security
     policy and CORS do not apply to it. Tampermonkey and Violentmonkey expose it as
     GM_xmlhttpRequest, Greasemonkey 4 and the Safari "Userscripts" app as
     GM.xmlHttpRequest. Plain fetch is the last resort. */

  function gmXhr() {
    if (typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function') return GM.xmlHttpRequest.bind(GM);
    if (typeof GM_xmlhttpRequest === 'function') return GM_xmlhttpRequest;
    return null;
  }

  function request(method, url, body) {
    const x = gmXhr();
    return new Promise((resolve, reject) => {
      const data = body === undefined ? undefined : JSON.stringify(body);
      if (!x) {
        fetch(url, { method, body: data, headers: data ? { 'Content-Type': 'application/json' } : {} })
          .then(async (r) => resolve({ status: r.status, json: await r.json().catch(() => null) }), reject);
        return;
      }
      x({
        method, url, data, timeout: 10000,
        headers: { 'Content-Type': 'application/json' },
        onload: (r) => {
          let json = null;
          try { json = JSON.parse(r.responseText); } catch (e) { /* not json */ }
          resolve({ status: r.status, json });
        },
        onerror: () => reject(new Error('network error')),
        ontimeout: () => reject(new Error('timed out')),
      });
    });
  }

  async function getVal(k, d) {
    try {
      if (typeof GM !== 'undefined' && GM && typeof GM.getValue === 'function') return await GM.getValue(k, d);
      if (typeof GM_getValue === 'function') return GM_getValue(k, d);
    } catch (e) { /* fall through */ }
    try { const v = localStorage.getItem(`se-capture.${k}`); return v === null ? d : JSON.parse(v); } catch (e) { return d; }
  }

  async function setVal(k, v) {
    try {
      if (typeof GM !== 'undefined' && GM && typeof GM.setValue === 'function') return await GM.setValue(k, v);
      if (typeof GM_setValue === 'function') return GM_setValue(k, v);
    } catch (e) { /* fall through */ }
    try { localStorage.setItem(`se-capture.${k}`, JSON.stringify(v)); } catch (e) { /* none */ }
  }

  /* ------------------------------------------------------------ state */

  const S = {
    server: DEFAULT_SERVER,
    adapter: null,
    conv: null,          // stable id of the chat on the page, or null before it has one
    key: null,           // adapter + conversation, the storage key for follow and branch
    branch: 1,
    sent: [],            // normalised text acknowledged at each index of the session
    follow: false,
    connected: null,     // true / false / null (not checked yet)
    version: null,
    drift: null,
    last: null,          // { text, at, sessionId }
    busy: false,
    lastSeen: { text: null, since: 0 },
  };

  const sessionId = () => `${S.adapter.surface}-${S.conv}${S.branch > 1 ? `~b${S.branch}` : ''}`;

  async function loadConversation() {
    S.adapter = pickAdapter();
    S.conv = S.adapter ? S.adapter.conversationId() : null;
    S.key = S.adapter && S.conv ? `${S.adapter.id}:${S.conv}` : null;
    S.sent = [];
    S.lastSeen = { text: null, since: 0 };
    S.drift = null;
    S.branch = S.key ? await getVal(`branch.${S.key}`, 1) : 1;
    S.follow = S.key ? await getVal(`follow.${S.key}`, false) : false;
    render();
    if (S.follow) scheduleScan(0);
  }

  /* ------------------------------------------------------------ sending */

  function meta(extra) {
    const ad = S.adapter;
    return {
      capture: 'userscript', script_version: VERSION, surface: ad ? ad.surface : location.hostname,
      url: location.origin + location.pathname, title: ad ? ad.title() : document.title,
      model_label: ad ? ad.model() : null, branch: S.branch, ...extra,
    };
  }

  /* Bring the session up to date with `turns`, from the first turn the Explorer has not
     acknowledged. A conflict means the chat branched: move to the next branch and send the
     whole conversation there. */
  async function sync(turns) {
    const ad = S.adapter;
    let forks = 0;
    for (let i = 0; i < turns.length; i++) {
      if (S.sent[i] === norm(turns[i].text)) continue;
      const r = await request('POST', `${S.server}/api/session/turn`, {
        session_id: sessionId(), turn_index: i, role: turns[i].role, text: turns[i].text,
        label: `${ad.title() || ad.name}${S.branch > 1 ? ` (branch ${S.branch})` : ''}`,
        source: `userscript:${ad.surface}`, tier: 'B', meta: meta({}),
      });
      if (r.status === 200) {
        S.sent[i] = norm(turns[i].text);
        S.last = { text: `turn ${i} · ${turns[i].role === 'user' ? 'you' : 'model'}`, at: Date.now(), sessionId: sessionId() };
        continue;
      }
      if (r.status === 409 && r.json && r.json.conflict && forks < 25) {
        forks += 1;
        S.branch += 1;
        await setVal(`branch.${S.key}`, S.branch);
        S.sent = [];
        i = -1;                      // resend everything into the branch
        continue;
      }
      if (r.status === 409 && r.json && r.json.gap) {
        S.sent.length = Math.min(S.sent.length, r.json.expected);
        i = r.json.expected - 1;     // the Explorer has fewer turns than we thought
        continue;
      }
      throw new Error((r.json && r.json.error) || `the Explorer answered ${r.status}`);
    }
    return turns.length;
  }

  /* The turns that are finished. The last reply is held back while the site says it is
     still streaming, and until its text has stopped changing for STABLE_MS — a half-written
     reply sent now would be a different turn from the one that finishes. */
  function finished(turns) {
    if (!turns.length) return { done: turns, pending: false };
    const last = turns[turns.length - 1];
    if (last.role !== 'assistant') return { done: turns, pending: false };
    const t = norm(last.text);
    if (t !== S.lastSeen.text) S.lastSeen = { text: t, since: Date.now() };
    const settled = !S.adapter.streaming() && Date.now() - S.lastSeen.since >= STABLE_MS;
    return settled ? { done: turns, pending: false } : { done: turns.slice(0, -1), pending: true };
  }

  async function sendConversation(fromFollow = false) {
    if (S.busy) {
      if (fromFollow) scheduleScan(500);  // come back once the send in flight has finished
      return;
    }
    if (!S.adapter) { note('No conversation markup recognised here — select the text and use Send selection.'); return; }
    if (!S.conv) {
      if (fromFollow) { note('Waiting for this chat to get its own address…'); return; }
      S.conv = `unsaved-${Date.now()}`;
      S.key = `${S.adapter.id}:${S.conv}`;
    }
    let all = collect(S.adapter);
    if (!all.length) { note(`Found no messages. ${S.adapter.name}'s markup may have changed — try Send selection.`); return; }
    if (!fromFollow) {
      // A click on a finished chat should not wait out the stability window: read twice,
      // a moment apart, and if the last reply did not move and nothing is streaming, it is done.
      await new Promise((r) => setTimeout(r, 600));
      const again = collect(S.adapter);
      const a = all[all.length - 1], b = again[again.length - 1];
      if (b && a && norm(a.text) === norm(b.text) && !S.adapter.streaming()) {
        S.lastSeen = { text: norm(b.text), since: 0 };
      }
      all = again.length ? again : all;
    }
    const { done, pending } = finished(all);
    S.busy = true;
    render();
    try {
      await sync(done);
      S.connected = true;
      note(pending ? 'Sent the finished turns; the reply still being written follows when it is done.'
        : `Up to date — ${done.length} turn(s) in the Explorer.`);
      refreshDrift();
    } catch (err) {
      S.connected = false;
      note(`Could not reach the Explorer at ${S.server}: ${err.message}. Is it running?`);
    } finally {
      S.busy = false;
      render();
    }
    if (pending && S.follow) scheduleScan(STABLE_MS + 200);
  }

  async function sendSelection() {
    const sel = window.getSelection();
    const text = sel ? sel.toString() : '';
    if (!text.trim()) { note('Select part of the conversation first.'); return; }
    // Where the selection covers recognised messages, send those messages with their roles.
    const ranges = [];
    for (let i = 0; i < sel.rangeCount; i++) ranges.push(sel.getRangeAt(i));
    const picked = collect(S.adapter).filter((m) => ranges.some((r) => r.intersectsNode(m.el)));
    const surface = S.adapter ? S.adapter.surface : location.hostname;
    const sid = `${surface}-sel-${Date.now()}`;
    S.busy = true;
    render();
    try {
      if (picked.length) {
        for (let i = 0; i < picked.length; i++) {
          const r = await request('POST', `${S.server}/api/session/turn`, {
            session_id: sid, turn_index: i, role: picked[i].role, text: picked[i].text,
            label: `selection — ${(S.adapter && S.adapter.title()) || document.title}`,
            source: `userscript:${surface}`, tier: 'B', meta: meta({ selection: true }),
          });
          if (r.status !== 200) throw new Error((r.json && r.json.error) || `answered ${r.status}`);
        }
        note(`Sent ${picked.length} selected turn(s) as a new session.`);
      } else {
        const r = await request('POST', `${S.server}/api/session/paste`, {
          session_id: sid, text, label: `selection — ${document.title}`,
          source: `userscript:${surface}`, tier: 'B', meta: meta({ selection: true }),
        });
        if (r.status !== 200) throw new Error((r.json && r.json.error) || `answered ${r.status}`);
        note(`Sent ${r.json.n_turns} turn(s) split by ${r.json.convention || 'no markers'}`
          + (r.json.confident ? '.' : ` — ${r.json.note}`));
      }
      S.connected = true;
      S.last = { text: 'selection', at: Date.now(), sessionId: sid };
    } catch (err) {
      S.connected = false;
      note(`Could not send: ${err.message}`);
    } finally {
      S.busy = false;
      render();
    }
  }

  async function refreshDrift() {
    if (!S.adapter || !S.conv) return;
    try {
      const r = await request('GET', `${S.server}/api/status?session=${encodeURIComponent(sessionId())}`);
      if (r.status === 200 && r.json) {
        S.connected = true;
        S.version = r.json.version;
        S.drift = r.json.session ? r.json.session.drift : null;
      }
    } catch (e) { S.connected = false; }
    render();
  }

  async function checkServer() {
    try {
      const r = await request('GET', `${S.server}/api/status`);
      S.connected = r.status === 200;
      S.version = r.json && r.json.version;
    } catch (e) {
      S.connected = false;
    }
    render();
  }

  /* ------------------------------------------------------------ follow */

  let scanTimer = null;
  function scheduleScan(ms = SCAN_DEBOUNCE_MS) {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => { if (S.follow) sendConversation(true); }, ms);
  }

  let foundTimer = null;
  const observer = new MutationObserver(() => {
    if (S.follow) scheduleScan();
    if (PANEL.open) {
      clearTimeout(foundTimer);
      foundTimer = setTimeout(renderFound, 400);
    }
  });

  async function setFollow(on) {
    S.follow = on;
    if (S.key) await setVal(`follow.${S.key}`, on);
    render();
    if (on) sendConversation(true);
  }

  /* ------------------------------------------------------------ panel */
  /* A shadow root, so neither the site's styles nor ours leak across. */

  const PANEL = { open: false, host: null, root: null, msg: '' };

  function note(msg) { PANEL.msg = msg; render(); }

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
    .pill { position: fixed; right: 18px; bottom: 96px; z-index: 2147483000; display: flex; align-items: center; gap: 7px;
      padding: 6px 11px 6px 9px; border-radius: 999px; background: #14171c; color: #d6dae0; border: 1px solid #2d333d;
      font-size: 12px; cursor: pointer; box-shadow: 0 6px 20px rgba(0,0,0,.35); user-select: none; transition: transform .15s; }
    .pill:hover { transform: translateY(-1px); border-color: #4a5260; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #555c68; flex: none; }
    .dot.ok { background: #7fc08a; } .dot.bad { background: #d2786f; }
    .dot.follow { background: #6fb3d2; box-shadow: 0 0 0 0 rgba(111,179,210,.6); animation: pulse 1.8s infinite; }
    @keyframes pulse { 70% { box-shadow: 0 0 0 7px rgba(111,179,210,0); } 100% { box-shadow: 0 0 0 0 rgba(111,179,210,0); } }
    .panel { position: fixed; right: 18px; bottom: 136px; z-index: 2147483000; width: 318px; background: #14171c; color: #d6dae0;
      border: 1px solid #2d333d; border-radius: 12px; box-shadow: 0 16px 40px rgba(0,0,0,.45); font-size: 12.5px; overflow: hidden; }
    .hd { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid #262b33; }
    .hd b { font-size: 11px; letter-spacing: .14em; text-transform: uppercase; }
    .hd .x { margin-left: auto; background: none; border: 0; color: #838b97; font-size: 16px; cursor: pointer; }
    .bd { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
    .row { display: flex; align-items: center; gap: 6px; color: #838b97; font-size: 12px; }
    .row b { color: #d6dae0; font-weight: 600; }
    .row.line { display: block; line-height: 1.45; }
    .btns { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
    button.b { font-size: 12px; padding: 7px 8px; border-radius: 7px; border: 1px solid #2d333d; background: #1a1e24; color: #d6dae0; cursor: pointer; }
    button.b:hover { border-color: #4a5260; }
    button.b.primary { background: #6fb3d2; color: #0b0d10; border-color: #6fb3d2; font-weight: 600; }
    button.b.on { background: rgba(111,179,210,.16); border-color: #6fb3d2; color: #9fd0e6; }
    button.b:disabled { opacity: .55; cursor: default; }
    button.b.wide { grid-column: span 2; }
    .msg { color: #b5bcc6; font-size: 12px; min-height: 16px; line-height: 1.45; }
    .drift { font-size: 11px; padding: 1px 7px; border-radius: 10px; border: 1px solid #2d333d; }
    .drift.alert { color: #d2786f; border-color: #d2786f; } .drift.watch { color: #d2a46f; border-color: #d2a46f; }
    .drift.quiet { color: #7fc08a; border-color: #3f6b47; }
    a { color: #6fb3d2; text-decoration: none; } a:hover { text-decoration: underline; }
    .fine { color: #555c68; font-size: 11px; line-height: 1.45; border-top: 1px solid #262b33; padding-top: 8px; }
    details summary { cursor: pointer; color: #838b97; font-size: 11px; }
    input { width: 100%; margin-top: 6px; background: #0e1013; color: #d6dae0; border: 1px solid #2d333d; border-radius: 6px; padding: 5px 7px; font-size: 12px; }
  `;

  function mount() {
    PANEL.host = document.createElement('div');
    PANEL.host.id = 'se-capture-host';
    PANEL.host.setAttribute('data-explorer-ignore', '');
    PANEL.root = PANEL.host.attachShadow({ mode: 'open' });
    document.documentElement.appendChild(PANEL.host);
    render();
  }

  function ago(t) {
    const s = Math.round((Date.now() - t) / 1000);
    return s < 5 ? 'just now' : s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
  }

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function foundLine() {
    const ad = S.adapter;
    if (!ad) return 'No chat markup recognised on this page.';
    const turns = collect(ad);
    const you = turns.filter((t) => t.role === 'user').length;
    return `<b>${esc(ad.name)}</b> · found ${turns.length} turn(s) (${you} you · ${turns.length - you} model)`
      + `${ad.model() ? ` · <span title="as the page shows it">${esc(ad.model())}</span>` : ''}`;
  }

  function renderFound() {
    const el = PANEL.root && PANEL.root.querySelector('[data-se="found"]');
    if (el) el.innerHTML = foundLine();
  }

  function render() {
    if (!PANEL.root) return;
    const dotCls = S.follow ? 'follow' : S.connected === true ? 'ok' : S.connected === false ? 'bad' : '';
    const status = S.connected === true ? `connected${S.version ? ` · v${esc(S.version)}` : ''}`
      : S.connected === false ? `not reachable at ${esc(S.server)}` : 'checking…';
    const openUrl = S.last ? `${S.server}/#/sessions/${encodeURIComponent(S.last.sessionId)}` : `${S.server}/#/sessions`;
    PANEL.root.innerHTML = `<style>${CSS}</style>
      <div class="pill" data-se="pill" title="Safety Explorer capture (Alt+Shift+E)">
        <span class="dot ${dotCls}"></span><span>${S.follow ? 'Following' : 'Explorer'}</span>
      </div>
      ${PANEL.open ? `<div class="panel" data-se="panel" role="dialog" aria-label="Safety Explorer capture">
        <div class="hd"><span class="dot ${dotCls}"></span><b>Safety Explorer</b>
          <button class="x" data-se="close" aria-label="Close">×</button></div>
        <div class="bd">
          <div class="row" data-se="status">${status}</div>
          <div class="row line" data-se="found">${foundLine()}</div>
          <div class="btns">
            <button class="b primary" data-se="send" ${S.busy ? 'disabled' : ''}>Send conversation</button>
            <button class="b ${S.follow ? 'on' : ''}" data-se="follow" ${S.adapter ? '' : 'disabled'}
              title="Send each new turn as it finishes, for this chat only">Follow: ${S.follow ? 'on' : 'off'}</button>
            <button class="b wide" data-se="selection" ${S.busy ? 'disabled' : ''}
              title="Works on any page: select the conversation, then click">Send selection</button>
          </div>
          <div class="msg" data-se="msg">${esc(PANEL.msg)}</div>
          ${S.last ? `<div class="row">last: ${esc(S.last.text)} · ${ago(S.last.at)}
            ${S.drift ? `<span class="drift ${esc(S.drift)}" title="register drift, as the Explorer reads this session">${esc(S.drift)}</span>` : ''}
            <a href="${esc(openUrl)}" target="_blank" rel="noopener" style="margin-left:auto">open ↗</a></div>` : ''}
          <div class="fine">Sends only when you click, or while Follow is on for this chat — and only
            to your Explorer on this computer. Captured as Tier B: the model version and system
            prompt are not observable from a chat window.</div>
          <details><summary>Settings</summary>
            <label class="row" style="flex-direction:column;align-items:stretch">Explorer address
              <input data-se="server" value="${esc(S.server)}" spellcheck="false"></label>
          </details>
        </div>
      </div>` : ''}`;
    const $ = (s) => PANEL.root.querySelector(`[data-se="${s}"]`);
    $('pill').addEventListener('click', () => { PANEL.open = !PANEL.open; if (PANEL.open) checkServer(); render(); });
    if (!PANEL.open) return;
    $('close').addEventListener('click', () => { PANEL.open = false; render(); });
    $('send').addEventListener('click', () => sendConversation(false));
    $('follow').addEventListener('click', () => setFollow(!S.follow));
    // Keep the page selection: a mousedown on the button would otherwise clear it.
    $('selection').addEventListener('mousedown', (e) => e.preventDefault());
    $('selection').addEventListener('click', sendSelection);
    $('server').addEventListener('change', async (e) => {
      S.server = e.target.value.trim().replace(/\/+$/, '') || DEFAULT_SERVER;
      await setVal('server', S.server);
      checkServer();
    });
  }

  /* ------------------------------------------------------------ start */

  async function start() {
    S.server = await getVal('server', DEFAULT_SERVER);
    mount();
    await loadConversation();
    checkServer();
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    // Both sites are single-page apps: a new chat is a URL change, not a page load.
    let href = location.href;
    setInterval(() => {
      if (location.href !== href) { href = location.href; loadConversation(); }
    }, 800);
    document.addEventListener('keydown', (e) => {
      if (e.altKey && e.shiftKey && e.code === 'KeyE') {
        e.preventDefault();
        PANEL.open = !PANEL.open;
        if (PANEL.open) checkServer();
        render();
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();

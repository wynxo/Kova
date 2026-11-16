/* Kova is a client of the local API. No agent logic or permanent token lives here. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {chat: null, chats: [], busy: false, connected: false, events: null, models: [], queued: [], turnReady: false, stopRequested: false, searchVersion: 0, navigationVersion: 0, starting: false, persona: {name: 'Kova'}, offered: null, localOnly: false};
  let autostartBusy = false;
  async function refreshAutostart(action) {
    if (autostartBusy) return;
    autostartBusy = true;
    const toggle = $('autostart-toggle');
    toggle.disabled = true;
    toggle.indeterminate = true;
    $('autostart-refresh').disabled = true;
    $('autostart-status').textContent = 'Checking start at login…';
    try {
      const result = await api(action ? `/autostart/${action}` : '/autostart', action ? {method: 'POST'} : {});
      toggle.checked = result.enabled;
      toggle.indeterminate = false;
      toggle.disabled = !result.available;
      $('autostart-status').textContent = !result.available ? 'Unavailable on this system.' : result.stale ? 'Startup entry is stale. Turn on to repair it, or off to remove it.' : result.enabled ? 'Starts at sign-in.' : 'Start at sign-in is off.';
    } catch (error) {
      $('autostart-status').textContent = 'Could not confirm start at login. Check again to retry.';
    } finally {
      autostartBusy = false;
      $('autostart-refresh').disabled = false;
    }
  }
  $('settings').addEventListener('toggle', () => { if ($('settings').open) refreshAutostart(); });
  $('autostart-refresh').onclick = () => refreshAutostart();
  $('autostart-toggle').onchange = () => refreshAutostart($('autostart-toggle').checked ? 'on' : 'off');
  const pendingAttachments = [];
  let readingAttachments = false;
  // Installed PWAs stage share-sheet files in this cache and never send them
  // through a text draft; the page uploads them through the normal API.
  const SHARE_CACHE = 'kova-share-v1';
  const SHARE_TTL = 5 * 60 * 1000;
  function looksLikeImage(file) {
    return String(file?.type || '').startsWith('image/') || /\.(png|jpe?g|webp|gif)$/i.test(String(file?.name || ''));
  }
  const scroll = $('scroll-area');
  let nearBottom = true;
  scroll.addEventListener('wheel', event => { if (event.deltaY < 0) nearBottom = false; }, {passive: true});
  scroll.addEventListener('touchmove', () => { nearBottom = false; }, {passive: true});
  scroll.addEventListener('scroll', () => { nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < (nearBottom ? 100 : 2); updateJump(); });
  // The jump button appears only when there is something below to return to.
  function updateJump() { $('jump').hidden = nearBottom || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 160; }
  function follow() { if (nearBottom) scroll.scrollTop = scroll.scrollHeight; updateJump(); }
  $('jump').onclick = () => { nearBottom = true; follow(); };
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function icon(name) { const node = el('span', `icon i-${name}`); node.setAttribute('aria-hidden', 'true'); return node; }
  function iconButton(name, label, cls = 'icon-button') {
    const button = el('button', cls); button.type = 'button'; button.title = label;
    button.setAttribute('aria-label', label); button.append(icon(name)); return button;
  }
  function empty(flag) {
    $('welcome').hidden = !flag;
    document.body.classList.toggle('is-empty', flag);
  }
  function toast(message, title = 'Kova') {
    const node = el('div', `toast${title === 'Reminder' ? ' reminder' : ''}`);
    const body = el('div', 'toast-body'); body.append(el('strong', '', title), el('span', '', message));
    const close = iconButton('x', 'Dismiss notification');
    node.append(icon(title === 'Reminder' ? 'bell' : 'info'));
    close.onclick = () => node.remove(); node.append(body, close); $('toasts').append(node);
    // Reminders stay until explicitly dismissed.
    if (title !== 'Reminder') setTimeout(() => node.remove(), 9000);
  }
  // A healthy link says nothing. Only an action that actually failed surfaces,
  // once, just above the message box, with Retry. `kind` lets a later success
  // clear only the notice it answers (an offline notice clears when Kova is back).
  let noticeKind = '';
  function notice(message = '', kind = 'error') {
    noticeKind = message ? kind : '';
    $('notice-text').textContent = message; $('notice').hidden = !message;
  }
  function clearNotice(kind) { if (!kind || noticeKind === kind) notice(''); }
  // Screen readers get one short sentence for what needs an answer and for how a
  // turn ended. The transcript stays aria-live="off", so streamed text is never
  // read out token by token. A new node each time re-announces repeated words.
  function announce(message) {
    const region = $('announce');
    region.append(el('span', '', message));
    while (region.children.length > 3) region.children[0].remove();
  }
  function firstOf(node, tags) {
    for (const child of node.children || []) {
      if (tags.test(child.tagName || '')) return child;
      const nested = firstOf(child, tags);
      if (nested) return nested;
    }
    return null;
  }
  // A card that needs an answer takes focus, unless the user is typing somewhere:
  // its first button (or, for a free-text question, the field to type in).
  function focusCard(card, preferred = null) {
    if (typingIn(document.activeElement)) return;
    const target = preferred || firstOf(card, /^button$/i) || firstOf(card, /^(input|select|textarea)$/i) || card;
    target.focus?.({preventScroll: true});
  }
  function describeApproval(event) {
    const detail = event.args?.command || event.args?.path || event.summary || event.tool;
    const folder = state.chat?.cwd ? ` in ${state.chat.cwd}` : '';
    return `${event.tool === 'run_command' ? 'run command ' : ''}${detail}${folder}`;
  }
  // The capability contract the server declares: what each chat mode offers
  // once every gate has agreed. Nothing is offered that is not declared here.
  function offers(capability) {
    if (state.offered === null) return true;  // not told yet: nothing is hidden
    return (state.offered[$('mode').value] || []).includes(capability);
  }
  function applyCapabilities(contract) {
    state.offered = contract.offered || {};
    controls();
  }
  async function refreshCapabilities(model = '') {
    try { applyCapabilities(await json(model ? `/capabilities?model=${encodeURIComponent(model)}` : '/capabilities')); }
    catch (_) { state.offered = null; controls(); }  // an older server declares nothing
  }
  function controls() {
    const busy = state.busy;
    const attachmentCount = pendingAttachments.filter(a => a.chat_id === state.chat?.id).length;
    for (const id of ['new-chat', 'new-incognito', 'mode', 'web', 'model', 'permission', 'cwd', 'add-provider']) $(id).disabled = busy || !state.connected;
    // Incognito belongs to starting a chat, so the + menu offers it on the start screen.
    $('new-incognito').hidden = !!state.chat;
    for (const id of ['mode', 'web', 'permission', 'cwd']) $(id).disabled ||= !!state.chat?.ephemeral;
    $('web').disabled ||= state.localOnly;
    $('permission').disabled ||= !offers('approvals');
    $('cwd').disabled ||= !offers('tools');
    $('composer').dataset.mode = $('mode').value;
    const nothingToSend = !state.connected || !$('prompt').value.trim();
    // With steering the composer stays usable mid-reply; without it the reply
    // is stopped or waited for, so send is withheld while one runs.
    $('send').hidden = busy && !offers('steering'); $('stop').hidden = !busy;
    // Queue and Steer only make sense once there is something to send.
    $('queue').hidden = !busy || nothingToSend; $('steer').hidden = !busy || nothingToSend;
    $('send').disabled = readingAttachments || !state.connected || (!$('prompt').value.trim() && !attachmentCount);
    $('attach').disabled = !!state.chat?.ephemeral || state.busy || readingAttachments || !state.connected;
    $('attach').classList.toggle('text-only', !offers('vision'));
    $('attach').title = offers('vision') ? 'Attach files or images' : 'Attach documents; images need Vision turned on in Model settings';
    $('queue').disabled = nothingToSend; $('steer').disabled = nothingToSend;
    $('stop').disabled = state.stopRequested;
    document.querySelectorAll('.history-row button').forEach(button => { button.disabled = busy; });
    $('composer').setAttribute('aria-busy', String(busy));
    syncMenus();
    refreshStatusBar();
  }
  // First slice of #204: the optional status bar reads what the server already
  // sent (chat messages, the model list, the active model) and reuses the same
  // ~4-chars-a-token estimate as the terminal chat. No new endpoint.
  function refreshStatusBar() {
    const status = typeof window !== 'undefined' ? window.KovaStatus : null;
    if (!status) return;
    const active = (state.models || []).find(entry => entry.name === $('model').value);
    status.update({model: $('model').value || '', windowTokens: (active && active.context) || 0,
                   messages: (state.chat && state.chat.messages) || []});
  }
  window.KovaStatusRefresh = refreshStatusBar;
  function disconnected(message) {
    state.connected = false;
    state.events?.close();
    notice(message || (onPhone() ? 'This phone is not connected. On your computer, run kova pair (or choose Connect your phone in Kova) and scan the code.'
      : 'Open kova-web on this computer to connect. No token or account needed.'));
    controls();
  }
  const OFFLINE = 'Cannot reach Kova. Check that it is running, then retry.';
  async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, {credentials: 'same-origin', ...options,
      headers: {...(typeof FormData !== 'undefined' && options.body instanceof FormData ? {} : {'Content-Type': 'application/json'}), ...options.headers}}); }
    catch (_) { const error = new Error(OFFLINE); error.offline = true; throw error; }
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { const body = await response.json(); if (typeof body.detail === 'string') message = body.detail; else if (Array.isArray(body.detail)) message = body.detail.map(item => `${item.loc?.slice(1).join('.') || 'Field'}: ${item.msg}`).join('; '); } catch (_) {}
      if (response.status === 401) disconnected(onPhone() ? 'This phone is no longer paired. On your computer, run kova pair (or choose Connect your phone) and scan the new code.'
        : 'Your connection expired. Open kova-web again to reconnect.');
      throw new Error(message);
    }
    return response;
  }
  const json = async (path, options) => (await api(path, options)).json();
  const write = (method, value) => ({method, body: JSON.stringify(value)});
  function markdown(node, text, final = false) {
    node.innerHTML = DOMPurify.sanitize(marked.parse(text, {breaks: true, gfm: true}), {
      // No remote images, embedded content, form controls, SVG or style injection.
      ALLOWED_TAGS: ['p','br','strong','em','del','a','code','pre','blockquote','ul','ol','li','h1','h2','h3','h4','h5','h6','hr','table','thead','tbody','tr','th','td'],
      ALLOWED_ATTR: ['href','title','class','start'],
    });
    node.querySelectorAll('table').forEach(table => {
      const wrap = el('div', 'table-wrap'); table.replaceWith(wrap); wrap.append(table);
    });
    node.querySelectorAll('a').forEach(link => {
      const href = link.getAttribute('href') || '';
      // Restrict navigation to explicit web links, never local API URLs or JS.
      if (!/^https?:\/\//i.test(href)) link.removeAttribute('href');
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    });
    // The bar is there from the first streamed line, so nothing jumps when the reply ends.
    node.querySelectorAll('pre code').forEach(code => {
      try { if (code.textContent.length < (final ? 20000 : 4000)) hljs.highlightElement(code); } catch (_) {}
      const language = /language-([\w+#.-]+)/.exec(code.className)?.[1] || 'text';
      const bar = el('div', 'code-bar'), copy = el('button', 'copy-code'), text = el('span', '', 'Copy');
      copy.type = 'button'; copy.setAttribute('aria-label', 'Copy code'); copy.append(icon('copy'), text);
      copy.onclick = () => copyText(code.textContent, () => {
        text.textContent = 'Copied'; setTimeout(() => { text.textContent = 'Copy'; }, 1800);
      });
      bar.append(el('span', 'code-lang', language), copy);
      code.parentNode.insertBefore(bar, code);
    });
  }
  async function copyText(text, copied) {
    try { await navigator.clipboard.writeText(text); copied?.(); }
    catch (_) { toast('Clipboard unavailable. Select the text to copy it.'); }
  }
  // What an answer cost and how fast it was: "1.2k in · 340 out · ~$0.004 · 38 tok/s".
  function shortTokens(count) {
    count = Number(count) || 0;
    if (count < 1000) return String(count);
    if (count < 10000) return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    if (count < 1e6) return `${Math.round(count / 1000)}k`;
    return `${(count / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  }
  function money(value) {
    if (!value) return '$0.00';
    return value < 0.01 ? `$${value.toFixed(4).replace(/0+$/, '')}` : `$${value.toFixed(2)}`;
  }
  function usageLine(usage) {
    const mark = usage.estimated ? '~' : '';
    const parts = [`${mark}${shortTokens(usage.prompt_tokens)} in`, `${mark}${shortTokens(usage.completion_tokens)} out`];
    if (usage.local) parts.push('$0.00 local');
    else if (usage.cost_usd != null) parts.push(`~${money(usage.cost_usd)}`);
    if (usage.tokens_per_second) parts.push(`${usage.tokens_per_second} tok/s`);
    return parts.join(' · ');
  }
  function usageDetails(usage) {
    const seconds = ms => `${(ms / 1000).toFixed(1)} s`;
    const lines = [];
    if (usage.ms_first_token != null) lines.push(`First token after ${seconds(usage.ms_first_token)}`);
    if (usage.ms_total != null) lines.push(`${seconds(usage.ms_total)} in total`);
    if (usage.model) lines.push(`Model: ${usage.model}`);
    if (usage.estimated) lines.push('Token counts estimated: the server sent none');
    if (!usage.local && usage.cost_usd == null) lines.push('Price unknown: add it under [pricing] in config.toml');
    else if (!usage.local) lines.push('Cost is an estimate from list prices');
    return lines.join('\n');
  }
  function usageMeter(usage) {
    const meter = el('span', 'usage-meter', usageLine(usage));
    meter.title = usageDetails(usage);
    meter.setAttribute('aria-label', `${usageLine(usage)}. ${usageDetails(usage).replaceAll('\n', '. ')}`);
    return meter;
  }
  // Quiet actions under a message: copy it, or bring your own words back to edit.
  function messageActions(article, read, role, usage = null) {
    const bar = el('div', 'message-actions');
    const glyph = icon('copy'), copy = el('button', 'message-action');
    copy.type = 'button'; copy.title = 'Copy'; copy.setAttribute('aria-label', role === 'user' ? 'Copy your message' : 'Copy reply');
    copy.append(glyph);
    copy.onclick = () => copyText(read(), () => {
      glyph.className = 'icon i-check'; copy.title = 'Copied';
      setTimeout(() => { glyph.className = 'icon i-copy'; copy.title = 'Copy'; }, 1600);
    });
    bar.append(copy);
    if (role === 'user') {
      const edit = iconButton('pencil', 'Edit as a new message', 'message-action');
      edit.onclick = () => { $('prompt').value = read(); resize(); $('prompt').focus(); };
      bar.append(edit);
    }
    if (usage) bar.append(usageMeter(usage));
    article.append(bar);
  }
  // Tool rows speak plainly: what ran, on what, and the gist of what came back.
  function toolLabel(name) {
    const words = String(name || 'tool').replaceAll('_', ' ');
    return words[0].toUpperCase() + words.slice(1);
  }
  function toolHint(args) {
    if (typeof args === 'string') { try { args = JSON.parse(args); } catch (_) { return ''; } }
    for (const key of ['command', 'path', 'query', 'url', 'title', 'text', 'when', 'name']) {
      if (typeof args?.[key] === 'string' && args[key].trim()) return args[key].replace(/\s+/g, ' ').trim().slice(0, 200);
    }
    return '';
  }
  function toolSummary(event) {
    if (event.ok && event.name === 'update_plan') return 'Plan updated';
    let text = event.summary || '';
    try {
      const data = JSON.parse(event.output);
      if (Array.isArray(data)) text = `${data.length} result${data.length === 1 ? '' : 's'}`;
      else if (data && typeof data === 'object') {
        const said = ['error', 'message', 'output', 'stdout', 'content', 'text', 'result'].map(key => data[key]).find(value => typeof value === 'string' && value.trim());
        if (said) text = said; else if (data.ok === true) text = 'Done';
      }
    } catch (_) { /* plain text results read fine as they are */ }
    return text.replace(/\s+/g, ' ').trim().slice(0, 200);
  }
  function toolRow(name, args, glyph) {
    const node = el('details', 'tool'), row = el('summary', 'tool-row'), detail = el('span', 'tool-detail', toolHint(args));
    row.append(glyph, el('strong', '', toolLabel(name)), detail); row.title = name || '';
    node.append(row);
    return {node, row, detail, hint: detail.textContent};
  }
  function message(role, content, heading, attachments = []) {
    const article = el('article', `message ${role}`);
    const label = el('div', 'message-label');
    if (role === 'user') label.append(document.createTextNode(heading || 'You'));
    else { const mark = el('img', 'avatar'); mark.src = '/app/assets/icon.svg'; mark.alt = ''; label.append(mark, document.createTextNode(heading || state.persona.name)); }
    const body = el('div', 'message-body');
    if (role === 'user') {
      body.textContent = content || '';
      if (Array.isArray(attachments) && attachments.length) {
        const files = el('div', 'message-attachments');
        for (const file of attachments) {
          const chip = el('span', 'attachment-chip message-attachment', file.name || 'Attached file');
          chip.title = 'Attached file';
          files.append(chip);
        }
        body.append(files);
      }
    } else if (content) { const md = el('div', 'markdown'); markdown(md, content, true); body.append(md); }
    article.append(label, body); $('messages').append(article);
    if (role === 'user' && content) messageActions(article, () => content, 'user');
    return body;
  }
  // Per-chat colour and icon live in chat-appearance.js; the sidebar keeps
  // working when it is missing, so every hook below guards its presence.
  const chatAppearance = () => window.KovaChatAppearance || null;
  function history(chats) {
    const list = $('chat-list');
    list.replaceChildren();
    if (!chats.length) list.append(el('div', 'history-empty', $('search').value ? 'No chats found.' : 'No chats yet.'));
    // Pinned chats first, then date groups; app.js only renders what the module
    // decided, so the grouping stays testable without a DOM (chat_list.js).
    const groups = globalThis.KovaChatList?.groups(chats) || [{label: '', chats}];
    for (const group of groups) {
      if (group.label) list.append(el('div', 'history-group', group.label));
      for (const chat of group.chats) {
        const row = el('div', `history-row${state.chat?.id === chat.id ? ' active' : ''}`);
        chatAppearance()?.decorateRow(row, chat);
        const button = el('button', 'history-open');
        const title = chat.ephemeral ? 'Incognito' : chat.title || 'New conversation';
        const titleRow = el('span', 'history-title');
        if (chat.pinned) titleRow.append(icon('pin'));
        titleRow.append(document.createTextNode(title));
        button.append(titleRow); button.title = title;
        // A row is its title. A second line appears only while the chat needs
        // attention (working, or waiting on an approval); the date groups say when.
        if (chat.state && chat.state !== 'done') button.append(el('span', 'history-date', chat.state.replaceAll('-', ' ')));
        button.onclick = () => safe(() => openChat(chat.id));
        const actions = el('details', 'history-actions'), more = el('summary');
        more.append(icon('ellipsis')); more.title = `Actions for ${title}`; more.setAttribute('aria-label', `Actions for ${title}`);
        actions.append(more); actions.setAttribute('aria-label', `Actions for ${title}`);
        const items = [['pin', chat.pinned ? 'Unpin' : 'Pin', () => togglePin(chat)], ['copy', 'Fork conversation', () => forkChat(chat.id)], ['pencil', 'Rename', () => editChat(chat, false)]];
        if (chatAppearance()) items.push(['sliders-horizontal', 'Colour & icon', () => editAppearance(chat)]);
        items.push(['trash-2', 'Delete', () => editChat(chat, true)]);
        for (const [symbol, name, fn] of items) {
          if (chat.ephemeral && symbol === 'copy') continue;
          const action = el('button', 'btn', name); action.type = 'button'; action.setAttribute('aria-label', `${name} ${title}`);
          action.onclick = () => safe(fn); actions.append(action);
        }
        row.append(button, actions); list.append(row);
      }
    }
    controls();
  }
  // A pin only reorders the sidebar, so it works while a turn runs and needs no
  // confirmation. The server keeps it in the same PATCH as the chat's settings.
  async function togglePin(chat) {
    const pinned = !chat.pinned;
    await json(`/chats/${chat.id}`, write('PATCH', {pinned}));
    chat.pinned = pinned;
    if (state.chat?.id === chat.id) state.chat.pinned = pinned;
    history(state.chats);
  }
  async function refreshChats() {
    const version = ++state.searchVersion;
    const query = $('search').value.trim();
    const chats = await json(query ? `/chats/search?q=${encodeURIComponent(query)}` : '/chats');
    if (version !== state.searchVersion) return;
    state.chats = chats; history(chats);
  }
  (function privacyPanel() {
    const dialog = el('dialog', 'panel privacy-dialog');
    dialog.setAttribute('aria-labelledby', 'privacy-title');
    const head = el('div', 'dialog-head'), title = el('h2', '', 'Privacy'); title.id = 'privacy-title';
    const close = iconButton('x', 'Close Privacy'); close.onclick = () => dialog.close(); head.append(title, close);
    const label = el('label', 'privacy-switch'), toggle = el('input'); toggle.type = 'checkbox';
    label.append(toggle, el('span', '', 'Local only'));
    const intro = el('p', '', 'Only loopback destinations are allowed when on. Enabling stops active turns and jobs, disconnects MCP, and withholds web and shell tools. It does not control other apps.');
    const filters = el('div', 'privacy-filters'), days = el('select'), chat = el('select');
    days.setAttribute('aria-label', 'Report period'); chat.setAttribute('aria-label', 'Filter by conversation');
    for (const count of [1, 7, 30, 90, 365]) { const option = el('option', '', `Last ${count} days`); option.value = String(count); days.append(option); }
    days.value = '30';
    const refresh = el('button', 'btn', 'Refresh'), download = el('button', 'btn', 'Export report');
    refresh.type = download.type = 'button'; filters.append(days, chat, refresh, download);
    const summary = el('p'); summary.setAttribute('role', 'status');
    const list = el('div', 'privacy-report');
    const note = el('p', '', 'Metadata only: hosts, times, sizes and usage. HTTP body bytes exclude headers, URL queries and TLS. Outgoing sizes are prepared payloads; failed requests may send nothing. Tokens appear only when the provider reports them. Incognito records stay in RAM and are excluded from this report. External process traffic is not recorded.');
    dialog.append(head, label, intro, filters, summary, list, note); document.body.append(dialog);
    let report = null, version = 0;
    function applyLocalOnly(value) {
      state.localOnly = Boolean(value); toggle.checked = state.localOnly;
      $('local-only-status').hidden = !state.localOnly; controls();
    }
    async function refreshReport() {
      const current = ++version; download.disabled = true;
      summary.textContent = 'Loading Privacy report…';
      try {
        const params = new URLSearchParams({days: days.value}); if (chat.value) params.set('chat_id', chat.value);
        const next = await json(`/privacy?${params}`);
        if (current !== version) return;
        report = next; applyLocalOnly(report.local_only);
        summary.textContent = `${report.requests} requests · ${report.bytes_out} body bytes prepared · ${report.bytes_in} received`;
        list.replaceChildren();
        if (!report.requests) list.append(el('p', '', 'No recorded requests in this period. Older traffic is not reconstructed.'));
        for (const group of report.groups) {
          const row = el('div', 'privacy-row');
          row.append(el('strong', '', `${group.day} · ${group.host}`),
            el('span', '', `${group.provider || 'Kova'} · ${group.kind} · ${group.local ? 'loopback' : 'remote'} · ${group.requests} requests · ${group.bytes_out} out / ${group.bytes_in} in · tokens ${group.tokens_in ?? '?'} / ${group.tokens_out ?? '?'}`));
          list.append(row);
        }
        const timeline = el('details'), heading = el('summary', '', report.truncated ? 'Latest 1000 requests (totals include all)' : 'Request timeline');
        timeline.append(heading);
        for (const item of report.items) {
          const row = el('p', '', `${item.created} · ${item.host} · ${item.kind} · ${item.status} · ${item.bytes_out} out / ${item.bytes_in} in`);
          if (item.chat_id) {
            const drill = el('button', 'btn quiet', 'This conversation'); drill.type = 'button';
            drill.onclick = () => { chat.value = item.chat_id; safe(refreshReport); }; row.append(drill);
          }
          timeline.append(row);
        }
        list.append(timeline); download.disabled = false;
      } catch (error) {
        if (current !== version) return;
        report = null; list.replaceChildren(); summary.textContent = `Could not load Privacy report. ${error.message || 'Refresh to retry.'}`;
      }
    }
    toggle.onchange = async () => {
      const requested = toggle.checked; toggle.disabled = true;
      try {
        const result = await json('/privacy', write('PATCH', {local_only: requested}));
        applyLocalOnly(result.local_only); await refreshReport(); await health();
      } catch (error) {
        toggle.checked = state.localOnly; summary.textContent = `Privacy setting was not saved. ${error.message || 'Retry.'}`;
      } finally { toggle.disabled = false; }
    };
    days.onchange = chat.onchange = refresh.onclick = () => safe(refreshReport);
    download.onclick = () => {
      if (!report) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}));
      const link = el('a'); link.href = url; link.download = 'kova-privacy.json'; link.click(); URL.revokeObjectURL(url);
    };
    $('open-privacy').onclick = () => safe(async () => {
      sidebar(false); chat.replaceChildren(); const all = el('option', '', 'All conversations'); all.value = ''; chat.append(all);
      for (const item of state.chats.filter(item => !item.ephemeral)) { const option = el('option', '', item.title || 'New conversation'); option.value = item.id; chat.append(option); }
      chat.value = ''; dialog.showModal(); await refreshReport();
    });
  })();

  // Phones get a drawer; wider windows collapse the sidebar in place and remember it.
  const wide = window.matchMedia?.('(min-width:721px)');
  function menuState() {
    const shown = wide?.matches ? !document.body.classList.contains('sidebar-collapsed') : document.body.classList.contains('sidebar-open');
    $('menu').setAttribute('aria-expanded', String(shown));
  }
  function sidebar(open) {
    document.body.classList.toggle('sidebar-open', open); $('backdrop').hidden = !open;
    menuState();
  }
  function toggleSidebar(show) {
    if (!wide?.matches) return sidebar(show ?? !document.body.classList.contains('sidebar-open'));
    const collapsed = !(show ?? document.body.classList.contains('sidebar-collapsed'));
    document.body.classList.toggle('sidebar-collapsed', collapsed); menuState();
    try { localStorage.setItem('kova-sidebar', collapsed ? 'collapsed' : 'open'); } catch (_) {}
  }
  function pageTitle() {
    $('incognito-note').hidden = !state.chat?.ephemeral;
    $('chat-menu').hidden = !state.chat;
    $('chat-title').textContent = state.chat?.ephemeral ? 'Incognito' : state.chat?.title || 'New chat';
    document.title = state.chat?.ephemeral ? `Incognito · ${state.persona.name}` : state.chat?.title ? `${state.chat.title} · ${state.persona.name}` : state.persona.name;
  }
  function renderQueue() {
    const strip = $('queue-strip');
    strip.replaceChildren();
    strip.hidden = !state.queued.length;
    for (const row of state.queued) {
      const item = el('div', 'queue-item');
      item.append(el('span', 'queue-mark', 'Queued'), el('span', 'queue-text', row.text));
      const remove = iconButton('x', 'Remove queued message');
      remove.onclick = () => safe(() => dropQueued(row));
      item.append(remove); strip.append(item);
    }
  }
  async function loadQueue() {
    const chatId = state.chat?.id;
    state.queued = []; renderQueue();
    if (!chatId) return;
    try {
      const rows = await json(`/chats/${chatId}/queue`);
      if (state.chat?.id === chatId) { state.queued = rows; renderQueue(); }
    } catch (_) { /* a queue read must never break opening a chat */ }
  }
  async function dropQueued(row) {
    await json(`/chats/${state.chat.id}/queue/${row.id}`, {method: 'DELETE'});
    state.queued = state.queued.filter(item => item.id !== row.id); renderQueue();
  }
  async function queueMessage(text) {
    if (!text || !state.chat) return;
    const command = parseCommand(text);
    if (command) return dispatchCommand(command);
    const row = await json(`/chats/${state.chat.id}/queue`, write('POST', {text}));
    state.queued.push(row); renderQueue();
    $('prompt').value = ''; resize(); controls();
  }
  async function steerMessage() {
    const text = $('prompt').value.trim();
    if (!text || !state.chat) return;
    const command = parseCommand(text);
    if (command) return dispatchCommand(command);
    await json(`/chats/${state.chat.id}/steer`, write('POST', {text}));
    $('prompt').value = ''; resize(); controls();
    toast('Sent into the running reply.', 'Steering');
  }
  async function runNextQueued() {
    if (!state.chat || state.busy) return;
    const row = await json(`/chats/${state.chat.id}/queue/next`, {method: 'POST'});
    if (!row?.text) return;
    state.queued = state.queued.filter(item => item.id !== row.id); renderQueue();
    // A paired phone's message is sent exactly as queued, bound to that phone's limits.
    await sendText(row.text, row.remote ? {queued: row.id} : {});
  }
  function setChat(chat) {
    state.chat = chat;
    renderAttachments();
    pageTitle();
    $('mode').value = chat?.mode || 'work';
    $('permission').value = chat?.permission || 'manual';
    $('cwd').value = chat?.cwd || '';
    setWeb(chat?.web ?? true);
    empty(!chat?.messages?.some(m => m.role === 'user'));
    $('messages').replaceChildren();
    // A saved turn reads like a live one: every round of it sits under one heading.
    const turns = []; let turn = null;
    for (const msg of chat?.messages || []) {
      if (msg.role === 'user') { message('user', msg.content, undefined, msg.attachments || []); turn = null; }
      else if (msg.role === 'assistant') {
        if (!turn) turns.push(turn = {body: message('assistant'), parts: []});
        if (msg.content) { const md = el('div', 'markdown'); markdown(md, msg.content, true); turn.body.append(md); turn.parts.push(msg.content); }
        if (msg.usage) turn.usage = msg.usage;
        if (msg.tool_calls?.length) for (const call of msg.tool_calls) {
          turn.body.append(toolRow(call.function?.name, call.function?.arguments, icon('check')).node);
        }
      }
      // Raw tool payloads stay in the server history, never masquerade as replies.
    }
    for (const saved of turns) if (saved.parts.length) messageActions(saved.body.parentNode, () => saved.parts.join('\n\n'), 'assistant', saved.usage);
    if (chat?.plan?.length) $('messages').append(planView(chat.plan));
    if (chat?.id) safe(() => loadChanges(chat.id));
    safe(loadQueue);
    nearBottom = true; requestAnimationFrame(follow); history(state.chats); sidebar(false); controls();
  }
  async function loadChanges(chatId) {
    const version = state.navigationVersion;
    const checkpoints = await json(`/chats/${chatId}/checkpoints`);
    if (version !== state.navigationVersion || state.chat?.id !== chatId || !checkpoints.length) return;
    const section = el('section', 'changes-view');
    section.setAttribute('aria-label', 'Changes Kova made');
    section.append(el('strong', '', 'Changes Kova made'));
    for (const checkpoint of checkpoints) section.append(reviewCard(checkpoint));
    $('messages').append(section); follow();
  }
  // An approval card only lives in the stream of the turn that asked. After a
  // reload, or in a second window, rebuild it from the server so the blocked
  // turn can still be answered instead of waiting out its 10-minute timeout.
  let recovered = {chatId: null, view: null, cards: new Map()}, background = null;
  async function loadPending(chatId) {
    const version = state.navigationVersion;
    const pending = await json(`/chats/${chatId}/approvals`);
    // A turn this window is streaming already shows its own cards.
    if (version !== state.navigationVersion || state.chat?.id !== chatId || state.busy) return;
    if (recovered.chatId !== chatId || !recovered.view?.parentNode) {
      const view = el('section', 'pending-view'); view.setAttribute('aria-label', 'Waiting for you');
      view.append(el('strong', '', 'Waiting for you'));
      recovered = {chatId, view, cards: new Map()};
    }
    const ids = new Set(pending.map(event => event.id));
    for (const [id, card] of recovered.cards) if (!ids.has(id)) { card.finish(); recovered.cards.delete(id); }
    const fresh = [];
    for (const event of pending) {
      if (recovered.cards.has(event.id)) continue;
      const card = event.type === 'question' ? questionCard(event) : approvalCard(event);
      recovered.cards.set(event.id, card); recovered.view.append(card.card); fresh.push({event, card});
    }
    if (pending.length && !recovered.view.parentNode) $('messages').append(recovered.view);
    // A reload or a second window rebuilds the card; say so the same way.
    for (const {event, card} of fresh) {
      announce(event.type === 'question' ? `Kova asked a question: ${event.args.question}`
                                         : `Kova needs approval: ${describeApproval(event)}`);
      if (card === fresh[0].card) focusCard(card.card);
    }
    follow();
  }
  // Live updates for a chat whose turn runs outside this window's stream.
  function chatStateChanged(chatId, next, previous) {
    if (state.busy || state.chat?.id !== chatId) return;
    if (next === 'blocked-on-approval' || recovered.cards.size) safe(() => loadPending(chatId));
    if (next !== 'done') background = chatId;
    // Show the finished reply, but only for a turn this window did not stream itself.
    else if (background === chatId && previous !== 'done') { background = null; safe(() => openChat(chatId)); }
  }
  async function openChat(id) {
    if (state.busy) return;
    const version = ++state.navigationVersion;
    const chat = await json(`/chats/${id}`);
    if (version === state.navigationVersion && !state.busy) {
      if (state.chat?.id !== id) await discardIncognito();
      if (version !== state.navigationVersion || state.busy) return;
      setChat(chat); background = null;
      if (chat.state && chat.state !== 'done') {
        background = id;
        $('messages').append(el('div', 'turn-status background-turn', chat.state === 'blocked-on-approval'
          ? 'This reply is paused until you answer below.' : 'Kova is still working on this reply. It appears here when it finishes.'));
        safe(() => loadPending(id));
      }
    }
  }
  async function discardIncognito() {
    if (state.chat?.ephemeral) {
      await json(`/chats/${state.chat.id}`, {method: 'DELETE'});
      // Never carry an unsent private draft into a saved conversation.
      $('prompt').value = ''; resize();
    }
  }
  async function newChat() { if (!state.busy) { ++state.navigationVersion; const wasIncognito = !!state.chat?.ephemeral; await discardIncognito(); setChat(null); $('prompt').value = ''; $('prompt').focus(); if (wasIncognito) await refreshChats(); } }
  async function newIncognito() {
    if (state.busy || !state.connected) return;
    ++state.navigationVersion;
    await discardIncognito();
    setChat(await json('/chats', write('POST', {ephemeral: true, title: 'Incognito', mode: 'chat', web: false})));
    $('prompt').value = ''; $('prompt').focus(); await refreshChats();
  }
  // No transcript or draft goes into browser storage. A normal tab close or
  // reload discards the server's RAM-only chat as well.
  window.addEventListener('pagehide', () => {
    if (state.chat?.ephemeral) fetch(`/chats/${state.chat.id}`, {method: 'DELETE', keepalive: true}).catch(() => {});
  });
  async function editChat(chat, remove) {
    if (state.busy) return;
    const dialog = $('chat-dialog');
    $('dialog-title').textContent = remove ? 'Delete conversation?' : 'Rename conversation';
    $('dialog-description').textContent = remove ? 'This removes the conversation from this computer. It cannot be undone.' : '';
    $('rename-label').hidden = remove; $('rename-input').value = chat.title || '';
    $('dialog-confirm').textContent = remove ? 'Delete' : 'Save';
    $('dialog-confirm').className = remove ? 'btn danger' : 'btn primary';
    dialog.returnValue = ''; dialog.showModal();
    const choice = await new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue), {once: true}));
    if (choice !== 'confirm') return;
    if (remove) {
      await json(`/chats/${chat.id}`, {method: 'DELETE'});
      if (state.chat?.id === chat.id) setChat(null);
    } else {
      const title = $('rename-input').value.trim(); if (!title) return toast('Give your conversation a name.');
      await json(`/chats/${chat.id}`, write('PATCH', {title}));
      if (state.chat?.id === chat.id) { state.chat.title = title; pageTitle(); }
    }
    await refreshChats();
  }
  async function editAppearance(chat) {
    if (state.busy || !chatAppearance()) return;
    const patch = await chatAppearance().openDialog(chat);
    if (!patch) return;
    const updated = await json(`/chats/${chat.id}`, write('PATCH', patch));
    if (state.chat?.id === chat.id) Object.assign(state.chat, updated);
    await refreshChats();
  }
  // A cached health answer is still useful, so say when it was measured.
  function checkedAgo(value) {
    if (!value) return '';
    const seconds = Math.max(0, Math.round((Date.now() - Date.parse(value)) / 1000));
    if (!Number.isFinite(seconds)) return '';
    if (seconds < 45) return 'just now';
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    return `${Math.round(seconds / 3600)} h ago`;
  }
  async function health() {
    try {
      const status = await json('/health');
      state.localOnly = Boolean(status.local_only);
      $('local-only-status').hidden = !state.localOnly;
      controls();
      if (!state.connected) return;
      clearNotice('offline');
      // The model's state is a detail of the model chip, not a header badge: a
      // turn that fails because the model is down says so in the conversation.
      const configured = Boolean(state.models.find(model => model.name === status.model)?.model);
      const checked = checkedAgo(status.model_checked_at);
      state.modelStatus = status.model_ok && configured ? ''
        : configured ? (checked ? `Model offline (checked ${checked})` : 'Model offline') : 'Model not set up';
      syncMenus();
    } catch (_) { /* a poll is not an action: stay quiet until something the user does fails */ }
  }
  // Frames may split across network chunks, including multibyte Unicode tokens.
  async function readEvents(response, accept) {
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    function parse() {
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (data) accept(JSON.parse(data));
      }
    }
    try {
      while (true) { const {done, value} = await reader.read(); if (done) break; buffer += decoder.decode(value, {stream: true}); parse(); }
      buffer += decoder.decode(); parse();
      if (buffer.trim()) throw new Error('Reply stream ended mid-frame. Reopen the conversation to check saved messages.');
    } finally { reader.releaseLock(); }
  }
  const STATUS_LABELS = {added: 'Added', modified: 'Modified', deleted: 'Deleted'};
  function changeTotals(files) {
    const totals = {added: 0, modified: 0, deleted: 0, additions: 0, deletions: 0};
    for (const file of files) {
      if (file.status in totals) totals[file.status] += 1;
      totals.additions += file.additions || 0; totals.deletions += file.deletions || 0;
    }
    return totals;
  }
  function diffView(preview) {
    return KovaDiff.render(preview);
  }

  function changeFile(chatId, checkpointId, file) {
    const details = el('details', 'change-file');
    const summary = el('summary');
    summary.append(el('span', `status-badge status-${file.status}`, STATUS_LABELS[file.status] || file.status),
                   el('span', 'change-path', file.path),
                   el('span', 'change-stats', `+${file.additions || 0} −${file.deletions || 0}`));
    details.append(summary);
    let loaded = false;
    details.addEventListener('toggle', () => safe(async () => {
      if (!details.open || loaded || !checkpointId) return;
      loaded = true;
      try {
        const preview = await json(`/chats/${chatId}/checkpoints/${checkpointId}/diff?path=${encodeURIComponent(file.path)}`);
        details.append(diffView(preview));
      } catch (error) { loaded = false; throw error; }
    }));
    return details;
  }
  function confirmRestore(checkpoint) {
    const chatId = state.chat?.id;
    if (!chatId) return;
    safe(async () => {
      // Everything at or after this change set is undone, not only its own files.
      const checkpoints = await json(`/chats/${chatId}/checkpoints`);
      const affected = new Map();
      for (const entry of checkpoints) {
        if (entry.id < checkpoint.id) continue;
        for (const file of entry.files || []) if (!affected.has(file.path)) affected.set(file.path, file.status);
      }
      const dialog = el('dialog', 'restore-dialog');
      dialog.append(el('h2', '', `Restore to change set ${checkpoint.id}?`));
      dialog.append(el('p', '', 'Files Kova changed since then are put back the way they were, and files it created are removed. Your git repository is never touched.'));
      const list = el('ul', 'restore-list');
      for (const [path, status] of affected) list.append(el('li', '', `${STATUS_LABELS[status] || status} ${path}`));
      if (!affected.size) list.append(el('li', '', 'Nothing to restore.'));
      dialog.append(list);
      const actions = el('div', 'dialog-actions');
      const cancel = el('button', 'btn', 'Cancel'), confirm = el('button', 'btn primary', 'Restore files');
      cancel.onclick = () => dialog.close();
      confirm.onclick = () => safe(async () => {
        confirm.disabled = true;
        try {
          const result = await json(`/chats/${chatId}/checkpoints/${checkpoint.id}/restore`, {method: 'POST'});
          dialog.close();
          const count = result.restored.length;
          toast(`Restored ${count} file${count === 1 ? '' : 's'}${result.deleted.length ? `, removed ${result.deleted.length}` : ''}.`);
        } catch (error) { confirm.disabled = false; throw error; }
      });
      actions.append(cancel, confirm); dialog.append(actions);
      document.body.append(dialog); dialog.onclose = () => dialog.remove(); dialog.showModal();
    });
  }
  function reviewCard(checkpoint) {
    const files = checkpoint.files || [], skipped = checkpoint.skipped || [];
    const card = el('section', 'changes-card');
    card.setAttribute('aria-label', 'Changes Kova made');
    const totals = changeTotals(files);
    const head = el('div', 'changes-head');
    head.append(icon('pencil'), el('strong', '', checkpoint.live ? 'Changed in this turn' : `Change set ${checkpoint.id}`));
    if (checkpoint.label) head.append(el('span', 'changes-label', checkpoint.label));
    if (files.length) head.append(el('span', 'change-stats', `+${totals.additions} −${totals.deletions}`));
    card.append(head);
    if (files.length) {
      const list = el('div', 'change-list');
      for (const file of files) list.append(changeFile(state.chat?.id, checkpoint.id, file));
      card.append(list);
    }
    if (skipped.length) card.append(el('small', 'changes-skipped', `${skipped.length} file${skipped.length === 1 ? ' is' : 's are'} too large to checkpoint: ${skipped.join(', ')}`));
    if (files.length && checkpoint.id) {
      const restore = el('button', 'btn quiet', 'Restore files'); restore.type = 'button';
      restore.onclick = () => confirmRestore(checkpoint);
      card.append(restore);
    }
    return card;
  }
  function planView(items) {
    const section = el('section', 'plan-view'); section.setAttribute('aria-label', 'Plan');
    section.append(el('strong', '', 'Plan'));
    const list = el('ul');
    for (const item of items) {
      const row = el('li', `plan-${item.status}`);
      const mark = el('span', 'plan-mark'); mark.setAttribute('role', 'img'); mark.setAttribute('aria-label', item.status.replace('_', ' '));
      if (item.status === 'done') mark.append(icon('check'));
      row.append(mark, el('span', '', item.text)); list.append(row);
    }
    section.append(list); return section;
  }
  function approvalCard(event) {
    const card = el('section', 'approval-card');
    card.setAttribute('aria-label', 'Tool approval');
    card.setAttribute('role', 'group'); card.setAttribute('tabindex', '-1');
    const heading = el('div', 'approval-heading');
    heading.append(icon('shield-check'), el('strong', '', 'Kova needs your approval'), el('span', `risk-badge risk-${event.risk}`, event.risk));
    card.append(heading, el('small', '', event.tool), el('pre', '', event.args?.command || event.args?.path || event.summary));
    if (event.change_summary) card.append(el('small', '', event.change_summary));
    if (event.args && event.tool !== 'run_command') {
      const details = el('details', 'approval-arguments');
      details.append(el('summary', '', 'Requested changes'), el('pre', '', JSON.stringify(event.args, null, 2)));
      card.append(details);
    }
    if (state.chat?.cwd) { const folder = el('small', 'approval-folder'); folder.append(icon('folder'), el('span', '', state.chat.cwd)); card.append(folder); }
    const label = el('label', 'approval-message'); label.append(el('span', '', 'Optional: what to do instead'));
    const input = el('input'); input.maxLength = 4000; input.placeholder = 'Tell Kova what to do instead'; label.append(input); card.append(label);
    const actions = el('div', 'approval-actions'), status = el('div', 'approval-status');
    status.setAttribute('role', 'status');
    const buttons = [];
    for (const [decision, text] of [['allow', 'Allow once'], ['allow_session', 'Allow for session'], ['allow_always', 'Always allow this'], ['deny', 'Deny']]) {
      const button = el('button', decision === 'allow' ? 'btn primary' : decision === 'deny' ? 'btn danger' : 'btn', text); button.type = 'button';
      button.onclick = () => safe(async () => {
        buttons.forEach(b => { b.disabled = true; }); input.disabled = true;
        try {
          await json(`/chats/${state.chat.id}/approvals/${event.id}`, write('POST', {decision, message: input.value || null}));
          status.textContent = decision === 'deny' ? 'Denied' : 'Allowed';
          $('prompt').focus({preventScroll: true});
        } catch (error) {
          buttons.forEach(b => { b.disabled = false; });
          input.disabled = false; throw error;
        }
      });
      actions.append(button); buttons.push(button);
    }
    card.append(actions, status);
    return {card, resolve(decision, message) {
      buttons.forEach(b => { b.disabled = true; }); input.disabled = true;
      status.textContent = decision === 'allow_always' ? 'Always allowed: this exact action' : decision === 'allow_session' ? 'Allowed for session: this exact action' : decision === 'allow' ? 'Allowed once' : message || 'Denied';
    }, finish() {
      buttons.forEach(b => { b.disabled = true; }); input.disabled = true;
      if (!status.textContent) status.textContent = 'No longer pending';
    }};
  }
  function questionCard(event) {
    const chatId = state.chat.id;
    const card = el('section', 'approval-card');
    card.setAttribute('aria-label', 'Question from Kova');
    card.setAttribute('role', 'group'); card.setAttribute('tabindex', '-1');
    card.append(el('strong', '', event.args.question));
    const actions = el('div', 'approval-actions'), status = el('div', 'approval-status');
    const input = el('input'); input.maxLength = 4000; input.placeholder = 'Your answer';
    const buttons = [];
    async function answer(value) {
      if (!value?.trim()) { status.textContent = 'Enter an answer first.'; return; }
      buttons.forEach(b => b.disabled = true); input.disabled = true;
      try {
        await json(`/chats/${chatId}/approvals/${event.id}`, write('POST', {decision: 'allow', message: value}));
        status.textContent = `Answered: ${value}`;
        $('prompt').focus({preventScroll: true});
      } catch (error) { buttons.forEach(b => b.disabled = false); input.disabled = false; throw error; }
    }
    for (const option of event.args.options || []) {
      const button = el('button', 'btn', option); button.type = 'button';
      button.onclick = () => safe(() => answer(option)); actions.append(button); buttons.push(button);
    }
    if (!event.args.options?.length) {
      card.append(input); const button = el('button', 'btn primary', 'Send answer'); button.type = 'button';
      button.onclick = () => safe(() => answer(input.value));
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); button.click(); } };
      actions.append(button); buttons.push(button);
    }
    card.append(actions, status);
    return {card, input, resolve() { buttons.forEach(b => b.disabled = true); input.disabled = true; },
      finish() { buttons.forEach(b => b.disabled = true); input.disabled = true; if (!status.textContent) status.textContent = 'No longer pending'; }};
  }
  function turnView(body) {
    let current = null, scheduled = false, finished = false, usage = null;
    const blocks = [], pendingTools = [], approvals = new Map();
    let plan = null;
    const activityText = el('span', 'shimmer', 'Thinking…');
    // The Kova mark with a small bolt that strikes now and then: still "working", never a loud spinner.
    const activityMark = el('span', 'thinking-mark'), activityLogo = el('img', 'thinking-logo');
    activityMark.setAttribute('aria-hidden', 'true'); activityLogo.src = '/app/assets/icon.svg'; activityLogo.alt = '';
    activityMark.append(activityLogo, el('span', 'thinking-bolt'));
    const activity = el('div', 'activity'); activity.append(activityMark, activityText); body.append(activity);
    function paint() {
      scheduled = false;
      for (const block of blocks) {
        if (block.kind === 'text' && block.dirty) { markdown(block.node, block.text, finished); block.dirty = false; }
        // Only the block being written right now carries the live cursor.
        block.node.classList.toggle('live', !finished && block === current && !block.end);
        if (block.kind === 'reasoning') {
          block.pre.textContent = block.text;
          block.summary.textContent = block.end ? `Thought for ${((block.end - block.start) / 1000).toFixed(1)}s` : 'Thinking…';
        }
      }
      follow();
    }
    function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(paint); } }
    function endReasoning() { if (current?.kind === 'reasoning' && !current.end) current.end = performance.now(); }
    return {
      accept(event) {
        const kind = event.type;
        if (kind !== 'reasoning') endReasoning();
        if (kind === 'text' || kind === 'reasoning') {
          // The reasoning line says "Thinking…" itself; one indicator at a time.
          activity.hidden = true;
          if (!current || current.kind !== kind) {
            current = {kind, text: '', dirty: true, start: performance.now()};
            if (kind === 'text') current.node = el('div', 'markdown');
            else { current.node = el('details', 'thinking'); current.summary = el('summary', '', 'Thinking…'); current.pre = el('pre'); current.node.append(current.summary, current.pre); }
            body.insertBefore(current.node, activity); blocks.push(current);
          }
          current.text += event.text; current.dirty = true;
        } else if (kind === 'compact_skipped') {
          toast(event.message);
        } else if (kind === 'compacted') {
          current = null;
          const details = el('details', 'turn-status');
          details.append(el('summary', '', `Compacted ${event.count} messages`), el('pre', '', event.summary));
          body.insertBefore(details, activity);
        } else if (kind === 'tool_call') {
          current = null; activity.hidden = true;
          const spinner = el('span', 'spinner'), tool = toolRow(event.name, event.args, spinner);
          if (!tool.hint) tool.detail.textContent = 'Running…';
          body.insertBefore(tool.node, activity);
          pendingTools.push({...tool, name: event.name, icon: spinner, done: false});
        } else if (kind === 'question') {
          activity.hidden = true;
          const question = questionCard(event); approvals.set(event.id, question);
          body.insertBefore(question.card, activity);
          announce(`Kova asked a question: ${event.args.question}`);
          window.KovaSounds?.play('needs');
          // A question with no options is answered by typing, so focus the field.
          focusCard(question.card, event.args.options?.length ? null : question.input);
        } else if (kind === 'question_resolved') {
          approvals.get(event.id)?.resolve();
        } else if (kind === 'approval') {
          activity.hidden = true;
          const approval = approvalCard(event); approvals.set(event.id, approval);
          body.insertBefore(approval.card, activity);
          announce(`Kova needs approval: ${describeApproval(event)}`);
          window.KovaSounds?.play('needs');
          focusCard(approval.card);
        } else if (kind === 'approval_resolved') {
          approvals.get(event.id)?.resolve(event.decision, event.message);
        } else if (kind === 'egress') {
          const remote = (event.items || []).filter(item => !item.local);
          const hosts = [...new Set(remote.map(item => item.host))];
          body.insertBefore(el('div', 'turn-status', hosts.length ? `Network: ${hosts.join(', ')} · ${remote.reduce((n, item) => n + item.bytes_out, 0)} body bytes prepared.` : 'Network: loopback only for this answer.'), activity);
        } else if (kind === 'privacy') {
          current = null;
          const count = Number(event.count) || 0;
          const kinds = Array.isArray(event.kinds) && event.kinds.length ? ` (${event.kinds.join(', ')})` : '';
          const parts = [];
          if (count) parts.push(`redacted ${count} likely secret${count === 1 ? '' : 's'}${kinds}`);
          if (event.incomplete) parts.push('removed the unscanned rest');
          body.insertBefore(el('div', 'turn-status', `Privacy: ${parts.join('; ') || 'guard on'} before sending.`), activity);
        } else if (kind === 'plan') {
          const next = planView(event.items);
          if (plan) plan.replaceWith(next);
          else { document.querySelectorAll('.plan-view').forEach(old => old.remove()); body.insertBefore(next, activity); }
          plan = next;
        } else if (kind === 'review') {
          current = null; activity.hidden = true;
          if (event.files?.length || event.skipped?.length) {
            body.insertBefore(reviewCard({id: event.checkpoint_id, label: event.label, live: true,
                                          files: event.files || [], skipped: event.skipped || []}), activity);
          }
        } else if (kind === 'tool_result') {
          const tool = pendingTools.find(tool => tool.name === event.name && !tool.done);
          if (tool) {
            if (event.file_change) tool.node.append(KovaDiff.file(event.file_change));
            tool.done = true; tool.icon.className = `icon i-${event.ok ? 'check' : 'x'}`; tool.row.classList.toggle('failed', !event.ok);
            // What ran stays visible; a failure says why instead. The full result opens underneath.
            const summary = toolSummary(event);
            tool.detail.textContent = event.ok && tool.hint ? tool.hint : summary;
            const output = String(event.output ?? event.summary ?? '');
            if (output.trim() && output.trim() !== tool.detail.textContent) tool.node.append(el('pre', 'tool-output', output.length > 6000 ? output.slice(0, 6000) + '\n…' : output));
          }
          current = null; activity.hidden = false; activityText.textContent = 'Working…';
        } else if (kind === 'error') {
          // One announcement for a failure: the live region, not a second alert.
          const error = el('div', 'turn-error'); error.append(icon('triangle-alert'), el('span', '', event.message));
          body.insertBefore(error, activity);
          announce(`Error: ${event.message}`);
        } else if (kind === 'steer') {
          current = null;
          body.insertBefore(el('div', 'turn-status', `Steering: ${event.text}`), activity);
        } else if (kind === 'stopped') {
          body.insertBefore(el('div', 'turn-status', 'Reply stopped.'), activity);
          announce('Stopped.');
        } else if (kind === 'done') {
          announce('Reply finished.');
          window.KovaSounds?.play('done');
          usage = event.usage || null;
          const budget = event.budget_warning;
          if (budget?.monthly_usd) toast(`This month's estimated spend is ~${money(budget.spent_usd)} of your ${money(budget.monthly_usd)} budget. Nothing is blocked.`, 'Budget');
        }
        schedule();
      },
      finish() {
        endReasoning(); finished = true; activity.remove();
        for (const approval of approvals.values()) approval.finish();
        for (const tool of pendingTools.filter(t => !t.done)) { tool.icon.className = 'icon i-minus'; tool.detail.textContent = tool.hint ? `Interrupted · ${tool.hint}` : 'Interrupted'; }
        for (const block of blocks) block.dirty = true;
        paint();
        const reply = blocks.filter(block => block.kind === 'text').map(block => block.text).join('\n\n');
        if (reply.trim() && body.parentNode) messageActions(body.parentNode, () => reply, 'assistant', usage);
      }
    };
  }
  // One catalog drives discovery, help and dispatch. Unknown paths remain messages.
  const commands = {
    incognito: '/incognito', new: '/new', fork: '/fork [n]', compact: '/compact', mode: '/mode chat|work',
    web: '/web on|off', model: '/model <name>', memory: '/memory [list]', help: '/help',
    resume: '/resume [n]', permissions: '/permissions [mode|rules]', think: '/think',
    diff: '/diff [n]', restore: '/restore <n>', changes: '/changes', cd: '/cd <folder>',
    steer: '/steer <text>', clear: '/clear', exit: '/exit', quit: '/quit'
  };
  const terminalCommands = new Set(['resume', 'permissions', 'think', 'diff', 'restore', 'changes', 'cd', 'steer', 'clear', 'exit', 'quit']);
  function parseCommand(text) {
    if (/[\r\n]/.test(text)) return null;
    const match = /^\/(\S+)(?:\s+(.*))?$/.exec(text.trim());
    return match && Object.hasOwn(commands, match[1]) ? {name: match[1], args: match[2] || ''} : null;
  }
  async function forkChat(id = state.chat?.id) {
    if (state.chat?.ephemeral && id === state.chat.id) throw new Error('Incognito chats cannot be forked or exported.');
    if (!id) throw new Error('Open a conversation to fork first.');
    if (state.busy) throw new Error('Wait for the current reply before forking.');
    const chat = await json(`/chats/${id}/fork`, {method: 'POST'});
    await openChat(chat.id); await refreshChats();
  }
  async function compactChat() {
    if (!state.chat) throw new Error('Open a conversation to compact first.');
    if (state.busy) throw new Error('Wait for the current reply before compacting.');
    const id = state.chat.id;
    state.busy = true; controls();
    try {
      const result = await json(`/chats/${id}/compact`, {method: 'POST'});
      toast(result.type === 'compact_skipped' ? result.message : `Compacted ${result.count} messages`);
    } finally { state.busy = false; controls(); }
  }
  async function dispatchCommand({name, args}) {
    if (terminalCommands.has(name)) { toast(`/${name} lives in the terminal chat (kova).`); return; }
    if (state.busy) throw new Error('Wait for the current reply before running a command.');
    if (name === 'help') {
      empty(false);
      const card = message('assistant', '', 'Kova commands');
      card.append(el('pre', '', Object.values(commands).join('\n') + '\nTerminal-only commands open in kova.'));
    } else if (name === 'incognito' && !args) await newIncognito();
    else if (name === 'new' && !args) await newChat();
    else if (name === 'fork') {
      const chats = args ? await json('/chats') : null;
      const n = Number(args);
      if (args && (!/^\d+$/.test(args) || n < 1 || !chats[n - 1])) throw new Error('Use /fork [n], with a conversation number from history (newest first).');
      await forkChat(args ? chats[n - 1].id : state.chat?.id);
    } else if (name === 'compact' && !args) await compactChat();
    else if (name === 'mode' && ['chat', 'work'].includes(args)) {
      await chatSetting({mode: args}); $('mode').value = args; controls();
    } else if (name === 'web' && ['on', 'off'].includes(args)) {
      await chatSetting({web: args === 'on'}); setWeb(args === 'on'); controls();
    } else if (name === 'model' && args) {
      if (!state.models.some(model => model.name === args)) throw new Error('Unknown model. Choose a configured model.');
      await json('/models/active', write('POST', {name: args}));
      $('model').value = args; selectedModel = args; await health(); await refreshCapabilities(selectedModel);
    } else if (name === 'model' && !args) { options(true); $('model-slot').querySelector('button')?.click(); }
    else if (name === 'memory' && (!args || args === 'list')) $('open-memory').onclick();
    else throw new Error(`Usage: ${commands[name]}`);
  }
  function commandPicker() {
    const text = $('prompt').value.trim(), picker = $('command-picker');
    picker.replaceChildren();
    picker.hidden = !text.startsWith('/') || /\s/.test(text);
    if (picker.hidden) return;
    for (const [name, usage] of Object.entries(commands)) {
      if (!name.startsWith(text.slice(1))) continue;
      const button = el('button', 'btn', usage + (terminalCommands.has(name) ? ' · terminal' : ''));
      button.type = 'button';
      button.onclick = () => { $('prompt').value = `/${name} `; picker.hidden = true; resize(); $('prompt').focus(); };
      picker.append(button);
    }
    picker.hidden = !picker.children.length;
  }
  $('commands').onclick = () => { $('prompt').value = '/'; commandPicker(); $('prompt').focus(); };
  $('compact-now').onclick = () => safe(compactChat);
  async function send(event) {
    event.preventDefault(); const text = $('prompt').value.trim();
    if ((!text && !pendingAttachments.length) || readingAttachments || !state.connected) return;
    const command = text ? parseCommand(text) : null;
    if (command) {
      try { await dispatchCommand(command); $('prompt').value = ''; resize(); commandPicker(); }
      catch (error) { toast(error.message); }
      return;
    }
    if (state.busy && pendingAttachments.length) return toast('Send attachments after the current reply.');
    if (state.busy) return safe(() => queueMessage(text));
    await sendText(text || 'Please read the attached files.');
  }
  async function sendText(text, sendOptions = {}) {
    if (!text || state.busy || !state.connected) return;
    ++state.navigationVersion;
    state.busy = true; state.turnReady = false; state.stopRequested = false; controls();
    let turn, terminal = false;
    try {
      if (!state.chat) state.chat = await json('/chats', write('POST', {mode: $('mode').value, web: webOn(), permission: $('permission').value, ...($('cwd').value.trim() ? {cwd: $('cwd').value.trim()} : {})}));
      const outgoing = sendOptions.queued ? [] : pendingAttachments.filter(a => a.chat_id === state.chat.id);
      empty(false); message('user', text, undefined, outgoing); $('prompt').value = ''; resize(); options(false);
      nearBottom = true; turn = turnView(message('assistant')); follow();
      const response = await api(`/chats/${state.chat.id}/messages`, write('POST', {text, model: $('model').value || null, attachments: outgoing.map(a => a.id), ...(sendOptions.queued ? {queued: sendOptions.queued} : {})}));
      if (!sendOptions.queued) { for (let i = pendingAttachments.length - 1; i >= 0; i--) { if (pendingAttachments[i].chat_id === state.chat.id) pendingAttachments.splice(i, 1); } renderAttachments(); }
      state.turnReady = true;
      if (state.stopRequested) await cancelTurn();
      await readEvents(response, data => {
        if (terminal) return;
        terminal = ['done', 'error', 'stopped'].includes(data.type);
        turn.accept(data);
      });
      if (!terminal) throw new Error('Reply connection interrupted. Reopen this conversation to check saved messages; it may still be running.');
    } catch (error) {
      if (error.offline) notice(error.message, 'offline');
      if (turn) turn.accept({type: 'error', message: error.message}); else if (!error.offline) toast(error.message);
    } finally {
      const interrupted = state.stopRequested;
      turn?.finish(); state.busy = false; state.turnReady = false; state.stopRequested = false; controls();
      if (document.activeElement === $('prompt')) $('prompt').focus({preventScroll: true});
      if (state.connected) {
        try {
          // Update title/history without replacing the live reasoning and tool rows.
          if (state.chat) { state.chat = await json(`/chats/${state.chat.id}`); pageTitle(); }
          await refreshChats(); await health();
        } catch (error) { report(error); }
      }
      if (!interrupted) {
        try { await runNextQueued(); } catch (error) { report(error); }
      }
    }
  }
  async function cancelTurn() {
    try { await json(`/chats/${state.chat.id}/stop`, {method: 'POST'}); }
    catch (error) { state.stopRequested = false; controls(); toast(error.message); }
  }
  async function stop() {
    if (!state.busy) return;
    state.stopRequested = true; controls();
    // A click while the chat/stream is opening is applied as soon as headers arrive.
    if (state.turnReady) await cancelTurn();
  }
  // Losing Kova itself is said once, by the box (with Retry); anything else is a toast.
  function report(error) { if (error.offline) notice(error.message, 'offline'); else toast(error.message); }
  function safe(fn) { Promise.resolve().then(fn).catch(error => { report(error); controls(); }); }
  function resize() { $('prompt').style.height = 'auto'; $('prompt').style.height = `${Math.min($('prompt').scrollHeight, 180)}px`; controls(); }
  async function readWorkerShare(token) {
    if (typeof caches === 'undefined' || !location.origin) return null;
    const root = `${location.origin}/app/share/${encodeURIComponent(token)}`;
    try {
      const cache = await caches.open(SHARE_CACHE);
      const manifest = await cache.match(`${root}/manifest`);
      if (!manifest) return null;
      await cache.delete(`${root}/manifest`);
      const data = await manifest.json();
      if (!data || !Number.isFinite(data.created) || Date.now() - data.created > SHARE_TTL) {
        // An expired draft must not leave its file bytes in the cache.
        for (const entry of data?.files || []) await cache.delete(entry.key);
        return null;
      }
      const files = [];
      for (const entry of data.files || []) {
        const stored = await cache.match(entry.key);
        if (!stored) continue;
        await cache.delete(entry.key);
        const blob = await stored.blob();
        files.push(new File([blob], entry.name || 'file', {type: entry.type || blob.type || 'application/octet-stream'}));
      }
      return {text: typeof data.text === 'string' ? data.text : '', files};
    } catch (_) { return null; }
  }
  // Shared files are native attachments, not filename/MIME/base64 prose. Files
  // from the installed PWA go through the same upload API as the paperclip;
  // the server fallback has already saved them and hands back ready rows.
  async function applyShare(draft) {
    await discardIncognito();
    pendingAttachments.length = 0;
    if (draft.chat) {
      const accepted = [];
      for (const row of draft.attachments || []) {
        if (String(row.mime || '').startsWith('image/') && !offers('vision')) {
          try { await json(`/chats/${row.chat_id || draft.chat.id}/attachments/${row.id}`, {method: 'DELETE'}); } catch (_) {}
          toast('This model cannot read images. Turn on Vision for it in Model settings.');
          continue;
        }
        if (accepted.length < 3) accepted.push(row);
      }
      if (!accepted.length && !(draft.text || '').trim()) {
        try {
          await json(`/chats/${draft.chat.id}`, {method: 'DELETE'});
        } catch (_) { /* the empty draft is harmless if cleanup is refused */ }
        setChat(null);
      } else {
        setChat(draft.chat);
        pendingAttachments.push(...accepted);
        renderAttachments(); controls();
        await refreshChats();
      }
    } else {
      setChat(null);
    }
    $('prompt').value = draft.text || '';
    resize();
    if (draft.files?.length) await attachFiles(draft.files);
  }
  function renderAttachments() {
    $('attachment-chips').replaceChildren();
    for (const item of pendingAttachments.filter(a => a.chat_id === state.chat?.id)) {
      const chip = el('span', 'attachment-chip', `${item.name} · ${(item.size / 1024).toFixed(1)} KB${item.pages ? ` · ${item.pages} pages` : ''} · ready `);
      const remove = el('button', '', '×'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${item.name}`);
      remove.onclick = () => safe(async () => {
        await json(`/chats/${item.chat_id}/attachments/${item.id}`, {method: 'DELETE'});
        pendingAttachments.splice(pendingAttachments.indexOf(item), 1); renderAttachments(); controls();
      });
      chip.append(remove); $('attachment-chips').append(chip);
    }
    $('attachment-note').hidden = !pendingAttachments.some(a => a.chat_id === state.chat?.id);
  }
  async function attachFiles(files) {
    if (state.chat?.ephemeral) return toast('Attachments are disabled in incognito.');
    if (readingAttachments || state.busy || !state.connected) return;
    const accepted = [];
    for (const file of files) {
      if (looksLikeImage(file) && !offers('vision')) { toast('This model cannot read images. Turn on Vision for it in Model settings.'); continue; }
      if (file.size > 25 * 1024 * 1024) { toast('Each attachment must be 25 MB or smaller.'); continue; }
      accepted.push(file);
    }
    if (!accepted.length) return;
    if (pendingAttachments.length + accepted.length > 3) return toast('Attach at most three files per turn.');
    readingAttachments = true; $('attachment-chips').append(el('span', '', 'reading…')); controls();
    try {
      if (!state.chat) state.chat = await json('/chats', write('POST', {mode: $('mode').value}));
      const chatId = state.chat.id;
      for (const file of accepted) {
        const form = new FormData(); form.append('file', file);
        pendingAttachments.push(await json(`/chats/${chatId}/attachments`, {method: 'POST', body: form}));
      }
    } catch (error) { toast(error.message); }
    finally { readingAttachments = false; renderAttachments(); controls(); }
  }
  $('attach').onclick = () => $('attachment-input').click();
  $('attachment-input').onchange = event => { attachFiles([...event.target.files]); event.target.value = ''; };
  $('composer').addEventListener('dragover', event => { event.preventDefault(); });
  $('composer').addEventListener('drop', event => { if (!event.dataTransfer.files.length) return; event.preventDefault(); attachFiles([...event.dataTransfer.files]); });
  $('prompt').addEventListener('paste', event => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) { event.preventDefault(); attachFiles(files); }
  });
  $('composer').addEventListener('submit', send);
  $('prompt').addEventListener('input', () => { resize(); commandPicker(); });
  $('prompt').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $('composer').requestSubmit(); }
    // Like a shell: Up in an empty box brings back what you last said.
    if (event.key === 'ArrowUp' && !$('prompt').value) {
      const last = (state.chat?.messages || []).filter(msg => msg.role === 'user' && typeof msg.content === 'string').at(-1);
      if (last) { event.preventDefault(); $('prompt').value = last.content; resize(); }
    }
  });
  $('stop').onclick = stop; $('new-chat').onclick = newChat; $('new-incognito').onclick = () => safe(newIncognito);
  $('queue').onclick = () => safe(() => queueMessage($('prompt').value.trim()));
  $('steer').onclick = () => safe(steerMessage);
  $('menu').onclick = () => toggleSidebar();
  try { if (localStorage.getItem('kova-sidebar') === 'collapsed') document.body.classList.toggle('sidebar-collapsed', true); } catch (_) {}
  menuState(); wide?.addEventListener?.('change', () => { sidebar(false); });
  $('backdrop').onclick = () => sidebar(false); $('sidebar-close').onclick = () => sidebar(false);
  if (/Mac|iPhone|iPad/.test(globalThis.navigator?.platform || '')) {
    document.querySelectorAll('[data-mod]').forEach(key => { key.textContent = '⌘'; });
    document.querySelectorAll('[data-shift]').forEach(key => { key.textContent = '⇧'; });
  }
  $('open-shortcuts').onclick = () => { sidebar(false); $('shortcuts-dialog').showModal(); };
  $('shortcuts-close').onclick = () => $('shortcuts-dialog').close();
  // Command palette (#183). palette.js owns the dialog; this is only the registry,
  // so every menu action below stays reachable from the keyboard too.
  const paletteMac = /Mac|iPhone|iPad/.test(globalThis.navigator?.platform || '');
  const shortcutKey = (...keys) => [paletteMac ? '⌘' : 'Ctrl',
    ...keys.map(key => key === 'Shift' ? (paletteMac ? '⇧' : 'Shift') : key)];
  let paletteChats = [];
  function paletteItems() {
    const items = [];
    const add = (group, item) => items.push({group, ...item});
    const current = state.chat && !state.chat.ephemeral ? state.chat : null;
    const panel = (button, title, hint, keywords) => ({title, hint, keywords, run: () => $(button).onclick?.()});
    add('Actions', {title:'New chat', hint:'Start with an empty message box', keywords:'create start fresh compose', shortcut:shortcutKey('Shift', 'K'), run:() => safe(newChat)});
    add('Actions', {title:'New incognito chat', hint:'Not saved on this PC', keywords:'private ephemeral temporary', shortcut:shortcutKey('Shift', 'N'), run:() => safe(newIncognito)});
    add('Actions', {title:'Search conversations', hint:'Filter the conversation list', keywords:'find history chats', shortcut:shortcutKey('Shift', 'F'), run:() => { toggleSidebar(true); $('search').focus(); $('search').select?.(); }});
    add('Actions', {title:'Keyboard shortcuts', hint:'Every key this window understands', keywords:'keys help cheat sheet', shortcut:shortcutKey('/'), run:() => $('shortcuts-dialog').showModal()});
    add('Actions', {title:'Show or hide the sidebar', hint:'Give the conversation more room', keywords:'drawer panel collapse', shortcut:shortcutKey('B'), run:() => toggleSidebar()});
    if (state.busy) add('Actions', {title:'Stop the reply', hint:'Cancel the turn that is running now', keywords:'cancel halt interrupt', shortcut:['Esc'], run:() => safe(stop)});
    add('Actions', {title:'Sign out of this window', hint:'End this browser session', keywords:'logout sign out leave disconnect', run:() => $('disconnect').onclick?.()});

    add('Appearance', {title:`Switch to ${document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'} theme`, hint:'Light and dark', keywords:'theme colour color mode appearance', shortcut:shortcutKey('Shift', 'L'), run:() => $('theme').onclick?.()});

    if (current) {
      add('This conversation', {title:'Compact now', hint:'Summarise older turns to free context', keywords:'summarise summarize shorten context', run:() => safe(compactChat)});
      add('This conversation', {title:'Fork this conversation', hint:'Continue in an independent copy', keywords:'copy duplicate branch', run:() => safe(() => forkChat(current.id))});
      add('This conversation', {title:'Rename this conversation', hint:current.title || 'New conversation', keywords:'title name edit', run:() => safe(() => editChat(current, false))});
      add('This conversation', {title:'Delete this conversation', hint:'Remove it from this computer', keywords:'remove trash discard', run:() => safe(() => editChat(current, true))});
      if (!state.busy && !$('mode').disabled) {
        const nextMode = $('mode').value === 'chat' ? 'work' : 'chat';
        add('This conversation', {title:`Switch to ${nextMode} mode`, hint:nextMode === 'work' ? 'Tools, files and memory' : 'Conversation only', keywords:'mode chat work tools', run:() => setMode(nextMode)});
      }
      if (!$('web').disabled) {
        const on = webOn();
        add('This conversation', {title:`Turn web search ${on ? 'off' : 'on'}`, hint:'For this conversation', keywords:'internet browse search web', run:() => $('web').onclick?.()});
      }
      if (offers('approvals') && !$('permission').disabled) {
        for (const [mode, hint] of [['manual', 'Ask before commands and file changes'], ['auto', 'Ask only before destructive commands'], ['full', 'Never ask. Use with care.']]) {
          add('Permissions', {title:`Permission: ${mode === 'full' ? 'Full access' : mode[0].toUpperCase() + mode.slice(1)}`, hint, keywords:`permission approvals ${mode}`, run:() => { $('permission').value = mode; $('permission').onchange?.(); }});
        }
      }
    }

    for (const [button, title, hint, keywords] of [
      ['open-settings', 'Settings', 'Every setting, in one place', 'preferences options configure'],
      ['open-memory', 'Memory', 'Browse and search remembered notes', 'notes recall remember'],
      ['open-reminders', 'Reminders', 'Everything scheduled to remind you', 'alarms schedule'],
      ['open-extensions', 'Extensions', 'Installed plugins and skills', 'plugins skills add-ons'],
      ['open-privacy', 'Privacy', 'What left this computer, and Local only', 'report local only network'],
      ['open-mcp', 'MCP servers', 'Tools from Model Context Protocol servers', 'mcp tools servers'],
      ['open-sounds', 'Sounds', 'Quiet cues for replies and reminders', 'sound audio cue volume chime'],
      ['add-provider', 'Add provider', 'Connect another model provider', 'model server api key'],
      ['connect-phone', 'Connect your phone', 'Pair a phone on the same Wi-Fi', 'pair qr device mobile'],
    ]) add('Settings', panel(button, title, hint, keywords));
    add('Settings', {title:'Install & notifications', hint:'Add Kova to your device and choose alerts', keywords:'pwa install notifications alerts', run:() => $('install-settings').onclick?.()});
    add('Settings', {title:'Startup settings', hint:'Keep reminders running after you log out', keywords:'start at sign in autostart login boot', run:() => window.KovaSettings?.open?.('general')});
    add('Settings', {title:'Model settings…', hint:'Vision and the active model', keywords:'model vision context', run:() => safe(openModelSettings)});
    add('Settings', {title:'Permission rules…', hint:'Pre-approve tools with patterns', keywords:'rules allow deny approvals', run:() => safe(openRules)});

    for (const model of state.models) {
      if (state.busy) break;
      add('Models', {title:`Use ${model.model || model.name}`, hint:model.provider_name || 'Custom provider', keywords:`model provider ${model.name}`, run:() => { $('model').value = model.name; $('model').onchange?.(); }});
    }
    for (const chat of paletteChats.slice(0, 50)) {
      if (state.busy) break;
      add('Conversations', {
        title: chat.ephemeral ? 'Incognito conversation' : chat.title || 'New conversation',
        hint: chat.ephemeral ? 'Incognito · not saved on this PC'
          : [chat.mode === 'chat' ? 'Chat' : 'Work', chat.state && chat.state !== 'idle' ? String(chat.state).replaceAll('-', ' ') : ''].filter(Boolean).join(' · '),
        keywords: 'chat conversation history',
        run: () => safe(() => openChat(chat.id)),
      });
    }
    return items;
  }
  const commandPalette = globalThis.KovaPalette?.create?.({items: paletteItems, onError: error => toast(error.message)}) || null;
  async function openPalette() {
    if (!state.connected || !commandPalette) return;
    closeMenu();
    commandPalette.open();
    try {
      const chats = await json('/chats');
      if (Array.isArray(chats)) paletteChats = chats;
    } catch (_) { /* the palette still offers commands if history cannot load */ }
    commandPalette.refresh();
  }
  const typingIn = node => Boolean(node) && (/^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName || '') || node.isContentEditable);
  document.addEventListener('keydown', event => {
    if (event.isComposing || document.querySelectorAll('dialog[open]').length) return;  // dialogs keep their own keys
    if (document.body.classList.contains('settings-open')) return;  // the Settings view owns the keyboard
    const mod = event.metaKey || event.ctrlKey, key = (event.key || '').toLowerCase();
    if (mod && event.shiftKey && key === 'n') { event.preventDefault(); safe(newIncognito); return; }
    // Ctrl/Cmd+K is the palette now; new conversation moved to Ctrl/Cmd+Shift+K
    // so the two never fight over the same chord (docs/SHORTCUTS.md).
    if (mod && !event.shiftKey && key === 'k') { event.preventDefault(); safe(openPalette); return; }
    if (mod && event.shiftKey && key === 'k') { event.preventDefault(); if (state.connected) safe(newChat); return; }
    if (mod && !event.shiftKey && key === 'b') toggleSidebar();
    else if (mod && event.shiftKey && key === 'f') { toggleSidebar(true); $('search').focus(); }
    else if (mod && event.shiftKey && key === 'l') $('theme').onclick();
    else if (mod && key === '/') $('shortcuts-dialog').showModal();
    // Like the terminal: Ctrl/Cmd+Space focuses the composer to type.
    else if (mod && key === ' ') { event.preventDefault(); $('prompt').focus(); return; }
    else if (key === 'escape') {
      if (document.body.classList.contains('sidebar-open')) sidebar(false);
      else if (state.busy) safe(stop);
      else return;
    } else if (!mod && !event.altKey && !typingIn(event.target) && key === '/') $('prompt').focus();
    else if (!mod && !event.altKey && !typingIn(event.target) && key === '?') $('shortcuts-dialog').showModal();
    else return;
    event.preventDefault();
  });
  let searchTimer;
  $('search').addEventListener('input', () => { clearTimeout(searchTimer); ++state.searchVersion; searchTimer = setTimeout(() => safe(refreshChats), 200); });
  async function chatSetting(value) {
    if (!state.chat) return;
    const chat = await json(`/chats/${state.chat.id}`, write('PATCH', value));
    Object.assign(state.chat, chat); await refreshChats();
  }
  $('mode').onchange = () => safe(async () => {
    try { await chatSetting({mode: $('mode').value}); controls(); }
    catch (error) { $('mode').value = state.chat?.mode || 'work'; throw error; }
  });
  const openRules = () => safe(async () => {
    const dialog = el('dialog', 'rules-dialog');
    const title = el('h2', '', 'Permission rules');
    const help = el('p', '', 'Deny beats ask beats allow. Patterns are case-sensitive globs. Session rules expire on restart. Exact grants include all arguments and the working folder.');
    const label = el('label', '', 'Rules (JSON array: action, tool, pattern, scope, optional message)');
    const input = el('textarea'); input.setAttribute('aria-label', 'Permission rules JSON');
    input.value = JSON.stringify(await json('/permission-rules'), null, 2);
    label.append(input);
    const actions = el('div', 'dialog-actions');
    const cancel = el('button', 'btn', 'Cancel'), save = el('button', 'btn primary', 'Save rules');
    cancel.onclick = () => dialog.close();
    save.onclick = () => safe(async () => {
      const rules = JSON.parse(input.value);
      await json('/permission-rules', write('PUT', rules)); dialog.close();
    });
    actions.append(cancel, save); dialog.append(title, help, label, actions);
    document.body.append(dialog); dialog.onclose = () => dialog.remove(); dialog.showModal();
  });
  $('permission').onchange = () => safe(async () => {
    try { await chatSetting({permission: $('permission').value}); }
    catch (error) { $('permission').value = state.chat?.permission || 'manual'; throw error; }
  });
  $('cwd').onchange = () => safe(async () => {
    try { await chatSetting({cwd: $('cwd').value.trim()}); }
    catch (error) { $('cwd').value = state.chat?.cwd || ''; throw error; }
  });
  // Web search is a switch in the chat options; its state lives in aria-checked.
  function webOn() { return $('web').getAttribute('aria-checked') === 'true'; }
  function setWeb(on) { $('web').setAttribute('aria-checked', String(on)); $('web').lastElementChild.textContent = on ? 'On' : 'Off'; }
  $('web').onclick = () => safe(async () => {
    const web = !webOn();
    await chatSetting({web}); setWeb(web);
  });
  let selectedModel = '';
  $('model').onchange = () => safe(async () => {
    try { await json('/models/active', write('POST', {name: $('model').value})); selectedModel = $('model').value; await health(); await refreshCapabilities(selectedModel); }
    catch (error) { $('model').value = selectedModel; throw error; }
  });
  async function openModelSettings() {
    const name = $('model').value;
    const model = state.models.find(entry => entry.name === name);
    if (!model) return toast('Choose a model first.');
    const dialog = el('dialog', 'model-dialog');
    dialog.setAttribute('aria-label', 'Model settings');
    const form = el('form'); form.method = 'dialog';
    const head = el('div', 'dialog-head'), title = el('h2', '', 'Model settings');
    const close = iconButton('x', 'Close model settings'); close.type = 'button'; close.onclick = () => dialog.close();
    head.append(title, close);
    const intro = el('p', 'provider-intro', `${model.provider_name || 'Custom'} · ${model.model || model.name}`);
    const fields = el('div', 'model-fields');
    for (const [label, value] of [['Provider', model.provider_name || 'Custom'], ['Model', model.model || model.name],
                                  ['Context', `${model.context} tokens`], ['Address', model.base_url]]) {
      const row = el('div', 'field'); row.append(el('span', '', label), el('small', '', value)); fields.append(row);
    }
    const vision = el('input'); vision.type = 'checkbox'; vision.checked = Boolean(model.vision);
    const toggle = el('label', 'provider-check'); toggle.append(vision, el('span', '', 'Vision: this model can read images'));
    const note = el('p', 'provider-footnote', 'Turn this on only when the model itself supports images. Kova never detects this for you.');
    const actions = el('div', 'dialog-actions');
    const cancel = el('button', 'btn quiet', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => dialog.close();
    const save = el('button', 'btn primary', 'Save'); save.type = 'button';
    save.onclick = () => safe(async () => {
      save.disabled = true;
      try {
        const updated = await json('/models', write('PATCH', {name, vision: vision.checked}));
        Object.assign(model, updated);
        if ($('model').value === name) await refreshCapabilities(name);
        dialog.close();
        toast(`Vision ${updated.vision ? 'on' : 'off'} for ${model.model || model.name}.`);
      } catch (error) { save.disabled = false; throw error; }
    });
    actions.append(cancel, save); form.append(head, intro, fields, toggle, note, actions);
    dialog.append(form); document.body.append(dialog); dialog.onclose = () => dialog.remove(); dialog.showModal();
  }
  // The Settings view links here without owning the dialog; keep one stable
  // trigger so the view never reaches into this closure.
  $('open-model-settings').onclick = () => safe(openModelSettings);
  function theme(value) {
    document.documentElement.dataset.theme = value;
    const next = value === 'light' ? 'Dark theme' : 'Light theme';
    $('theme').lastElementChild.textContent = next; $('theme').title = next;
    $('theme-icon').className = `icon i-${value === 'light' ? 'moon' : 'sun'}`;
    // The desktop shell follows this colour for its window background.
    $('theme-color').setAttribute('content', value === 'light' ? '#faf9f7' : '#0d0d10');
    try { localStorage.setItem('kova-theme', value); } catch (_) {}
  }
  $('theme').onclick = () => theme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  try { theme(localStorage.getItem('kova-theme') || 'dark'); } catch (_) { theme('dark'); }
  $('disconnect').onclick = () => safe(async () => { await discardIncognito(); await json('/app/logout', {method: 'POST'}); setChat(null); $('chat-list').replaceChildren(); disconnected('Disconnected. Open kova-web to reconnect.'); });
  async function setupIdentity() {
    state.persona = await json('/persona');
    if (!state.persona.configured) {
      const dialog = el('dialog');
      const form = el('form'); form.method = 'dialog';
      const title = el('h2', '', 'Meet your assistant');
      const nameLabel = el('label', '', 'Name');
      const name = el('input'); name.value = 'Kova'; name.required = true; name.maxLength = 40;
      nameLabel.append(name);
      const toneLabel = el('label', '', 'Tone'); const tone = el('select');
      for (const value of ['warm', 'concise', 'professional', 'playful']) {
        const option = el('option', '', value[0].toUpperCase() + value.slice(1)); option.value = value; tone.append(option);
      }
      toneLabel.append(tone);
      const error = el('p'); error.setAttribute('role', 'alert');
      const submit = el('button', 'btn', 'Continue'); submit.type = 'submit';
      form.append(title, nameLabel, toneLabel, error, submit); dialog.append(form); document.body.append(dialog);
      dialog.addEventListener('cancel', event => event.preventDefault());
      await new Promise(resolve => {
        form.addEventListener('submit', async event => {
          event.preventDefault(); submit.disabled = true;
          try { state.persona = await json('/persona', write('POST', {name: name.value, tone: tone.value})); dialog.close(); resolve(); }
          catch (err) { error.textContent = err.message; submit.disabled = false; }
        });
        dialog.showModal(); name.focus();
      });
      dialog.remove();
    }
    pageTitle();
  }

  // The /events stream carries reminders and chat states. A non-200 reply (a 401
  // after the server restarted) closes an EventSource for good, and a sleeping
  // phone can hold a stream that silently died, so watch it, say so honestly,
  // and reconnect with backoff. The server pings every 15 s.
  const STALE_MS = 45000;
  const live = {retry: 0, timer: null, seen: 0, pinged: false, lost: false};
  function connectEvents() {
    clearTimeout(live.timer); state.events?.close();
    const source = new EventSource('/events'); state.events = source;
    const alive = () => { if (state.events === source) live.seen = Date.now(); };
    source.addEventListener('open', () => {
      if (state.events !== source) return;
      alive(); live.retry = 0;
      if (live.lost) { live.lost = false; safe(resync); }
    });
    source.addEventListener('ping', () => { live.pinged = true; alive(); });
    source.addEventListener('conversation_state', event => { alive(); try { const update = JSON.parse(event.data); const chat = state.chats.find(c => c.id === update.chat_id); if (chat) { chat.state = update.state; history(state.chats); } else safe(refreshChats); if (state.chat?.id === update.chat_id) { const previous = state.chat.state; state.chat.state = update.state; chatStateChanged(update.chat_id, update.state, previous); } } catch (_) {} });
    source.addEventListener('reminder', event => { alive(); try { const reminder = JSON.parse(event.data); toast(reminder.text, 'Reminder'); notifyReminder(reminder); window.KovaSounds?.play('reminder'); } catch (_) {} });
    source.onerror = () => {
      if (state.events !== source || !state.connected) return;
      // CONNECTING means the browser is already retrying a dropped stream itself.
      streamLost(source.readyState === EventSource.CLOSED);
    };
  }
  function streamLost(closed) {
    // Reconnecting is routine and quiet; only a sign-out (401) is ever shown.
    live.lost = true;
    if (!closed) return;
    state.events?.close(); clearTimeout(live.timer);
    const delay = Math.min(30000, 1000 * 2 ** live.retry++);
    live.timer = setTimeout(() => safe(reconnectEvents), delay);
  }
  async function reconnectEvents() {
    if (!state.connected) return;
    let response = null;
    try { response = await fetch('/chats', {credentials: 'same-origin'}); } catch (_) {}
    if (!state.connected) return;
    // Browser sessions live in server memory, so a restarted server forgot this window.
    if (response?.status === 401) return disconnected('Kova restarted, so this window was signed out. Open kova-web on this computer to reconnect.');
    if (!response?.ok) return streamLost(true);
    connectEvents();
  }
  // Anything missed while the stream was down: chat states, the open chat, model status.
  async function resync() {
    await refreshChats();
    if (state.chat?.id && !state.busy) await openChat(state.chat.id);
    await health();
  }
  function checkStream() {
    if (!state.connected || !state.events) return;
    const stale = live.pinged && Date.now() - live.seen > STALE_MS;
    if (state.events.readyState === EventSource.CLOSED || stale) streamLost(true);
  }
  async function start() {
    if (state.starting) return;
    state.starting = true; state.events?.close();
    // Strip before any asynchronous work, so a consumed ticket never stays in history.
    const fragment = new URLSearchParams(location.hash.slice(1));
    const ticket = fragment.get('ticket'), pairing = fragment.get('pair'), share = fragment.get('share');
    historyReplace();
    try {
      if (ticket) await json('/app/login', write('POST', {ticket}));
      // A phone that scanned a pairing QR code trades its one-use secret for a device cookie.
      if (pairing) { await json('/app/pair', write('POST', {secret: pairing})); toast('This phone is now paired with Kova.'); }
      await json('/chats'); state.connected = true; clearNotice();
      if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) navigator.serviceWorker.register('/app/sw.js', {scope: '/app'}).catch(() => {});
      await setupIdentity();
      const [models, status] = await Promise.all([json('/models'), json('/health')]);
      state.models = models;
      renderModels(models);
      $('model').value = status.model; selectedModel = status.model;
      // The contract belongs to the model in the composer, not just the active one.
      await refreshCapabilities(selectedModel);
      await refreshChats(); controls(); await health();
      live.lost = false; live.retry = 0; live.pinged = false; connectEvents();
      if (share) {
        const local = await readWorkerShare(share);
        const draft = local || await json(`/app/share/${encodeURIComponent(share)}`, {method: 'POST'});
        await applyShare(draft);
      }
      $('prompt').focus();
    } catch (error) { disconnected(error instanceof TypeError ? 'Kova is offline. Reconnect to your computer to load chats and reminders.' : error.message); }
    finally { state.starting = false; }
  }
  function renderModels(models) {
    const groups = new Map();
    for (const model of models) {
      const provider = model.provider_name || 'Custom';
      if (!groups.has(provider)) { const group = el('optgroup'); group.label = provider; groups.set(provider, group); }
      const option = el('option', '', model.model || model.name);
      option.value = model.name; option.title = model.name;
      groups.get(provider).append(option);
    }
    $('model').replaceChildren(...groups.values());
    syncMenus();
  }

  let localRows = [], localController = null, localBackend = 'ollama';
  const localSize = value => value == null ? 'unknown' : `${(value / 1073741824).toFixed(1)} GiB`;
  function localChoiceChanged() {
    const row = localRows.find(r => r.id === $('local-model-choice').value);
    $('local-model-reason').textContent = row ? `${row.rating.replaceAll('-', ' ')}: ${row.reason}` : 'No model fits. Free disk/RAM or choose explicit CPU mode.';
    $('local-model-size').textContent = row ? `Download about ${localSize(row.download_bytes)}${row.engine_download_bytes ? ` + ${localSize(row.engine_download_bytes)} engine` : ''} · ${row.quant} · ${row.context} token context` : '';
    $('local-license').hidden = !row;
    if (row) $('local-license').href = row.license_url;
    $('local-install').disabled = !row || row.rating === 'too-big';
  }
  async function openLocalModels() {
    const version = ++providerVersion;
    clearTimeout(providerTimer);
    if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {});
    providerLogin = null;
    clearProviderSecrets(); lockProvider(true);
    $('provider-details').hidden = true; $('provider-models-step').hidden = true;
    $('provider-local').hidden = true; $('provider-next').hidden = true;
    $('local-model-step').hidden = false; $('provider-back').hidden = false;
    $('local-progress').hidden = true; $('local-install').disabled = true;
    $('provider-status').textContent = 'Checking hardware and local backends…';
    try {
      const data = await json(`/local-models?cpu=${$('local-cpu').checked ? 'true' : 'false'}`);
      if (version !== providerVersion) return;
      const h = data.hardware;
      $('local-hardware').textContent = `${h.gpu || 'No GPU detected'} · ${localSize(h.vram_bytes)} VRAM · ${localSize(h.ram_bytes)} RAM · ${localSize(h.free_disk)} free`;
      $('local-disk-note').textContent = data.disk_note;
      $('local-fit-details').replaceChildren(...data.models.map(row =>
        el('li', '', `${row.name} · ${row.rating.replaceAll('-', ' ')}: ${row.reason}`)));
      localBackend = data.backend || (data.ollama_present ? 'ollama' : 'managed');
      localRows = data.models;
      $('local-model-choice').replaceChildren(...localRows.map(row => {
        const option = el('option', '', `${row.name} · ${row.rating.replaceAll('-', ' ')}`);
        option.value = row.id; option.disabled = row.rating === 'too-big'; return option;
      }));
      $('local-model-choice').value = localRows.find(r => r.rating !== 'too-big')?.id || '';
      localChoiceChanged();
      $('provider-status').textContent = data.engine_error || (localBackend === 'ollama'
        ? 'Choose a model, review its licence and download size, then download.'
        : 'Ollama is not running. Download will install a pinned llama.cpp engine plus the GGUF. Kova manages startup and idle unload. Alternatively install Ollama from https://ollama.com/download and run ollama serve, then recheck.');
      $('local-installed').replaceChildren(...data.installed.map(model => {
        const row = el('div', 'local-installed-row'), button = el('button', 'btn quiet', 'Remove');
        button.type = 'button'; button.setAttribute('aria-label', `Remove ${model.name}`);
        button.onclick = () => safe(async () => {
          if (!confirm(model.backend === 'managed'
            ? 'Remove this GGUF and its Kova connection? The file is kept if another managed variant uses it.'
            : 'Remove this model from Ollama and Kova? Other apps using its source tag may be affected. Shared layers may remain.')) return;
          button.disabled = true;
          try {
            const result = await json(`/local-models/${encodeURIComponent(model.provider)}`, {method: 'DELETE'});
            await refreshLocalSelection();
            if (version !== providerVersion) return;
            await openLocalModels(); $('provider-status').textContent = result.message;
          } catch (error) { $('provider-status').textContent = error.message; button.disabled = false; }
        });
        row.append(el('span', '', model.name), button); return row;
      }));
    } catch (error) {
      if (version === providerVersion) $('provider-status').textContent = error.message;
    }
  }
  async function refreshLocalSelection() {
    const [models, status] = await Promise.all([json('/models'), json('/health')]);
    state.models = models; renderModels(models); $('model').value = status.model; selectedModel = status.model;
    await refreshCapabilities(); syncMenus(); await health();
  }
  async function installLocalModel() {
    if (localController || $('local-install').disabled) return;
    const version = ++providerVersion;
    const controller = new AbortController(); localController = controller;
    $('local-install').disabled = true; $('local-cpu').disabled = true; $('local-model-choice').disabled = true; $('local-recheck').disabled = true;
    $('local-stop').hidden = false; $('provider-back').disabled = true;
    $('local-installed').querySelectorAll('button').forEach(button => { button.disabled = true; });
    $('local-progress').hidden = false; $('local-progress').removeAttribute('value');
    $('provider-status').textContent = 'Starting download…';
    let complete = false;
    try {
      const response = await api('/local-models/install', {...write('POST', {model: $('local-model-choice').value, cpu: $('local-cpu').checked, backend: localBackend}), signal: controller.signal});
      await readEvents(response, event => {
        if (version !== providerVersion) return;
        if (event.type === 'error') throw new Error(event.message);
        if (event.type === 'done') complete = true;
        if (event.type === 'progress') {
          $('provider-status').textContent = event.message;
          if (event.total) { $('local-progress').max = event.total; $('local-progress').value = event.completed; }
          else $('local-progress').removeAttribute('value');
        }
      });
      if (version !== providerVersion) return;
      if (!complete) throw new Error('Download stream ended before completion. Retry to resume. No ready model was confirmed.');
      await refreshLocalSelection(); closeProvider(); toast('Model ready on this PC. Start chatting.');
    } catch (error) {
      if (version === providerVersion) $('provider-status').textContent = controller.signal.aborted
        ? 'Download cancelled. Retry to resume partial downloads. Check the model picker if setup had already finished.' : error.message;
    } finally {
      localController = null;
      $('local-stop').hidden = true; $('provider-back').disabled = false;
      $('local-cpu').disabled = false; $('local-model-choice').disabled = false; $('local-recheck').disabled = false;
      $('local-installed').querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (version === providerVersion) localChoiceChanged();
    }
  }
  $('provider-local').onclick = () => safe(openLocalModels);
  $('local-cpu').onchange = () => safe(openLocalModels);
  $('local-recheck').onclick = () => safe(openLocalModels);
  $('local-model-choice').onchange = localChoiceChanged;
  $('local-install').onclick = installLocalModel;
  $('local-stop').onclick = () => localController?.abort();

  let providerSpecs = [], providerDraft = null, providerChoices = [], providerLogin = null, providerTimer = null;
  let providerVersion = 0;
  function lockProvider(flag) {
    $('provider-details').querySelectorAll('input,select,textarea').forEach(field => { field.disabled = flag; });
    syncMenus();
  }
  function clearProviderSecrets() {
    $('provider-key').value = ''; $('provider-headers').value = '';
    providerDraft = null;
  }
  function closeProvider() {
    localController?.abort();
    providerVersion++; clearTimeout(providerTimer);
    if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {});
    providerLogin = null; clearProviderSecrets(); providerChoices = [];
    $('provider-dialog').close();
  }
  function providerSpec() {
    return providerSpecs.find(p => p.id === $('provider-kind').value) || {};
  }
  function providerCaps() {
    return providerSpec().capabilities || [];
  }
  function providerKindChanged() {
    clearProviderSecrets();
    const spec = providerSpec();
    if (!spec.id) return;
    const caps = providerCaps();
    $('provider-name').value = spec.id;
    // Every field follows the kind's declared capabilities, never its name.
    $('provider-url-row').hidden = !caps.includes('editable_base_url');
    $('provider-url').value = ''; $('provider-url').placeholder = spec.base_url_hint || 'https://example.com/v1';
    $('provider-key-row').hidden = !caps.includes('api_key');
    $('provider-key').required = Boolean(spec.key_url);
    $('provider-headers-row').hidden = !caps.includes('custom_headers');
    $('provider-consent-row').hidden = !caps.includes('harness_login'); $('provider-consent').checked = false;
    $('provider-key-link').hidden = !spec.key_url; $('provider-key-link').href = spec.key_url || '#';
    $('provider-next').textContent = caps.includes('browser_login') ? 'Continue with ChatGPT' : 'List models';
    $('provider-status').textContent = '';
    syncMenus();
  }
  function providerActiveOptions() {
    const previous = $('provider-active').value;
    const selected = providerChoices.filter(c => c.input.checked);
    $('provider-active').replaceChildren(...selected.map(c => { const option = el('option', '', c.model); option.value = c.model; return option; }));
    if (selected.some(c => c.model === previous)) $('provider-active').value = previous;
    $('provider-next').disabled = !selected.length;
    syncMenus();
  }
  function showProviderModels(models) {
    if (!models.length) throw new Error('No models listed. Check access or load a model first.');
    providerChoices = models.map((model, index) => {
      const label = el('label', 'provider-check'), input = el('input');
      input.type = 'checkbox'; input.checked = index === 0;
      label.append(input, el('span', '', model));
      input.onchange = providerActiveOptions;
      return {model, input, label};
    });
    $('provider-models').replaceChildren(...providerChoices.map(c => c.label));
    $('provider-details').hidden = true; $('provider-models-step').hidden = false;
    $('provider-back').hidden = false; $('provider-next').textContent = 'Connect provider';
    $('provider-filter').value = ''; providerActiveOptions();
    $('provider-status').textContent = `${models.length} models available. Choose one or more.`;
    $('provider-filter').focus();
  }
  $('provider-filter').oninput = () => {
    const query = $('provider-filter').value.toLowerCase();
    for (const choice of providerChoices) choice.label.hidden = !choice.model.toLowerCase().includes(query);
  };
  $('add-provider').onclick = () => safe(async () => {
    const data = await json('/providers/catalog'); providerSpecs = data.providers;
    $('provider-kind').replaceChildren(...providerSpecs.map(spec => {
      const option = el('option', '', spec.name + (spec.id === 'codex' && !data.harness_available ? ' (not found)' : ''));
      option.value = spec.id; return option;
    }));
    $('provider-kind').value = 'openai'; $('provider-details').hidden = false;
    $('provider-local').hidden = false; $('provider-next').hidden = false; $('local-model-step').hidden = true; $('local-cpu').checked = false;
    $('provider-models-step').hidden = true; $('provider-back').hidden = true;
    $('provider-next').disabled = false; lockProvider(false); providerKindChanged(); sidebar(false); $('provider-dialog').showModal();
  });
  $('provider-kind').onchange = providerKindChanged;
  $('provider-cancel').onclick = closeProvider;
  $('provider-dialog').addEventListener('cancel', event => { event.preventDefault(); closeProvider(); });
  $('provider-back').onclick = () => {
    if (localController) return;
    $('provider-local').hidden = false; $('provider-next').hidden = false; $('local-model-step').hidden = true;
    providerVersion++; clearTimeout(providerTimer);
    if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {});
    providerLogin = null; clearProviderSecrets(); providerChoices = [];
    $('provider-models-step').hidden = true; $('provider-details').hidden = false;
    $('provider-back').hidden = true; $('provider-next').disabled = false; lockProvider(false); providerKindChanged();
  };
  $('provider-form').onsubmit = async event => {
    event.preventDefault(); if (!$('local-model-step').hidden) return;
    const version = ++providerVersion;
    // Open synchronously with the gesture so browser sign-in is not popup-blocked.
    const signIn = !providerDraft && providerCaps().includes('browser_login');
    const popup = signIn ? window.open('about:blank', '_blank') : null;
    if (popup) popup.opener = null;
    $('provider-next').disabled = true;
    lockProvider(true);
    try {
      if (providerDraft) {
        const selected = providerChoices.filter(c => c.input.checked).map(c => c.model);
        await json('/providers', write('POST', {...providerDraft, models: selected, active: $('provider-active').value}));
        if (version !== providerVersion) return;
        providerLogin = null; closeProvider();
        const [models, status] = await Promise.all([json('/models'), json('/health')]);
        state.models = models; renderModels(models); $('model').value = status.model; selectedModel = status.model;
        await refreshCapabilities(selectedModel);
        syncMenus(); await health(); toast('Provider connected. Ready when you are.'); return;
      }
      const kind = $('provider-kind').value, caps = providerCaps();
      const body = {kind, id: $('provider-name').value, name: $('provider-name').value,
        api_key: $('provider-key').value || null, consent: $('provider-consent').checked};
      if (caps.includes('editable_base_url')) body.base_url = $('provider-url').value;
      if (caps.includes('custom_headers') && $('provider-headers').value.trim()) {
        try { body.headers = JSON.parse($('provider-headers').value); }
        catch (_) { throw new Error('Custom headers must be a JSON object.'); }
      }
      $('provider-status').textContent = signIn ? 'Finish sign-in in your browser…' : 'Listing models…';
      if (signIn) {
        const login = await json('/providers/auth/start', write('POST', {}));
        if (version !== providerVersion) { api(`/providers/auth/${login.login_id}`, {method: 'DELETE'}).catch(() => {}); popup?.close(); return; }
        providerLogin = login.login_id; body.login_id = providerLogin;
        if (popup) popup.location = login.url;
        else {
          const link = el('a', '', 'Continue with ChatGPT'); link.href = login.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          $('provider-status').replaceChildren(link);
        }
        const poll = async () => {
          try {
            const status = await json(`/providers/auth/${providerLogin}`);
            if (version !== providerVersion) return;
            if (!status.complete) { providerTimer = setTimeout(poll, 1500); return; }
            const data = await json('/providers/validate', write('POST', body));
            if (version !== providerVersion) return;
            showProviderModels(data.models); providerDraft = body; clearProviderFieldValues();
          } catch (error) { if (version === providerVersion) { $('provider-status').textContent = error.message; $('provider-next').disabled = false; lockProvider(false); if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {}); providerLogin = null; } }
        };
        await poll(); return;
      }
      const data = await json('/providers/validate', write('POST', body));
      if (version !== providerVersion) return;
      showProviderModels(data.models); providerDraft = body; clearProviderFieldValues();
    } catch (error) {
      popup?.close(); if (version === providerVersion) $('provider-status').textContent = error.message;
    } finally { if (version === providerVersion && providerDraft) providerActiveOptions();
      if (version === providerVersion && !providerDraft && (!signIn || !providerLogin)) { $('provider-next').disabled = false; lockProvider(false); } }
  };
  function clearProviderFieldValues() { $('provider-key').value = ''; $('provider-headers').value = ''; }
  // Keep these API-backed panels in the client; credentials never enter storage.
  for (const kind of ['memory', 'reminders']) {
    const launch = $(`open-${kind}`), name = kind === 'memory' ? 'Memory' : 'Reminders';
    const dialog = el('dialog', 'panel'); dialog.setAttribute('aria-label', name);
    const head = el('div', 'dialog-head'), close = iconButton('x', `Close ${name.toLowerCase()}`);
    close.onclick = () => dialog.close(); head.append(el('h2', '', name), close);
    const intro = el('p', 'panel-intro', kind === 'memory' ? 'Notes Kova keeps on this computer and can recall later.' : 'Kova will nudge you here when a reminder is due.');
    const search = el('input'); search.type = 'search'; search.placeholder = 'Search memory'; search.setAttribute('aria-label', 'Search memory');
    const list = el('div', 'panel-list'), form = el('form', 'panel-form'), title = el('input'), body = el('textarea');
    title.required = true; body.required = true; body.rows = 2;
    title.placeholder = kind === 'memory' ? 'Title' : 'When (e.g. in 10 min)';
    title.setAttribute('aria-label', title.placeholder);
    body.placeholder = kind === 'memory' ? 'Note' : 'Reminder'; body.setAttribute('aria-label', body.placeholder);
    const save = el('button', 'btn primary', kind === 'memory' ? 'Add note' : 'Add reminder'); save.type = 'submit';
    form.append(title, body, save); dialog.append(head, intro);
    if (kind === 'memory') dialog.append(search);
    dialog.append(list, form); document.body.append(dialog);
    search.oninput = () => { ++version; safe(refresh); };
    let version = 0;
    async function refresh() {
      const current = ++version, rows = await json(`/${kind}${kind === 'memory' && search.value.trim() ? '?q=' + encodeURIComponent(search.value.trim()) : ''}`);
      if (current !== version) return;
      list.replaceChildren();
      if (!rows.length) list.append(el('p', 'panel-empty', search.value.trim() ? 'Nothing matches that search.' : kind === 'memory' ? 'Nothing here yet. Add a note below, or ask Kova to remember something.' : 'Nothing here yet. Add a reminder below, or ask Kova to remind you.'));
      for (const row of rows) {
        const item = el('section', 'panel-item'), text = el('div'), remove = iconButton('trash-2', 'Delete');
        text.append(el('strong', '', row.title || row.text), el('p', '', kind === 'memory' ? row.body : new Date(row.when || row.reminder_at).toLocaleString()));
        if (kind === 'memory') {
          const from = row.origin === 'model' ? 'Kova noted' : 'you said';
          const when = row.created || row.updated ? new Date(row.created || row.updated).toLocaleDateString() : '';
          text.append(el('p', 'panel-meta', [from, row.source, when].filter(Boolean).join(' · ')));
        }
        item.append(text, remove);
        remove.onclick = () => safe(async () => {
          remove.disabled = true;
          try { await json(`/${kind}/${encodeURIComponent(row.slug ?? row.id)}`, {method: 'DELETE'}); await refresh(); }
          finally { remove.disabled = false; }
        }); list.append(item);
      }
    }
    launch.onclick = () => safe(async () => { sidebar(false); dialog.showModal(); await refresh(); });
    form.onsubmit = event => {
      event.preventDefault(); if (save.disabled) return;
      safe(async () => {
        save.disabled = true;
        try {
          await json(`/${kind}`, write('POST', kind === 'memory' ? {title: title.value, body: body.value} : {when: title.value, text: body.value}));
          title.value = ''; body.value = ''; await refresh();
        } finally { save.disabled = false; }
      });
    };
  }
  // MCP servers (#135): tools from Model Context Protocol servers, each with an
  // on/off switch and an "always allow" switch. Secrets stay out of the browser:
  // tokens go through `kova mcp add --env/--header` or config.toml.
  {
    const launch = $('open-mcp'), enc = encodeURIComponent;
    const dialog = el('dialog', 'panel mcp-dialog'); dialog.setAttribute('aria-label', 'MCP servers');
    const head = el('div', 'dialog-head'), close = iconButton('x', 'Close MCP servers');
    close.onclick = () => dialog.close(); head.append(el('h2', '', 'MCP servers'), close);
    const intro = el('p', 'panel-intro', 'Tools from MCP servers join Kova’s own in work mode. Kova asks before every call unless you switch on Always allow.');
    const list = el('div', 'panel-list'), form = el('form', 'panel-form');
    const name = el('input'), target = el('input'), args = el('textarea');
    name.required = true; name.placeholder = 'Name, e.g. github'; name.maxLength = 32; name.autocomplete = 'off';
    target.required = true; target.placeholder = 'Command (e.g. npx) or https:// address'; target.autocomplete = 'off';
    args.rows = 2; args.placeholder = 'Arguments, one per line (commands only)';
    for (const field of [name, target, args]) field.setAttribute('aria-label', field.placeholder);
    const save = el('button', 'btn primary', 'Add server'); save.type = 'submit';
    form.append(name, target, args, save,
      el('p', 'panel-meta', 'Need a token? Add the server with kova mcp add --env or --header, so it never passes through the browser.'));
    dialog.append(head, intro, list, form); document.body.append(dialog);
    const expanded = new Set();
    let version = 0;
    function toggle(label, checked, change) {
      const wrap = el('label', 'mcp-switch'), box = el('input'); box.type = 'checkbox'; box.checked = checked;
      box.onchange = () => safe(async () => {
        box.disabled = true;
        try { await change(box.checked); }
        catch (error) { box.checked = !box.checked; throw error; }
        finally { box.disabled = false; }
      });
      wrap.append(box, el('span', '', label)); return wrap;
    }
    const patchTool = (server, tool, body) => json(`/mcp/${enc(server)}/tools/${enc(tool)}`, write('PATCH', body)).then(refresh);
    function render(rows) {
      list.replaceChildren();
      if (!rows.length) list.append(el('p', 'panel-empty', 'No MCP servers yet. Add one below, or run kova mcp add in a terminal.'));
      for (const row of rows) {
        const item = el('section', 'panel-item mcp-server'), text = el('div'), actions = el('div', 'mcp-actions');
        const count = `${row.tools.length} tool${row.tools.length === 1 ? '' : 's'}`;
        text.append(el('strong', '', row.name),
          el('p', 'panel-meta', [row.transport, row.state === 'connected' ? count : row.state, row.target].join(' · ')));
        if (row.error) text.append(el('p', 'mcp-error', row.error));
        text.append(toggle('Server on', row.enabled, async on => { await json(`/mcp/${enc(row.name)}`, write('PATCH', {enabled: on})); await refresh(); }));
        if (row.tools.length) {
          const tools = el('details', 'mcp-tools'); tools.open = expanded.has(row.name);
          tools.ontoggle = () => { if (tools.open) expanded.add(row.name); else expanded.delete(row.name); };
          tools.append(el('summary', '', `Tools (${row.tools.filter(tool => tool.enabled).length} on)`));
          for (const tool of row.tools) {
            const line = el('div', 'mcp-tool'), info = el('div');
            info.append(el('code', '', tool.name));
            if (tool.description) info.append(el('p', 'panel-meta', tool.description));
            line.append(info, toggle('On', tool.enabled, on => patchTool(row.name, tool.name, {enabled: on})),
              toggle('Always allow', tool.always_allow, on => patchTool(row.name, tool.name, {always_allow: on})));
            tools.append(line);
          }
          text.append(tools);
        }
        const test = el('button', 'btn quiet', 'Test'); test.type = 'button'; test.title = `Reconnect ${row.name}`;
        test.onclick = () => safe(async () => {
          test.disabled = true; test.textContent = 'Testing…';
          try { const result = await json(`/mcp/${enc(row.name)}/test`, {method: 'POST'}); toast(result.error || `${row.name} is working.`, 'MCP'); await refresh(); }
          finally { test.disabled = false; test.textContent = 'Test'; }
        });
        const remove = iconButton('trash-2', `Remove ${row.name}`);
        remove.onclick = () => safe(async () => {
          if (!confirm(`Remove ${row.name}? Its tools and always-allow switches go with it.`)) return;
          remove.disabled = true;
          try { await json(`/mcp/${enc(row.name)}`, {method: 'DELETE'}); await refresh(); }
          finally { remove.disabled = false; }
        });
        actions.append(test, remove); item.append(text, actions); list.append(item);
      }
    }
    async function refresh() {
      const current = ++version, rows = await json('/mcp');
      if (current === version) render(rows);
    }
    launch.onclick = () => safe(async () => { sidebar(false); dialog.showModal(); await refresh(); });
    form.onsubmit = event => {
      event.preventDefault(); if (save.disabled) return;
      safe(async () => {
        save.disabled = true; save.textContent = 'Connecting…';
        const where = target.value.trim(), url = /^https?:\/\//i.test(where);
        const body = url ? {name: name.value.trim(), url: where}
          : {name: name.value.trim(), command: where, args: args.value.split('\n').map(line => line.trim()).filter(Boolean)};
        try {
          const row = await json('/mcp', write('POST', body));
          if (row.error) toast(row.error, 'MCP');
          name.value = ''; target.value = ''; args.value = ''; expanded.add(row.name); await refresh();
        } finally { save.disabled = false; save.textContent = 'Add server'; }
      });
    };
  }
  // Extensions (#137): the catalog of plugins and skills, and everything that is
  // installed. Installing is preview, then confirm: the server downloads the
  // source, checks its pinned checksum and shows exactly what it adds; nothing
  // is written until Install. Skills show where they came from and, unless a
  // plugin owns them, can be read, edited and deleted here.
  {
    const launch = $('open-extensions'), enc = encodeURIComponent;
    const dialog = el('dialog', 'panel ext-dialog'); dialog.setAttribute('aria-label', 'Extensions');
    const head = el('div', 'dialog-head'), close = iconButton('x', 'Close extensions');
    close.onclick = () => dialog.close(); head.append(el('h2', '', 'Extensions'), close);
    const intro = el('p', 'panel-intro', 'Add abilities to Kova. Nothing runs until you have seen what it adds and said yes.');
    const tabs = el('div', 'segmented ext-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Extensions');
    const search = el('input'); search.type = 'search'; search.placeholder = 'Search extensions'; search.setAttribute('aria-label', 'Search extensions');
    const list = el('div', 'panel-list ext-list'), footer = el('p', 'panel-meta ext-source');
    let tab = 'installed', data = {catalog: null, plugins: [], skills: []}, version = 0;
    const buttons = {};
    for (const [key, label] of [['installed', 'Installed'], ['plugin', 'Plugins'], ['skill', 'Skills']]) {
      const button = el('button', '', label); button.type = 'button'; button.setAttribute('role', 'tab');
      button.onclick = () => { tab = key; render(); };
      buttons[key] = button; tabs.append(button);
    }
    dialog.append(head, intro, tabs, search, list, footer); document.body.append(dialog);
    search.oninput = () => render();
    const ORIGINS = {learned: 'Learned by Kova', catalog: 'From the catalog', yours: 'Added by you', plugin: 'Plugin'};
    const matches = (...parts) => {
      const words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const text = parts.flat().filter(Boolean).join(' ').toLowerCase();
      return words.every(word => text.includes(word));
    };
    function section(parent, title, lines) {
      if (!lines.length) return;
      parent.append(el('p', 'ext-label', title));
      const ul = el('ul', 'ext-lines'); for (const line of lines) ul.append(el('li', '', line)); parent.append(ul);
    }
    // The same list `kova plugin install` prints before it asks.
    function pluginAdds(parent, info) {
      section(parent, 'MCP servers (tools)', info.mcp.map(server => {
        const target = server.url || [server.command, ...server.args].join(' ');
        const notes = [server.url ? 'network access' : '', server.env_keys.length ? 'env: ' + server.env_keys.join(', ') : ''].filter(Boolean);
        return `${server.name} → ${target}${notes.length ? ` (${notes.join('; ')})` : ''}`;
      }));
      section(parent, 'Skills', info.skills.map(skill => skill.description ? `${skill.name} — ${skill.description}` : skill.name));
      section(parent, 'Slash commands', info.commands.map(command => command.description ? `/${command.name} — ${command.description}` : `/${command.name}`));
      section(parent, 'Permission rules', info.permissions.map(rule => `${rule.action} ${rule.tool} ${rule.pattern}`));
      if (!info.mcp.length && !info.skills.length && !info.commands.length && !info.permissions.length) parent.append(el('p', 'panel-meta', 'Adds nothing yet.'));
    }
    function toggle(label, checked, change) {
      const wrap = el('label', 'mcp-switch'), box = el('input'); box.type = 'checkbox'; box.checked = checked;
      box.onchange = () => safe(async () => {
        box.disabled = true;
        try { await change(box.checked); }
        catch (error) { box.checked = !box.checked; throw error; }
        finally { box.disabled = false; }
      });
      wrap.append(box, el('span', '', label)); return wrap;
    }
    function heading(text) { list.append(el('h3', 'ext-heading', text)); }
    function renderPlugin(plugin) {
      const item = el('section', 'panel-item'), text = el('div'), actions = el('div', 'mcp-actions');
      text.append(el('strong', '', `${plugin.name} ${plugin.version}`));
      if (plugin.description) text.append(el('p', '', plugin.description));
      text.append(el('p', 'panel-meta', [plugin.author ? `by ${plugin.author}` : '', plugin.source?.startsWith('catalog:') ? 'from the catalog' : plugin.source].filter(Boolean).join(' · ')));
      const adds = el('details', 'mcp-tools'); adds.append(el('summary', '', 'What it adds')); pluginAdds(adds, plugin);
      text.append(toggle('On', plugin.enabled, async on => { await json(`/extensions/plugins/${enc(plugin.name)}`, write('PATCH', {enabled: on})); await refresh(); }), adds);
      const remove = iconButton('trash-2', `Remove ${plugin.name}`);
      remove.onclick = () => safe(async () => {
        if (!confirm(`Remove ${plugin.name}? Its skills, commands, MCP servers and rules go with it.`)) return;
        remove.disabled = true;
        try { await json(`/extensions/plugins/${enc(plugin.name)}`, {method: 'DELETE'}); await refresh(); }
        finally { remove.disabled = false; }
      });
      actions.append(remove); item.append(text, actions); list.append(item);
    }
    function renderSkill(skill) {
      const item = el('section', 'panel-item'), text = el('div'), actions = el('div', 'mcp-actions');
      const origin = skill.origin === 'plugin' ? `Plugin ${skill.plugin}` : ORIGINS[skill.origin];
      const when = skill.updated ? new Date(skill.updated) : null;
      text.append(el('strong', '', skill.name), el('p', '', skill.description),
        el('p', 'panel-meta', [origin, skill.version ? `v${skill.version}` : '', when && !isNaN(when) ? when.toLocaleDateString() : ''].filter(Boolean).join(' · ')));
      const body = el('details', 'mcp-tools'); body.append(el('summary', '', 'Instructions'), el('pre', 'ext-body', skill.body));
      text.append(body);
      if (skill.editable) {
        const edit = el('button', 'btn quiet', 'Edit'); edit.type = 'button';
        edit.onclick = () => {
          const form = el('form', 'ext-edit'), name = el('input'), description = el('input'), instructions = el('textarea');
          name.value = skill.name; name.maxLength = 60; name.required = true; name.setAttribute('aria-label', 'Skill name');
          description.value = skill.description; description.maxLength = 240; description.required = true; description.setAttribute('aria-label', 'One-line description');
          instructions.value = skill.body; instructions.rows = 8; instructions.required = true; instructions.setAttribute('aria-label', 'Instructions');
          const save = el('button', 'btn primary', 'Save'); save.type = 'submit';
          const cancel = el('button', 'btn quiet', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => render();
          const row = el('div', 'dialog-actions'); row.append(cancel, save);
          form.append(name, description, instructions, row);
          form.onsubmit = event => {
            event.preventDefault(); if (save.disabled) return;
            safe(async () => {
              save.disabled = true;
              try { await json(`/extensions/skills/${enc(skill.id)}`, write('PUT', {name: name.value, description: description.value, body: instructions.value})); await refresh(); }
              finally { save.disabled = false; }
            });
          };
          body.replaceWith(form); edit.disabled = true; name.focus();
        };
        const remove = iconButton('trash-2', `Delete ${skill.name}`);
        remove.onclick = () => safe(async () => {
          if (!confirm(`Delete the skill “${skill.name}”? Kova will no longer see it.`)) return;
          remove.disabled = true;
          try { await json(`/extensions/skills/${enc(skill.id)}`, {method: 'DELETE'}); await refresh(); }
          finally { remove.disabled = false; }
        });
        actions.append(edit, remove);
      }
      item.append(text, actions); list.append(item);
    }
    function renderEntry(entry) {
      const item = el('section', 'panel-item'), text = el('div'), actions = el('div', 'mcp-actions');
      text.append(el('strong', '', `${entry.name} ${entry.version}`));
      if (entry.description) text.append(el('p', '', entry.description));
      text.append(el('p', 'panel-meta', [entry.author ? `by ${entry.author}` : '', entry.tags.join(', ')].filter(Boolean).join(' · ')));
      section(text, 'What it may do', entry.permissions);
      if (entry.status === 'available') text.append(el('p', 'panel-meta ext-code', `Source: ${entry.source.url}`));
      const button = el('button', 'btn quiet'); button.type = 'button';
      const upgrade = !!entry.installed_version;
      if (entry.status !== 'available') { button.textContent = 'Coming soon'; button.disabled = true; }
      else if (entry.installed_version && !entry.update_available) { button.textContent = 'Installed'; button.disabled = true; }
      else button.textContent = upgrade ? 'Update' : 'Install';
      button.onclick = () => safe(async () => {
        button.disabled = true; button.textContent = 'Checking…';
        let preview;
        try { preview = await json(`/extensions/catalog/${enc(entry.name)}/preview`, write('POST', {version: entry.version})); }
        catch (error) { button.disabled = false; button.textContent = upgrade ? 'Update' : 'Install'; throw error; }
        const box = el('div', 'ext-confirm');
        box.append(el('p', 'ext-label', `Checksum verified · SHA-256 ${preview.sha256.slice(0, 16)}…`), el('p', 'panel-meta ext-code', `Source: ${preview.source}`));
        if (preview.plugin) pluginAdds(box, preview.plugin);
        if (preview.skill) { box.append(el('p', 'ext-label', 'Instructions it adds')); box.append(el('pre', 'ext-body', preview.skill.body)); }
        const yes = el('button', 'btn primary', upgrade ? 'Update' : 'Install'); yes.type = 'button';
        const no = el('button', 'btn quiet', 'Cancel'); no.type = 'button';
        const row = el('div', 'dialog-actions'); row.append(no, yes); box.append(row);
        no.onclick = () => { api(`/extensions/catalog/preview/${enc(preview.ticket)}`, {method: 'DELETE'}).catch(() => {}); render(); };
        yes.onclick = () => safe(async () => {
          yes.disabled = true; no.disabled = true; yes.textContent = 'Installing…';
          try { const done = await json('/extensions/install', write('POST', {ticket: preview.ticket, upgrade})); toast(`Installed ${done.installed}.`, 'Extensions'); await refresh(); }
          catch (error) { await refresh(); throw error; }
        });
        text.append(box); button.remove(); yes.focus();
      });
      actions.append(button); item.append(text, actions); list.append(item);
    }
    function render() {
      for (const [key, button] of Object.entries(buttons)) { button.setAttribute('aria-selected', String(key === tab)); button.tabIndex = key === tab ? 0 : -1; }
      list.replaceChildren();
      const filtered = search.value.trim() !== '';
      if (tab === 'installed') {
        const shownPlugins = data.plugins.filter(plugin => matches(plugin.name, plugin.description));
        const shownSkills = data.skills.filter(skill => matches(skill.name, skill.description, skill.origin, skill.plugin));
        if (shownPlugins.length) { heading('Plugins'); shownPlugins.forEach(renderPlugin); }
        if (shownSkills.length) { heading('Skills'); shownSkills.forEach(renderSkill); }
        if (!shownPlugins.length && !shownSkills.length) list.append(el('p', 'panel-empty', filtered ? 'Nothing matches that search.' : 'Nothing installed yet. Browse Plugins or Skills, or let Kova learn a skill while you work.'));
        footer.textContent = 'Skills learned by Kova live in your skills folder; plugin skills go with their plugin.';
        return;
      }
      if (!data.catalog) { list.append(el('p', 'panel-empty', 'Loading the catalog…')); return; }
      const entries = data.catalog.entries.filter(entry => entry.kind === tab && matches(entry.name, entry.description, entry.tags, entry.author));
      if (!entries.length) list.append(el('p', 'panel-empty', filtered ? 'Nothing matches that search.' : 'The catalog has nothing here yet.'));
      entries.forEach(renderEntry);
      footer.textContent = data.catalog.warning || (data.catalog.bundled ? 'Catalog bundled with Kova.' : `Catalog: ${data.catalog.source}`);
    }
    async function refresh() {
      const current = ++version;
      const [plugins, skills, catalog] = await Promise.all([json('/extensions/plugins'), json('/extensions/skills'), json('/extensions/catalog')]);
      if (current !== version) return;
      data = {plugins, skills, catalog}; render();
    }
    launch.onclick = () => safe(async () => { sidebar(false); render(); dialog.showModal(); await refresh(); });
  }
  async function notifyReminder(event) {
    if (document.hasFocus?.() || localStorage.getItem('kova-notify') !== 'yes' ||
        !('Notification' in window) || Notification.permission !== 'granted' || !('serviceWorker' in navigator)) return;
    try {
      const registration = await navigator.serviceWorker.getRegistration('/app');
      registration?.active?.postMessage(event);
    } catch (_) { /* In-page reminders still work when OS notifications are unavailable. */ }
  }
  {
    let installPrompt = null;
    window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; });
    const open = $('install-settings');
    const dialog = el('dialog', 'panel'); dialog.setAttribute('aria-label', 'Install & notifications');
    const close = el('button', 'btn', 'Close'); close.onclick = () => dialog.close();
    const install = el('button', 'btn primary', 'Install on this device');
    install.onclick = async () => {
      if (installPrompt) { await installPrompt.prompt(); installPrompt = null; }
      else toast('Use your browser’s Install app or Add to Home Screen menu. Phone installation requires HTTPS.');
    };
    const phone = el('button', 'btn', 'Install on your phone');
    phone.onclick = () => { dialog.close(); $('connect-phone').click(); };
    const notify = el('button', 'btn');
    const label = () => { notify.textContent = localStorage.getItem('kova-notify') === 'yes' ? 'Notify me: on' : 'Notify me: off'; }; label();
    notify.onclick = async () => {
      if (localStorage.getItem('kova-notify') === 'yes') localStorage.removeItem('kova-notify');
      else if ('Notification' in window && await Notification.requestPermission() === 'granted') localStorage.setItem('kova-notify', 'yes');
      else toast('Notifications are unavailable or blocked on this device.');
      label();
    };
    dialog.append(el('h2', '', 'Install on your phone'),
      el('p', '', 'Scan the phone pairing QR to open /app, then use your browser’s install menu. Installation and notifications require HTTPS (or localhost on this computer). LAN HTTP pairing alone does not enable them.'),
      phone, install, notify,
      el('p', '', 'Notify me applies only to this browser/device. Reminders arrive through the local event stream while the app is alive and connected. Fully closed or suspended apps need a push service; Kova does not use FCM or send anything off this machine.'), close);
    document.body.append(dialog); open.onclick = () => dialog.showModal();
  }

  // Phone pairing (#66). The page on this computer shows a one-use QR code; the
  // phone that scans it opens /app#pair=… over the Wi-Fi and becomes a paired device.
  function onPhone() { return Boolean(location.hostname) && !['127.0.0.1', 'localhost', '[::1]', '::1'].includes(location.hostname); }
  {
    const launch = $('connect-phone');
    const dialog = el('dialog', 'panel phone-dialog'); dialog.setAttribute('aria-label', 'Connect your phone');
    const head = el('div', 'dialog-head'), close = iconButton('x', 'Close');
    close.onclick = () => dialog.close(); head.append(el('h2', '', 'Connect your phone'), close);
    const body = el('div', 'phone-body'), devices = el('div', 'phone-devices');
    dialog.append(head, body, devices); document.body.append(dialog);
    let code = null, timer = null, version = 0;
    function forget() {
      clearTimeout(timer); timer = null;
      if (code?.state === 'waiting') api(`/phone/pair/${code.id}`, {method: 'DELETE'}).catch(() => {});
      code = null;
    }
    dialog.addEventListener('close', () => { ++version; forget(); });
    function button(text, cls, action) {
      const node = el('button', cls, text); node.type = 'button';
      node.onclick = () => safe(async () => { node.disabled = true; try { await action(); } finally { node.disabled = false; } });
      return node;
    }
    async function setAccess(enabled) { await json('/phone', write('POST', {enabled})); await render(); }
    async function render() {
      const current = ++version; forget();
      const status = await json('/phone');
      if (current !== version) return;
      body.replaceChildren();
      if (!status.enabled) {
        const points = el('ul', 'phone-points');
        for (const text of ['Your chats, memory and model stay on this computer. The phone is a window into it.',
                            'Kova also listens on your home network, never the internet.',
                            'Only phones you pair with a QR code get in. Unpair any of them at any time.'])
          points.append(el('li', '', text));
        body.append(el('p', 'panel-intro', 'Use Kova from your phone on the same Wi-Fi.'), points,
                    button('Turn on phone access', 'btn primary', () => setAccess(true)));
      } else if (!status.listening) {
        const error = el('p', 'phone-error', status.error || 'Phone access is on but not listening.'); error.setAttribute('role', 'alert');
        body.append(error, button('Try again', 'btn primary', () => setAccess(true)));
      } else {
        const box = el('div', 'phone-qr'); body.append(box);
        await newCode(box, current);
      }
      renderDevices(status);
    }
    async function newCode(box, current) {
      forget();
      const pair = await json('/phone/pair', {method: 'POST'});
      if (current !== version) { api(`/phone/pair/${pair.id}`, {method: 'DELETE'}).catch(() => {}); return; }
      code = {id: pair.id, state: 'waiting'};
      const expires = Date.now() + pair.expires_in * 1000;
      const img = el('img', 'phone-code'); img.src = pair.qr; img.alt = 'QR code that connects your phone to Kova'; img.width = 216; img.height = 216;
      const steps = el('ol', 'phone-steps');
      for (const text of ['Put your phone on the same Wi-Fi as this computer.', 'Point its camera at the code.', 'Tap the link that appears.']) steps.append(el('li', '', text));
      const status = el('p', 'phone-status'); status.setAttribute('role', 'status');
      const copy = button('Copy link instead', 'btn quiet', async () => {
        try { await navigator.clipboard.writeText(pair.url); toast('Link copied. It works once, for two minutes.'); }
        catch (_) { toast('Clipboard unavailable here.'); }
      });
      const card = el('div', 'phone-card'); card.append(img, el('small', 'phone-address', pair.address));
      const side = el('div', 'phone-side'); side.append(steps, status, copy);
      box.replaceChildren(card, side);
      async function poll() {
        if (code?.id !== pair.id) return;
        const result = await json(`/phone/pair/${pair.id}`);
        if (code?.id !== pair.id) return;
        if (result.status === 'paired') {
          code.state = 'paired';
          const done = el('div', 'phone-done'); done.append(icon('check'), el('strong', '', `Paired ${result.device?.name || 'your phone'}`),
            el('p', '', 'Kova is ready on it. Tip: add it to the home screen for one-tap access.'),
            button('Pair another phone', 'btn', render));
          box.replaceChildren(done); renderDevices(await json('/phone'));
          return;
        }
        const left = Math.max(0, Math.round((expires - Date.now()) / 1000));
        if (result.status === 'expired' || !left) {
          code.state = 'expired'; img.classList.toggle('expired', true);
          status.textContent = 'This code expired.';
          side.append(button('Show a new code', 'btn primary', () => newCode(box, version)));
          return;
        }
        status.textContent = `Waiting for your phone… ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
        timer = setTimeout(() => safe(poll), 1500);
      }
      await poll();
    }
    function renderDevices(status) {
      devices.replaceChildren();
      if (!status.devices.length && !status.enabled) return;
      devices.append(el('strong', 'phone-devices-title', status.devices.length ? 'Paired devices' : 'No phones paired yet'));
      for (const device of status.devices) {
        const row = el('div', 'panel-item'), text = el('div');
        const seen = new Date(device.last_seen);
        text.append(el('strong', '', device.name), el('p', 'panel-meta', `Last seen ${Number.isNaN(seen.getTime()) ? 'never' : seen.toLocaleString()}`));
        const remove = button('Unpair', 'btn quiet', async () => {
          await json(`/devices/${encodeURIComponent(device.id)}`, {method: 'DELETE'});
          renderDevices(await json('/phone'));
        });
        remove.setAttribute('aria-label', `Unpair ${device.name}`);
        row.append(text, remove); devices.append(row);
      }
      if (status.enabled) devices.append(button('Turn off phone access', 'btn quiet phone-off', () => setAccess(false)));
    }
    launch.hidden = onPhone();
    launch.onclick = () => safe(async () => { sidebar(false); dialog.showModal(); await render(); });
    if (onPhone()) {
      // On the phone itself, Disconnect means unpair.
      $('disconnect').title = 'Unpair this phone'; $('disconnect').setAttribute('aria-label', 'Unpair this phone');
      const unpair = $('disconnect').onclick;
      $('disconnect').onclick = () => { if (window.confirm('Unpair this phone? You will need to scan a new code from your computer to come back.')) unpair(); };
    }
  }
  /* Custom menus. The hidden <select> stays the single source of truth, so every
     handler above keeps reading .value; these only draw and drive it. */
  const menus = [], modeButtons = [...document.querySelectorAll('#mode-switch [role="radio"]')];
  let popup = null;
  function optionsOf(select) { return [...(select.options || [])]; }
  function syncMenus() {
    for (const owner of menus) {
      const chosen = optionsOf(owner.select).find(option => option.value === owner.select.value);
      owner.text.textContent = chosen?.textContent || owner.placeholder;
      owner.button.disabled = owner.select.disabled || !optionsOf(owner.select).length;
      owner.button.title = `${owner.label}: ${owner.text.textContent}`;
    }
    for (const button of modeButtons) {
      const checked = button.dataset.value === $('mode').value;
      button.setAttribute('aria-checked', String(checked)); button.tabIndex = checked ? 0 : -1;
      button.disabled = $('mode').disabled;
    }
    if (popup && popup.owner.button.disabled) closeMenu();
    // The chip says which model answers; mode, permission, folder and web sit
    // behind it, and its title spells them out for anyone who hovers or listens.
    const model = optionsOf($('model')).find(option => option.value === $('model').value);
    const label = model?.textContent || 'No model';
    $('chat-options-label').textContent = label;
    const detail = [label, $('mode').value === 'chat' ? 'Chat mode' : 'Work mode', state.modelStatus, state.localOnly ? 'Local only' : ''].filter(Boolean).join(', ');
    $('chat-options').title = `Chat options: ${detail}`;
    $('chat-options').setAttribute('aria-label', `Chat options. ${detail}`);
  }
  function closeMenu(refocus = false) {
    if (!popup) return;
    const {node, owner} = popup; popup = null;
    node.remove(); owner.button.setAttribute('aria-expanded', 'false');
    if (refocus) owner.button.focus();
  }
  function openMenu(owner) {
    closeMenu();
    const {select, button} = owner, items = [];
    const node = el('div', 'popover'), list = el('div', 'popover-list');
    list.id = `${select.id}-listbox`; list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', owner.label); list.tabIndex = -1;
    let active = -1, typed = '', typedAt = 0;
    const choose = value => {
      closeMenu(true);
      if (value === select.value) return;
      select.value = value; syncMenus(); select.onchange?.();
    };
    const add = (option, parent) => {
      const index = items.length, item = el('div', 'option'), text = el('span', 'option-text');
      item.id = `${select.id}-option-${index}`; item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.value === select.value));
      text.append(el('span', 'option-title', option.textContent));
      const note = owner.describe?.(option);
      if (note) text.append(el('span', 'option-note', note));
      item.append(text, icon('check'));
      item.onclick = () => choose(option.value);
      item.onpointermove = () => { if (active !== index) activate(index, false); };
      parent.append(item); items.push({item, value: option.value, label: option.textContent.toLowerCase(), group: parent === list ? null : parent});
    };
    for (const child of select.children) {
      if (child.tagName !== 'OPTGROUP') { add(child, list); continue; }
      const group = el('div', 'option-group'); group.setAttribute('role', 'group'); group.setAttribute('aria-label', child.label);
      group.append(el('div', 'option-group-label', child.label));
      for (const option of child.children) add(option, group);
      list.append(group);
    }
    // Related actions close the list: reachable with the same arrows and Enter.
    for (const action of owner.actions || []) {
      const index = items.length, item = el('div', 'option option-action');
      item.id = `${select.id}-option-${index}`; item.setAttribute('role', 'option'); item.setAttribute('aria-selected', 'false');
      item.append(icon(action.glyph), el('span', 'option-title', action.label));
      item.onclick = () => { closeMenu(true); action.run(); };
      item.onpointermove = () => { if (active !== index) activate(index, false); };
      list.append(item); items.push({item, action, value: null, label: action.label.toLowerCase(), group: null});
    }
    const none = el('div', 'option-empty', 'No matches'); none.hidden = true; list.append(none);
    const search = items.length > 7 ? el('input', 'popover-search') : null;
    const target = search || list;
    if (search) {
      search.type = 'search'; search.placeholder = `Search ${owner.label.toLowerCase()}s`; search.autocomplete = 'off'; search.spellcheck = false;
      search.setAttribute('role', 'combobox'); search.setAttribute('aria-label', search.placeholder);
      search.setAttribute('aria-expanded', 'true'); search.setAttribute('aria-controls', list.id); search.setAttribute('aria-autocomplete', 'list');
      search.oninput = () => {
        const query = search.value.trim().toLowerCase();
        for (const entry of items) entry.item.hidden = !entry.label.includes(query);
        for (const group of list.querySelectorAll('.option-group')) group.hidden = !group.querySelector('.option:not([hidden])');
        none.hidden = items.some(entry => !entry.item.hidden);
        activate(items.findIndex(entry => !entry.item.hidden));
      };
      node.append(search);
    }
    node.append(list);
    const visible = () => items.map((entry, index) => entry.item.hidden ? -1 : index).filter(index => index >= 0);
    function activate(index, reveal = true) {
      if (items[active]) items[active].item.classList.remove('active');
      active = index;
      if (!items[index]) { target.removeAttribute('aria-activedescendant'); return; }
      items[index].item.classList.add('active');
      target.setAttribute('aria-activedescendant', items[index].item.id);
      if (reveal) items[index].item.scrollIntoView({block: 'nearest'});
    }
    function step(delta) {
      const order = visible(); if (!order.length) return;
      const at = order.indexOf(active);
      activate(order[(at < 0 ? (delta > 0 ? 0 : order.length - 1) : at + delta + order.length) % order.length]);
    }
    node.addEventListener('keydown', event => {
      const order = visible();
      if (event.key === 'ArrowDown') step(1);
      else if (event.key === 'ArrowUp') step(-1);
      else if (event.key === 'Home' && !search) activate(order[0]);
      else if (event.key === 'End' && !search) activate(order.at(-1));
      else if (event.key === 'Enter' || (event.key === ' ' && !search)) { if (items[active] && !items[active].item.hidden) items[active].item.onclick(); }
      else if (event.key === 'Escape') { event.stopPropagation(); closeMenu(true); }
      else if (event.key === 'Tab') { closeMenu(true); return; }
      else {
        if (!search && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          const now = Date.now(); typed = now - typedAt > 600 ? event.key.toLowerCase() : typed + event.key.toLowerCase(); typedAt = now;
          const match = items.findIndex(entry => entry.label.startsWith(typed));
          if (match >= 0) activate(match);
        }
        return;
      }
      event.preventDefault();
    });
    node.addEventListener('focusout', event => {
      if (popup?.node === node && event.relatedTarget && !node.contains(event.relatedTarget)) closeMenu();
    });
    (button.closest('dialog') || document.body).append(node);
    popup = {node, owner, place};
    button.setAttribute('aria-expanded', 'true');
    function place() {
      const rect = button.getBoundingClientRect(), gap = 6, edge = 8;
      const below = innerHeight - rect.bottom - gap - edge, above = rect.top - gap - edge;
      node.style.minWidth = `${Math.min(Math.max(rect.width, owner.width), innerWidth - edge * 2)}px`;
      node.style.maxHeight = ''; node.style.top = node.style.bottom = 'auto';
      const up = below < Math.min(node.offsetHeight, 260) && above > below;
      node.style.maxHeight = `${Math.max(120, Math.min(400, up ? above : below))}px`;
      if (up) node.style.bottom = `${innerHeight - rect.top + gap}px`; else node.style.top = `${rect.bottom + gap}px`;
      node.dataset.side = up ? 'top' : 'bottom';
      const width = node.offsetWidth, left = owner.align === 'end' ? rect.right - width : rect.left;
      node.style.left = `${Math.max(edge, Math.min(left, innerWidth - width - edge))}px`;
    }
    place();
    activate(items.findIndex(entry => entry.value === select.value));
    target.focus({preventScroll: true});
  }
  function menu(id, label, {describe, glyph, align = 'start', width = 190, placeholder = 'Choose…', labelledby, actions} = {}) {
    const select = $(id), button = el('button', 'menu-button'), text = el('span', 'menu-value', placeholder);
    button.type = 'button'; button.id = `${id}-button`;
    button.setAttribute('aria-haspopup', 'listbox'); button.setAttribute('aria-expanded', 'false');
    if (labelledby) button.setAttribute('aria-labelledby', `${labelledby} ${id}-button`); else button.setAttribute('aria-label', label);
    if (glyph) button.append(icon(glyph));
    button.append(text, icon('chevron-down'));
    const owner = {select, button, text, label, describe, align, width, placeholder, actions};
    button.onclick = () => { if (popup?.owner === owner) closeMenu(); else openMenu(owner); };
    button.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); if (popup?.owner !== owner) openMenu(owner); }
    });
    $(`${id}-slot`).append(button); menus.push(owner); return owner;
  }
  const permissionNotes = {manual: 'Ask before commands and file changes', auto: 'Ask only before destructive commands', full: 'Never ask. Use with care.'};
  menu('permission', 'Permission', {glyph: 'shield-check', width: 270, describe: option => permissionNotes[option.value],
    actions: [{glyph: 'sliders-horizontal', label: 'Edit permission rules…', run: openRules}]});
  menu('model', 'Model', {align: 'end', width: 260, placeholder: 'No model', describe: option => option.textContent === option.value ? '' : option.value,
    actions: [{glyph: 'sliders-horizontal', label: 'Model settings…', run: () => safe(openModelSettings)}]});
  menu('provider-kind', 'Provider', {width: 240, labelledby: 'provider-kind-label'});
  menu('provider-active', 'Active model', {width: 240, labelledby: 'provider-active-label'});
  function setMode(value) {
    if ($('mode').disabled || $('mode').value === value) return;
    $('mode').value = value; syncMenus(); $('mode').onchange();
    if (!state.chat) controls();
  }
  modeButtons.forEach((button, index) => {
    button.onclick = () => setMode(button.dataset.value);
    button.addEventListener('keydown', event => {
      const delta = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
      if (!delta) return;
      event.preventDefault();
      const next = modeButtons[(index + delta + modeButtons.length) % modeButtons.length];
      setMode(next.dataset.value); next.focus();
    });
  });
  /* The two small surfaces of the message box: the + menu (attach, commands,
     incognito on the start screen) and the chat options behind the model chip. */
  const moreButton = $('compose-more'), moreMenu = $('compose-menu');
  const optionsButton = $('chat-options'), optionsPanel = $('chat-options-panel');
  const moreItems = () => [...moreMenu.querySelectorAll('[role="menuitem"]')].filter(item => !item.hidden && !item.disabled);
  function more(open, focus = 'first') {
    if (open) options(false);
    moreMenu.hidden = !open; moreButton.setAttribute('aria-expanded', String(open));
    if (open) { const items = moreItems(); (focus === 'last' ? items.at(-1) : items[0])?.focus(); }
  }
  moreButton.onclick = () => more(moreMenu.hidden);
  moreButton.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); more(true, event.key === 'ArrowUp' ? 'last' : 'first'); }
  });
  moreMenu.addEventListener('keydown', event => {
    const items = moreItems(), at = items.indexOf(document.activeElement);
    if (event.key === 'ArrowDown') items[(at + 1) % items.length]?.focus();
    else if (event.key === 'ArrowUp') items[(at - 1 + items.length) % items.length]?.focus();
    else if (event.key === 'Home') items[0]?.focus();
    else if (event.key === 'End') items.at(-1)?.focus();
    else if (event.key === 'Escape') { event.stopPropagation(); more(false); moreButton.focus(); }
    else if (event.key === 'Tab') { more(false); return; }
    else return;
    event.preventDefault();
  });
  // Choosing an item closes the menu first, so focus lands where the item sends it.
  moreMenu.addEventListener('click', event => { if (event.target.closest?.('[role="menuitem"]')) more(false); }, true);
  function options(open) {
    if (open) more(false);
    optionsPanel.hidden = !open; optionsButton.setAttribute('aria-expanded', String(open));
    if (!open) closeMenu();
  }
  optionsButton.onclick = () => {
    const open = optionsPanel.hidden; options(open);
    if (open) optionsPanel.querySelector('button:not(:disabled), input:not(:disabled)')?.focus();
  };
  optionsPanel.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    event.stopPropagation(); event.preventDefault(); options(false); optionsButton.focus();
  });
  // Focus that leaves both the panel and its listbox (a popover in <body>) closes it.
  optionsPanel.addEventListener('focusout', event => {
    const next = event.relatedTarget;
    if (next && !optionsPanel.contains(next) && next !== optionsButton && !next.closest?.('.popover')) options(false);
  });
  document.addEventListener('pointerdown', event => {
    if (popup && !popup.node.contains(event.target) && !popup.owner.button.contains(event.target)) closeMenu();
    const inside = node => node.contains(event.target);
    if (!moreMenu.hidden && !inside(moreMenu) && !inside(moreButton)) more(false);
    if (!optionsPanel.hidden && !inside(optionsPanel) && !inside(optionsButton) && !event.target.closest?.('.popover')) options(false);
  }, true);
  window.addEventListener('resize', () => popup?.place());
  window.addEventListener('blur', () => closeMenu());
  $('notice-retry').onclick = () => safe(start);
  window.addEventListener('online', () => safe(start));
  function historyReplace() { if (location.hash) window.history.replaceState(null, '', location.pathname + location.search); }
  window.addEventListener('beforeunload', () => state.events?.close());
  // A visible tab polls every 30 s. A hidden tray window skips the poll and backs
  // off to 2–5 minutes, so it neither blocks a model check nor hammers /health.
  let healthTimer = null;
  function scheduleHealth(delay) {
    clearTimeout(healthTimer);
    const wait = delay ?? (document.hidden ? 120000 + Math.random() * 180000 : 30000);
    healthTimer = setTimeout(async () => {
      if (state.connected && !document.hidden) await health();
      scheduleHealth();
    }, wait);
  }
  scheduleHealth();
  setInterval(checkStream, 15000);
  // A phone tab that wakes up checks its stream (and model) at once instead of later.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { checkStream(); if (state.connected) health(); }
    scheduleHealth();
  });
  window.addEventListener('hashchange', () => { if (/(ticket|pair|share)=/.test(location.hash) && !state.busy) { state.events?.close(); start(); } });

  controls(); start();
})();

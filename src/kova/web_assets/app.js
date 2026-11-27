/* Kova is a client of the local API. No agent logic or permanent token lives here. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {chat: null, chats: [], busy: false, connected: false, events: null, models: [], providers: [], queued: [], turnReady: false, stopRequested: false, searchVersion: 0, navigationVersion: 0, starting: false, persona: {name: 'Kova'}, offered: null, localOnly: false, activity: new Map(), activityStarted: {}, finishedActivity: [], activityOpen: false, comparing: false};
  let defaultModel = '', draftModel = null, pickerSaving = false, capabilitiesVersion = 0;
  let compare = null;  // compare.js (#96), created once the helpers it borrows exist
  const effortLabels = {'': 'Automatic', off: 'Off', low: 'Low', medium: 'Medium', high: 'High'};
  // Quick ask overlay (#71). The Electron shell opens /app?quick=1 in a small
  // always-on-top window; the same client then behaves as a one-line ask box.
  const QUICK_TITLE_PREFIX = '[quick] ';
  const isQuick = typeof location !== 'undefined' && new URLSearchParams(typeof location.search === 'string' ? location.search : '').get('quick') === '1';
  let quickSelection = '';  // clipboard selection the shell handed over, shown as a chip
  // Quick ask chats keep their [quick] title prefix so the sidebar and the
  // palette skip them, exactly as `kova ask` hides its [ask] chats.
  const isQuickChat = chat => typeof chat?.title === 'string' && chat.title.startsWith(QUICK_TITLE_PREFIX);
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
  // Composer extras (#197): input history and @-mentions live in their own
  // files and are wired up beside the message box, not inside its send path.
  const composerModules = {history: null, mentions: null};
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
    $('welcome').hidden = !flag || (!!bots && state.chat?.kind === 'bot');
    document.body.classList.toggle('is-empty', flag);
    paintBotChat();
    renderStarters();
  }
  // Start screen suggestions and greeting (#194). starters.js owns the list,
  // its editor and its shared preference; app.js only fills the message box.
  const starterComponent = () => globalThis.KovaStarters || null;
  function renderStarters() {
    const host = $('starters'), edit = $('starters-edit'), module = starterComponent();
    if (!module || !host || state.chat) {
      if (host) host.hidden = true;
      if (edit) edit.hidden = true;
      return;
    }
    module.render(host, text => {
      $('prompt').value = text; resize(); $('prompt').focus({preventScroll: true});
    });
    // The editor stays reachable when the list is empty; that is how it is
    // turned back on after "Turn off".
    if (edit) edit.hidden = false;
  }
  function renderWelcome() {
    // The chat-app shell asks a mode-specific question instead.
    if (shell) { shell.refresh(); return; }
    const title = $('welcome-title');
    if (title && state.persona?.name) title.textContent = `${state.persona.name} shows its work.`;
  }
  $('starters-edit')?.addEventListener('click', () => safe(async () => {
    const module = starterComponent(); if (!module) return;
    const next = await module.openEditor();
    if (next === null) return;
    const edit = $('starters-edit');
    edit.disabled = true;
    try { await module.save(next); }
    finally { edit.disabled = false; }
  }));
  document.addEventListener('kova:starters', renderStarters);
  // Per-chat drafts (#197). drafts.js owns storage and the incognito exclusion;
  // app.js saves on typing and restores when a chat opens. A saved chat only.
  const draftComponent = () => globalThis.KovaDrafts || null;
  const draftOf = chat => draftComponent()?.read(chat) || '';
  const rememberDraft = () => draftComponent()?.write(state.chat, $('prompt').value);
  function consumeDraft(chat, text) {
    if (state.chat?.id === chat?.id) {
      if ($('prompt').value !== text) return;
      $('prompt').value = '';
      resize();
    } else if (draftOf(chat) !== text) return;
    draftComponent()?.clear(chat);
  }
  // One toast component (toast.js, #198). These wrappers are the only way
  // app.js posts transient messages; the severity decides politeness and how
  // long the message waits. Errors stay until dismissed.
  const toastComponent = globalThis.KovaToast || null;
  function toast(message, title = 'Kova', options = {}) {
    if (toastComponent) return toastComponent.show(message, {title, ...options});
    // An older cached shell may not have toast.js yet: keep a plain fallback.
    const node = el('div', `toast toast-${options.severity || 'info'}`);
    const body = el('div', 'toast-body'); body.append(el('strong', 'toast-title', title), el('span', 'toast-message', message));
    const close = iconButton('x', 'Dismiss notification');
    node.append(icon(title === 'Reminder' ? 'bell' : 'info'));
    close.onclick = () => node.remove(); node.append(body, close); $('toasts').append(node);
    // Reminders stay until explicitly dismissed.
    if (title !== 'Reminder' && options.sticky !== true) setTimeout(() => node.remove(), 9000);
  }
  function toastSuccess(message, title = 'Kova', options = {}) {
    return toast(message, title, {...options, severity: 'success'});
  }
  function toastError(message, title = 'Kova', options = {}) {
    return toast(message, title, {...options, severity: 'error'});
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
    const version = ++capabilitiesVersion;
    try {
      const contract = await json(model ? `/capabilities?model=${encodeURIComponent(model)}` : '/capabilities');
      if (version === capabilitiesVersion) applyCapabilities(contract);
    } catch (_) {
      if (version === capabilitiesVersion) { state.offered = null; controls(); }
    }
  }
  let shell = null;
  const inBots = () => shell?.app() === 'bots';
  const botConversation = chat => chat?.kind === 'bot' || chat?.kind === 'group';
  const inCurrentWorkspace = chat => botConversation(chat) === inBots();
  function controls() {
    const busy = state.busy;
    for (const button of activityOpenButtons) button.disabled = busy;
    const attachmentCount = pendingAttachments.filter(a => a.chat_id === state.chat?.id).length;
    for (const id of ['new-chat', 'new-incognito', 'mode', 'web', 'model', 'permission', 'cwd', 'add-provider']) $(id).disabled = busy || !state.connected;
    for (const id of ['model', 'mode', 'reasoning-effort']) $(id).disabled = busy || pickerSaving || !state.connected;
    if (bots?.botModel(state.chat)) $('model').disabled = $('reasoning-effort').disabled = true;
    bots?.refreshControls();
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
    $('queue').hidden = !busy || nothingToSend || state.comparing; $('steer').hidden = !busy || nothingToSend || state.comparing;
    $('compare-open').disabled = busy || pickerSaving || !state.connected || inBots();
    $('send').disabled = readingAttachments || pickerSaving || !state.connected || (!$('prompt').value.trim() && !attachmentCount);
    $('attach').disabled = !!state.chat?.ephemeral || state.busy || pickerSaving || readingAttachments || !state.connected;
    $('attach').classList.toggle('text-only', !offers('vision'));
    $('attach').title = offers('vision') ? 'Attach files or images' : 'Attach documents; images need Vision turned on in Model settings';
    $('queue').disabled = nothingToSend; $('steer').disabled = nothingToSend;
    $('stop').disabled = state.stopRequested;
    document.querySelectorAll('.history-row button').forEach(button => { button.disabled = busy; });
    document.querySelectorAll('.project-action').forEach(button => { button.disabled = busy || !state.connected; });
    $('composer').setAttribute('aria-busy', String(busy));
    syncMenus();
    refreshStatusBar();
    shell?.refresh();
  }
  // First slice of #204: the optional status bar reads what the server already
  // sent (chat messages, the model list, the active model) and reuses the same
  // ~4-chars-a-token estimate as the terminal chat. No new endpoint.
  function refreshStatusBar() {
    const status = typeof window !== 'undefined' ? window.KovaStatus : null;
    if (!status) return;
    const active = (state.models || []).find(entry => entry.name === $('model').value);
    status.update({model: $('model').value || '', windowTokens: (active && active.context) || 0,
                   messages: (state.chat && state.chat.messages) || [],
                   connected: state.connected, phone: onPhone()});
  }
  window.KovaStatusRefresh = refreshStatusBar;
  function disconnected(message) {
    state.connected = false;
    ++state.searchVersion;
    state.events?.close();
    state.activity.clear();
    state.activityStarted = {};
    state.finishedActivity = [];
    bots?.reset();
    paintBotChat();
    renderActivity();
    $('chat-list').replaceChildren(); $('chat-list').removeAttribute?.('aria-busy');
    bots?.renderDirectory();
    notice(message || (onPhone() ? 'This phone is not connected. On your computer, run kova pair (or choose Connect your phone in Kova) and scan the code.'
      : 'Open kova web on this computer to connect. No token or account needed.'));
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
        : 'Your connection expired. Open kova web again to reconnect.');
      throw new Error(message);
    }
    return response;
  }
  const json = async (path, options) => (await api(path, options)).json();
  const write = (method, value) => ({method, body: JSON.stringify(value)});
  const projects = globalThis.KovaProjects?.create({el, json, write, safe, refresh: refreshChats, newChat: newProjectChat,
    models: () => state.models || []});
  const bots = isQuick ? null : globalThis.KovaBots?.create({el, icon, json, write, safe,
    directory: $('bot-directory'), editor: $('bot-editor'), header: $('bot-chat-header'), welcome: $('bot-welcome'),
    models: () => state.models, activeModel: () => defaultModel, chat: () => state.chat,
    available: () => state.connected && !state.busy,
    onEditorOpen: () => { ++state.navigationVersion; sidebar(false); options(false); toggleZen(false, {fromTheme: true}); },
    onChange: () => { paintBotChat(); syncComposerModel(); history(state.chats); },
    openChat: openBotChat, openThread: id => openChat(id), toolLabel, toolHint, changeFiles, reviewChanges,
    focusChat: () => (state.chat ? $('prompt') : $('bot-directory-title'))?.focus()});
  shell = isQuick ? null : globalThis.KovaShell?.create({state, forkChat, chatSetting, safe, controls, switchApp: changeWorkspace}) || null;
  // Markdown belongs to markdown-view.js (#189): footnotes, task lists, tables
  // and streaming code need more than one sanitize call. The inline fallback
  // keeps a shell that did not ship the module readable instead of blank.
  function markdown(node, text, final = false) {
    if (window.KovaMarkdown?.render) { window.KovaMarkdown.render(node, text, {final}); return; }
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
    // Quiet header row (language label + Copy button) and highlight.js
    // highlighting, present from the first streamed line so nothing jumps when
    // the reply ends. markdown() rebuilds this subtree on every chunk, so clear
    // the module's once-per-root marker or it would skip the rebuild.
    node.classList.remove('code-header-ready');
    window.CodeHeader?.attach(node, final);
  }
  async function copyText(text, copied) {
    try { await navigator.clipboard.writeText(text); copied?.(); }
    catch (_) { toastError('Clipboard unavailable. Select the text to copy it.'); }
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
  // Quiet actions under a message: copy it as Markdown, edit your own words,
  // retry the last reply, or open what the server recorded for the answer.
  function messageActions(article, read, role, usage = null, options = {}) {
    const module = globalThis.KovaMessageActions;
    if (module) {
      const md = window.KovaMarkdown;
      const blocks = [...article.querySelectorAll('.message-body > .markdown')];
      const headings = md?.outline?.(...blocks) || [];
      const bar = module.bar({
        role, text: read, usage, model: options.model || usage?.model || '',
        toolCount: options.toolCount, onRetry: options.onRetry, onEdit: options.onEdit,
        onRetryModel: options.onRetry && state.models.some(entry => entry.model && entry.name !== options.model)
          ? () => chooseRetryModel(options.model, options.messageId) : null,
        onBranch: options.messageId && !state.chat?.ephemeral
          ? () => safe(() => forkChat(state.chat?.id, options.messageId)) : null,
        plainText: blocks.length && md?.plainText ? () => blocks.map(md.plainText).join('\n\n') : null,
        onOutline: headings.length && window.KovaPanel?.open ? () => openReplyOutline(headings) : null,
        alternatives: options.alternatives, onAlternative: options.onAlternative,
        editLabel: options.editLabel, onError: toastError,
      });
      const stamp = module.timestamp(options.created);
      if (stamp) article.append(stamp);
      article.append(bar);
      module.touchActions(article, bar);
      return;
    }
    // An older cached shell may not have message-actions.js yet.
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
  function openReplyOutline(headings) {
    const list = el('ol', 'reply-outline');
    for (const heading of headings) {
      const item = el('li');
      const button = el('button', 'nav-row', heading.textContent.trim());
      button.type = 'button';
      button.onclick = () => {
        if (!wide?.matches) window.KovaPanel.close();
        for (let parent = heading.parentElement; parent && parent.id !== 'messages'; parent = parent.parentElement) {
          if (parent.tagName === 'DETAILS') parent.open = true;
        }
        heading.tabIndex = -1;
        heading.focus({preventScroll: true});
        heading.scrollIntoView({block: 'start'});
      };
      item.append(button);
      list.append(item);
    }
    window.KovaPanel.open({title: 'Reply outline', node: list});
  }
  // Drop the last turn's DOM, keeping the user's words when a retry replaces
  // only the reply. The server is the source of truth; this just stops the
  // transcript from showing a stale reply until the new one streams in.
  function clearLastTurn(includeUser) {
    const nodes = [...$('messages').children];
    let cut = -1;
    nodes.forEach((node, index) => { if (node.classList.contains('user')) cut = index; });
    if (cut < 0) return;
    for (const node of nodes.slice(includeUser ? cut : cut + 1)) node.remove();
  }
  async function retryLastTurn(model = null, messageId = null) {
    if (state.busy) return toast('Wait for the current reply to finish.');
    if (!state.chat) return;
    await sendText('', {action: 'retry', model, messageId, omitUser: true, clear: 'reply'});
  }
  function retrySameModel(model, messageId) {
    return retryLastTurn(state.models.some(entry => entry.model && entry.name === model) ? model : null, messageId);
  }
  // Flip the newest turn between attempts. The server keeps every variant, so
  // this only changes which one is active; a full reload draws the same result.
  async function switchAlternative(turnId, index) {
    if (state.busy) return toast('Wait for the current reply to finish.');
    if (!state.chat || !turnId) return;
    const chat = await json(`/chats/${state.chat.id}/alternatives`, write('POST', {turn_id: turnId, index}));
    if (state.chat?.id === chat.id) { setChat(chat); follow(); }
  }
  function chooseRetryModel(previous, messageId) {
    if (state.busy) return toast('Wait for the current reply to finish.');
    const entries = state.models.filter(entry => entry.model && entry.name !== previous);
    if (!entries.length) return;
    const dialog = el('dialog', 'panel edit-dialog');
    dialog.setAttribute('aria-label', 'Retry with another model');
    const heading = el('h2', '', 'Retry with another model');
    const label = el('label');
    const models = el('select');
    for (const entry of entries) {
      const option = el('option', '', entry.name);
      option.value = entry.name;
      models.append(option);
    }
    label.append(el('span', '', 'Model'), models);
    const note = el('p', 'provider-footnote', 'Retry replaces this reply without changing the chat’s model.');
    const row = el('div', 'dialog-actions');
    const cancel = el('button', 'btn quiet', 'Cancel');
    cancel.onclick = () => dialog.close();
    const submit = el('button', 'btn primary', 'Retry');
    submit.onclick = () => { dialog.close(); safe(() => retryLastTurn(models.value, messageId)); };
    row.append(cancel, submit);
    dialog.append(heading, label, note, row);
    document.body.append(dialog);
    dialog.onclose = () => dialog.remove();
    dialog.showModal();
    models.focus();
  }
  function editLastMessage(text) {
    if (state.busy) return toast('Wait for the current reply to finish.');
    if (!state.chat) return;
    const dialog = el('dialog', 'panel edit-dialog');
    dialog.setAttribute('aria-label', 'Edit your last message');
    const form = el('form'); form.method = 'dialog';
    const head = el('div', 'dialog-head'), title = el('h2', '', 'Edit your last message');
    const close = iconButton('x', 'Close'); close.type = 'button'; close.onclick = () => dialog.close();
    head.append(title, close);
    const label = el('label'); label.append(el('span', '', 'Your message'));
    const field = el('textarea'); field.value = text; label.append(field);
    const note = el('p', 'provider-footnote', 'Sending replaces this message and the reply after it. Files stay attached.');
    const actions = el('div', 'dialog-actions');
    const cancel = el('button', 'btn quiet', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => dialog.close();
    const save = el('button', 'btn primary', 'Save and resend'); save.type = 'button';
    save.onclick = () => safe(async () => {
      const next = field.value.trim();
      if (!next) return toast('Write a message first.');
      save.disabled = true; dialog.close();
      await sendText(next, {action: 'edit', clear: 'turn'});
    });
    actions.append(cancel, save); form.append(head, label, note, actions);
    dialog.append(form); document.body.append(dialog); dialog.onclose = () => dialog.remove(); dialog.showModal();
    field.focus();
  }
  // Tool rows speak plainly: what ran, on what, and the gist of what came back.
  function toolLabel(name) {
    const words = String(name || 'tool').replaceAll('_', ' ');
    return words[0].toUpperCase() + words.slice(1);
  }
  function toolHint(args) {
    if (typeof args === 'string') { try { args = JSON.parse(args); } catch (_) { return ''; } }
    for (const key of ['command', 'path', 'query', 'url', 'title', 'text', 'when', 'name', 'task']) {
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
  function message(role, content, heading, attachments = [], metadata = {}) {
    const article = el('article', `message ${role}`);
    const label = el('div', 'message-label');
    if (role === 'user') label.append(document.createTextNode(heading || 'You'));
    else { const mark = el('span', 'kova-mark avatar'); mark.setAttribute('aria-hidden', 'true'); label.append(mark, document.createTextNode(heading || state.persona.name)); }
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
    if (metadata.author) bots?.labelReply(label, metadata.author, metadata.author_name, state.persona.name);
    if (role === 'user') {
      messageActions(article, () => content, 'user', null, {
        created: metadata.created,
        onEdit: metadata.edit && content ? () => editLastMessage(content) : null,
      });
    }
    return body;
  }
  // Per-chat colour and icon live in chat-appearance.js; the sidebar keeps
  // working when it is missing, so every hook below guards its presence.
  const chatAppearance = () => window.KovaChatAppearance || null;
  // Activity center (#191): every reply running outside this window and every
  // chat that needs an answer, in one place. It is fed by the same
  // conversation_state events as the sidebar dots, so a turn started in another
  // client appears here too. Downloads stay in their own panel.
  const ACTIVITY_RECENT_MS = 120000;
  const activityAges = new Map();
  const activityOpenButtons = new Set();
  const activityActions = new Map();
  let activityTimer = null;
  function activityAge(started) {
    const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
    return seconds < 90 ? `${Math.max(1, seconds)}s` : `${Math.round(seconds / 60)} min`;
  }
  function noteChatState(id, next, title = '', ephemeral = false, kind) {
    if (typeof id !== 'string' || !id) return;
    const previous = state.activity.get(id);
    kind ||= previous?.kind;
    const isPrivate = ephemeral || previous?.ephemeral || id.startsWith('incognito-');
    const wasRunning = previous && (previous.state === 'working' || previous.state === 'blocked-on-approval');
    const running = next === 'working' || next === 'blocked-on-approval';
    if (running && !wasRunning) {
      state.activityStarted[id] = Date.now();
      state.finishedActivity = state.finishedActivity.filter(item => item.id !== id);
    }
    if (!running && wasRunning) {
      if (!isPrivate) {
        state.finishedActivity.unshift({id, title: title || previous.title || 'Conversation', kind, at: Date.now()});
        state.finishedActivity = state.finishedActivity.slice(0, 3);
      }
      delete state.activityStarted[id];
    }
    state.activity.set(id, {title: isPrivate ? '' : title || previous?.title || '', state: next, ephemeral: Boolean(isPrivate), kind});
  }
  function removeActivity(id) {
    state.activity.delete(id);
    delete state.activityStarted[id];
    state.finishedActivity = state.finishedActivity.filter(item => item.id !== id);
  }
  function activityRows() {
    return [...state.activity.entries()]
      .filter(([, item]) => item.state === 'working' || item.state === 'blocked-on-approval')
      .map(([id, item]) => ({id, ...item}));
  }
  // The desktop tray shows one aggregate: waiting (needs you) beats working beats idle. Only
  // the Electron shell exposes a bridge; kova web and the plain browser skip it.
  function reportTrayState(rows) {
    const next = rows.some(row => row.state === 'blocked-on-approval') ? 'waiting'
      : rows.some(row => row.state === 'working') ? 'working' : 'idle';
    window.kovaDesktop?.reportState?.(next);
  }
  function activityName(row) {
    return row.title || (row.ephemeral ? 'Incognito' : 'New conversation');
  }
  function tickActivity() {
    if (state.finishedActivity.some(item => Date.now() - item.at >= ACTIVITY_RECENT_MS)) {
      renderActivity();
      return;
    }
    for (const [line, started] of activityAges) line.textContent = `Working · ${activityAge(started)}`;
  }
  function syncActivityTimer() {
    if (activityAges.size || state.finishedActivity.length) {
      if (activityTimer == null) activityTimer = setInterval(tickActivity, 1000);
    } else if (activityTimer != null) {
      clearInterval(activityTimer);
      activityTimer = null;
    }
  }
  function activityOpenButton(id, name, kind = 'open') {
    const open = el('button', 'btn quiet small', 'Open'); open.type = 'button';
    open.setAttribute('aria-label', `Open ${name}`);
    open.disabled = state.busy;
    open.onclick = () => {
      if (state.busy) return;
      activityPanel(false, true);
      safe(() => openChat(id));
    };
    activityOpenButtons.add(open);
    activityActions.set(`${kind}:${id}`, open);
    return open;
  }
  function renderActivity() {
    const button = $('activity'), count = $('activity-count'), panel = $('activity-panel');
    if (!button || !count || !panel) return;
    const now = Date.now();
    state.finishedActivity = state.finishedActivity.filter(item => now - item.at < ACTIVITY_RECENT_MS);
    const running = activityRows();
    reportTrayState(running);
    const rows = running.filter(row => row.kind && inCurrentWorkspace(row));
    const finished = state.finishedActivity.filter(row => row.kind && inCurrentWorkspace(row));
    button.hidden = !rows.length && !finished.length;
    button.dataset.active = String(rows.length > 0);
    button.setAttribute('aria-label', rows.length ? `Activity: ${rows.length} running` : 'Activity: recent results');
    count.hidden = !rows.length;
    count.textContent = rows.length ? String(rows.length) : '';
    if (button.hidden && state.activityOpen) { activityPanel(false, true); return; }
    if (!state.activityOpen) { syncActivityTimer(); return; }
    const active = document.activeElement;
    const focused = [...activityActions].find(([, node]) => node === active)?.[0];
    activityAges.clear();
    activityOpenButtons.clear();
    activityActions.clear();
    panel.replaceChildren();
    const head = el('div', 'activity-head');
    head.append(el('strong', '', 'Activity'));
    const close = iconButton('x', 'Close activity');
    close.onclick = () => activityPanel(false, true);
    activityActions.set('close', close);
    head.append(close);
    panel.append(head);
    for (const row of rows) {
      const item = el('div', 'activity-row');
      const info = el('div', 'activity-info');
      const title = el('strong', 'activity-name', activityName(row));
      const stateLine = el('span', 'activity-state', row.state === 'blocked-on-approval'
        ? 'Needs your answer'
        : `Working · ${activityAge(state.activityStarted[row.id] || now)}`);
      if (row.state === 'working') activityAges.set(stateLine, state.activityStarted[row.id] || now);
      info.append(title, stateLine);
      const stopButton = el('button', 'btn quiet small', 'Stop'); stopButton.type = 'button';
      stopButton.setAttribute('aria-label', `Stop ${activityName(row)}`);
      stopButton.onclick = () => safe(async () => {
        if (state.busy && state.chat?.id === row.id) return stop();
        await json(`/chats/${row.id}/stop`, {method: 'POST'});
        await refreshChats();
      });
      activityActions.set(`stop:${row.id}`, stopButton);
      const actions = el('div', 'activity-actions'); actions.append(activityOpenButton(row.id, activityName(row)), stopButton);
      item.append(info, actions); panel.append(item);
    }
    if (finished.length) {
      panel.append(el('div', 'activity-group', 'Recently finished'));
      for (const item of finished) {
        const row = el('div', 'activity-row');
        const info = el('div', 'activity-info');
        info.append(el('strong', 'activity-name', item.title), el('span', 'activity-state', 'Finished'));
        row.append(info, activityOpenButton(item.id, item.title, 'recent')); panel.append(row);
      }
    }
    if (focused) {
      const next = activityActions.get(focused);
      (next && !next.disabled ? next : close).focus({preventScroll: true});
    }
    syncActivityTimer();
  }
  function activityPanel(open, restoreFocus = false) {
    const button = $('activity'), panel = $('activity-panel');
    if (!button || !panel) return;
    const focused = document.activeElement;
    state.activityOpen = open;
    panel.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) {
      renderActivity();
      activityActions.get('close')?.focus();
    } else {
      if (restoreFocus && (focused === button || [...activityActions.values()].includes(focused))) {
        (button.hidden ? $('menu') : button).focus({preventScroll: true});
      }
      activityAges.clear();
      activityOpenButtons.clear();
      activityActions.clear();
      panel.replaceChildren();
      syncActivityTimer();
    }
  }
  $('activity')?.addEventListener('click', () => activityPanel(!state.activityOpen, true));
  // A pin/search only changes the sidebar list; activity keeps the last state
  // of every chat it has seen, so filtering cannot hide a running reply.
  function absorbChatState(chat) {
    noteChatState(chat.id, chat.state, chat.title, chat.ephemeral, chat.kind || 'chat');
  }
  // A short, still skeleton while the first list arrives (#199). It is
  // decoration: the polite status line says what is happening, and a failed
  // first load clears the list instead of leaving it looking busy forever.
  function loadingChats() {
    const list = $('chat-list');
    list.setAttribute('aria-busy', 'true');
    list.replaceChildren(el('p', 'sr-only', 'Loading conversations…'));
    const skeleton = el('div', 'history-skeleton');
    skeleton.setAttribute('aria-hidden', 'true');
    for (let index = 0; index < 4; index += 1) {
      const row = el('div', 'history-skeleton-row');
      row.append(el('span', 'history-skeleton-bar'), el('span', 'history-skeleton-bar'));
      skeleton.append(row);
    }
    list.append(skeleton);
  }
  // Inline rename (#195): double-click a row title, type, Enter saves, Escape
  // restores. The title updates in place through the same PATCH the menu uses.
  function inlineRename(chat, button) {
    if (state.busy || !button) return;
    const input = el('input', 'history-rename');
    input.value = chat.title || '';
    input.maxLength = 200; input.autocomplete = 'off'; input.spellcheck = false;
    input.setAttribute('enterkeyhint', 'done');
    input.setAttribute('aria-label', `Rename ${chat.title || 'New conversation'}`);
    button.replaceWith(input);
    input.focus(); input.select();
    let settled = false;
    const finish = (save, restoreFocus = false) => {
      if (settled) return;
      settled = true;
      const title = input.value.trim();
      input.replaceWith(button);
      if (restoreFocus) button.focus();
      if (!save || !title || title === (chat.title || '')) return;
      safe(async () => {
        await json(`/chats/${chat.id}`, write('PATCH', {title}));
        const listed = state.chats.find(item => item.id === chat.id);
        if (listed) listed.title = title;
        if (state.chat?.id === chat.id) { state.chat.title = title; pageTitle(); }
        history(state.chats, document.activeElement === button ? chat.id : null);
      });
    };
    input.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'Enter') { event.preventDefault(); finish(true, true); }
      else if (event.key === 'Escape') { event.preventDefault(); finish(false, true); }
    });
    input.addEventListener('blur', () => finish(true));
  }
  let historyClick;
  // What a sidebar row says while its chat needs attention, in plain words.
  const CHAT_STATE = {'working': 'Working', 'blocked-on-approval': 'Waiting for your OK'};
  function history(chats, focusChatId = null) {
    const list = $('chat-list');
    const focused = bots && list.contains(document.activeElement) ? document.activeElement : null;
    list.replaceChildren();
    list.removeAttribute?.('aria-busy');
    const query = $('search').value.trim(), searching = !!query;
    let foundBots = false;
    if (!searching) bots?.renderDirectory();
    chats = chats.filter(inCurrentWorkspace);
    if (inBots()) {
      foundBots = bots?.renderContacts(list, chats, query);
      if (!searching) {
        // Groups and each bot's latest thread live in the inbox; older threads list here.
        chats = chats.filter(chat => chat.kind !== 'group' && (!bots?.hasBot(chat.bot_id)
          || (chat.bot_id === state.chat?.bot_id && chat.id !== KovaChatList.latestDirectChat(chats, chat.bot_id)?.id)));
        if (chats.length) list.append(el('h2', 'bot-history-title', 'Earlier threads'));
      }
    }
    if (focused?.closest('.bot-contacts') && list.contains(focused)) focused.focus({preventScroll: true});
    const {sections, ungrouped} = (inBots() ? null : projects)?.render(list, chats, {
      searching, disabled: state.busy || !state.connected,
    }) ?? {sections: [], ungrouped: chats};
    if (!chats.length && !sections.length) {
      const empty = el('div', 'history-empty');
      if (searching) { if (!foundBots) { empty.append(el('span', '', 'No chats found.')); list.append(empty); } controls(); return; }
      if (inBots()) { controls(); return; }
      empty.append(el('span', '', 'No chats yet.'));
      const begin = el('button', 'btn quiet', 'Start a chat'); begin.type = 'button';
      begin.onclick = () => { bots?.close(); $('prompt').focus(); };
      empty.append(begin); list.append(empty);
    }
    // Pinned chats first, then date groups; app.js only renders what the module
    // decided, so the grouping stays testable without a DOM (chat_list.js).
    const groups = globalThis.KovaChatList?.groups(ungrouped) || [{label: '', chats: ungrouped}];
    for (const group of [...sections, ...groups]) {
      if (group.label) list.append(el('div', 'history-group', group.label));
      for (const chat of group.chats) {
        const row = el('div', `history-row${state.chat?.id === chat.id ? ' active' : ''}`);
        chatAppearance()?.decorateRow(row, chat);
        const button = el('button', 'history-open');
        const title = chat.ephemeral ? 'Incognito' : chat.title || 'New conversation';
        const titleRow = el('span', 'history-title');
        if (chat.pinned) titleRow.append(icon('pin'));
        if (chat.ephemeral) titleRow.append(icon('eye-off'));
        titleRow.append(document.createTextNode(title));
        bots?.decorateHistory(titleRow, chat);
        button.append(titleRow); button.title = title;
        // A row is its title. A second line appears only while the chat needs
        // attention (working, or waiting on an approval); the date groups say when.
        if (chat.state && chat.state !== 'done' && chat.state !== 'idle') {
          row.dataset.state = chat.state;
          button.append(el('span', 'history-date', CHAT_STATE[chat.state] || chat.state.replaceAll('-', ' ')));
        }
        button.onclick = event => {
          clearTimeout(historyClick);
          const version = state.navigationVersion;
          const open = () => {
            if (version === state.navigationVersion) safe(() => openChat(chat.id));
          };
          // Pointer clicks need time to become a double-click. Keyboard and
          // assistive technology activation do not.
          if (!event?.detail) open();
          else if (event.detail === 1) historyClick = setTimeout(open, 500);
        };
        button.addEventListener('dblclick', event => {
          event.preventDefault();
          clearTimeout(historyClick);
          inlineRename(chat, button);
        });
        const actions = el('details', 'history-actions'), more = el('summary');
        more.append(icon('ellipsis')); more.title = `Actions for ${title}`; more.setAttribute('aria-label', `Actions for ${title}`);
        actions.append(more); actions.setAttribute('aria-label', `Actions for ${title}`);
        const items = [['pin', chat.pinned ? 'Unpin' : 'Pin', () => togglePin(chat)], ['copy', 'Fork conversation', () => forkChat(chat.id)], ['pencil', 'Rename', () => editChat(chat, false)]];
        if (chatAppearance()) items.push(['sliders-horizontal', 'Colour & icon', () => editAppearance(chat)]);
        if (!inBots() && !chat.ephemeral && (chat.project_id || projects?.hasProjects())) items.push(['folder', 'Move to project', () => projects.move(chat)]);
        items.push(['trash-2', 'Delete', () => editChat(chat, true)]);
        for (const [symbol, name, fn] of items) {
          if (chat.ephemeral && symbol === 'copy') continue;
          const action = el('button', 'btn', name); action.type = 'button'; action.setAttribute('aria-label', `${name} ${title}`);
          action.onclick = () => safe(fn); actions.append(action);
        }
        // A right-click opens the same menu the ellipsis does (#195).
        row.addEventListener('contextmenu', event => {
          if (event.target?.classList.contains('history-rename')) return;
          event.preventDefault();
          clearTimeout(historyClick);
          actions.open = true;
        });
        row.append(button, actions); (group.container || list).append(row);
        if (chat.id === focusChatId) button.focus();
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
    const [chats, projectRows] = await Promise.all([
      json(query ? `/chats/search?workspace=${inBots() ? 'bots' : 'kova'}&q=${encodeURIComponent(query)}` : '/chats'),
      query ? null : projects?.load(),
    ]);
    if (version !== state.searchVersion) return;
    if (!query) projects?.setProjects(projectRows);
    // Quick ask chats are one-off asks; they never clutter the sidebar history.
    const listed = chats.filter(chat => !isQuickChat(chat));
    state.chats = listed;
    // A full list is also the moment a chat deleted elsewhere disappears from
    // activity; a search result must not hide the others.
    if (!query) {
      const live = new Set(listed.map(chat => chat.id));
      for (const id of [...state.activity.keys()]) {
        if (!live.has(id)) removeActivity(id);
      }
    }
    for (const chat of listed) absorbChatState(chat);
    history(listed); renderActivity();
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
        const timeline = el('details'), heading = el('summary', 'privacy-timeline-toggle', report.truncated ? 'Latest 1000 requests (totals include all)' : 'Request timeline');
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
  function sidebar(open, focus = $('sidebar-close')) {
    const drawer = $('sidebar'), backdrop = $('backdrop');
    const wasOpen = document.body.classList.contains('sidebar-open');
    const active = document.activeElement;
    document.body.classList.toggle('sidebar-open', open); backdrop.hidden = !open;
    if (open) {
      drawer.setAttribute('role', 'dialog');
      drawer.setAttribute('aria-modal', 'true');
      drawer.setAttribute('data-focus-trap', '');
      if (!wasOpen || focus !== $('sidebar-close')) {
        const enter = () => {
          focus.focus({preventScroll: true});
          if (focus === $('search')) focus.select();
        };
        enter();
        // Visibility can still be transitioning the first time the drawer opens.
        if (document.activeElement !== focus) Promise.allSettled(
          (drawer.getAnimations?.() || []).map(animation => animation.finished)
        ).then(() => {
          if (document.body.classList.contains('sidebar-open') && document.activeElement === active) {
            enter();
          }
        });
      }
    } else if (wasOpen) {
      drawer.removeAttribute('role');
      drawer.removeAttribute('aria-modal');
      drawer.removeAttribute('data-focus-trap');
      if (active === backdrop || drawer.contains(active)) $('menu').focus({preventScroll: true});
    }
    menuState();
  }
  function toggleSidebar(show, focus) {
    if (inZen()) toggleZen(false);
    if (!wide?.matches) return sidebar(show ?? !document.body.classList.contains('sidebar-open'), focus);
    const collapsed = !(show ?? document.body.classList.contains('sidebar-collapsed'));
    document.body.classList.toggle('sidebar-collapsed', collapsed); menuState();
    try { localStorage.setItem('kova-sidebar', collapsed ? 'collapsed' : 'open'); } catch (_) {}
    focus?.focus(); focus?.select?.();
  }
  function searchConversations() {
    toggleSidebar(true, $('search'));
  }
  const inZen = () => document.body.classList.contains('zen-mode');
  let zenOverride = false;
  function toggleZen(open = !inZen(), {fromTheme = false} = {}) {
    if (!fromTheme) zenOverride = true;
    if (open === inZen()) return;
    if (open) {
      window.KovaSettings?.close?.();
      window.KovaPanel?.close?.();
      if (state.activityOpen) activityPanel(false);
      $('chat-menu').open = false;
      sidebar(false);
    }
    document.body.classList.toggle('zen-mode', open);
    $('zen-exit').hidden = !open;
    if (open && !fromTheme && !document.activeElement?.closest?.('#composer, #messages')) $('prompt').focus();
    if (!open && document.activeElement === $('zen-exit')) $('prompt').focus();
    announce(open ? 'Zen mode on.' : 'Zen mode off.');
  }
  $('zen-enter').onclick = () => toggleZen(true);
  $('zen-exit').onclick = () => toggleZen(false);
  function applyThemeZen(event) {
    if (zenOverride || event?.detail?.preview || typeof getComputedStyle !== 'function') return;
    const value = getComputedStyle(document.documentElement).getPropertyValue('--chat-zen').trim();
    toggleZen(value === 'on', {fromTheme: true});
  }
  document.addEventListener('kova:theme', applyThemeZen);
  applyThemeZen();
  function pageTitle() {
    chatAppearance()?.decorateTopbar?.(state.chat);
    $('incognito-note').hidden = !state.chat?.ephemeral;
    $('chat-menu').hidden = !state.chat;
    $('chat-title').textContent = state.chat?.ephemeral ? 'Incognito' : state.chat?.title || (inBots() ? 'Kova Bots' : 'New chat');
    paintBotChat();
    document.title = state.chat?.ephemeral ? `Incognito · ${state.persona.name}` : state.chat?.title ? `${state.chat.title} · ${state.persona.name}` : state.persona.name;
    // The optional themed desktop title bar shows the same window title as the
    // native one; it is decorative for assistive tech, which reads the window.
    $('titlebar-title').textContent = document.title;
  }
  function paintBotChat() {
    if (!bots) return;
    const chat = state.chat, empty = document.body.classList.contains('is-empty');
    const bot = bots.renderChat(chat, $('chat-title'), empty);
    const label = `Message ${bot?.name || state.persona.name}`;
    $('prompt').placeholder = label;
    $('prompt-label').textContent = label;
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
    const chat = state.chat, draft = $('prompt').value;
    const row = await json(`/chats/${chat.id}/queue`, write('POST', {text}));
    if (state.chat?.id === chat.id) { state.queued.push(row); renderQueue(); }
    consumeDraft(chat, draft);
  }
  async function steerMessage() {
    const chat = state.chat, draft = $('prompt').value, text = draft.trim();
    if (!text || !chat) return;
    const command = parseCommand(text);
    if (command) return dispatchCommand(command);
    await json(`/chats/${chat.id}/steer`, write('POST', {text}));
    consumeDraft(chat, draft);
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
    compare?.dismiss();
    rememberDraft();
    bots?.setWorkChat(chat);
    state.chat = chat;
    if (chat) shell?.setApp(botConversation(chat) ? 'bots' : 'kova');
    composerModules.history?.reset();
    composerModules.mentions?.close();
    // A chat keeps its own model. The welcome screen remembers the model last
    // chosen there, so a reload no longer snaps back to the server default.
    draftModel = chat ? null : restorePickerModel();
    syncComposerModel();
    renderAttachments();
    pageTitle();
    $('mode').value = chat?.mode || 'work';
    $('permission').value = chat?.permission || 'manual';
    $('cwd').value = chat?.cwd || '';
    setWeb(chat?.web ?? true);
    empty(!chat?.messages?.some(m => m.role === 'user'));
    $('messages').replaceChildren();
    window.KovaPlanCard?.reset();
    // A saved turn reads like a live one: every round of it sits under one heading.
    const turns = []; let turn = null, savedTools = null, retryable = false;
    const alternatives = chat?.alternatives;
    const lastUser = chat?.messages?.findLast(msg => msg.role === 'user');
    for (const msg of chat?.messages || []) {
      if (msg.role === 'user') {
        retryable = msg === lastUser;
        message('user', msg.content, undefined, msg.attachments || [], {created: msg.created, edit: retryable});
        turn = null; savedTools = null;
        if (msg.model_switch) modelSwitch(msg.model_switch);
      }
      else if (msg.role === 'assistant') {
        // In a group, each bot's reply to the same message is its own bubble.
        if (turn && (msg.author || null) !== turn.author) { turn = null; savedTools = null; }
        if (!turn) turns.push(turn = {body: message('assistant', undefined, undefined, [], msg), parts: [], tools: 0,
                                      retry: retryable && chat?.kind !== 'group', author: msg.author || null});
        if (msg.content) { savedTools = null; const md = el('div', 'markdown'); markdown(md, msg.content, true); turn.body.append(md); turn.parts.push(msg.content); }
        if (msg.content && !msg.tool_calls?.length) turn.reply = msg;
        if (msg.usage) turn.usage = msg.usage;
        if (msg.tool_calls?.length) {
          turn.tools += msg.tool_calls.length;
          for (const call of msg.tool_calls) {
            if (!savedTools) { savedTools = KovaToolCards.create({saved: true}); turn.body.append(savedTools.node); }
            savedTools.add({id: call.id, name: call.function?.name, args: call.function?.arguments, label: toolLabel(call.function?.name), hint: toolHint(call.function?.arguments)});
          }
        }
      }
      else if (msg.role === 'tool' && savedTools) savedTools.result({id: msg.tool_call_id, output: msg.content});
      // Raw tool payloads stay in the server history, never masquerade as replies.
    }
    for (const saved of turns) if (saved.parts.length) messageActions(saved.body.parentNode, () => saved.parts.join('\n\n'), 'assistant',
      saved.usage, {model: saved.usage?.model || chat?.model, toolCount: saved.tools,
                    created: saved.reply?.created, messageId: saved.reply?.id,
                    alternatives: saved.retry ? alternatives : null,
                    onAlternative: saved.retry && alternatives?.turn_id
                      ? index => safe(() => switchAlternative(alternatives.turn_id, index)) : null,
                    onRetry: saved.retry ? () => safe(() => retrySameModel(saved.usage?.model, saved.reply?.id)) : null});
    if (chat?.plan?.length) $('messages').append(planView(chat.plan));
    $('prompt').value = draftOf(chat); resize();
    if (chat?.id) safe(() => loadChanges(chat.id));
    safe(loadQueue);
    nearBottom = true; requestAnimationFrame(follow); history(state.chats); sidebar(false); controls();
  }
  function modelSwitch(name, before = null) {
    const text = `Switched to ${modelByName(name)?.model || name}`;
    const divider = el('div', 'model-switch', text);
    if (before) $('messages').insertBefore(divider, before); else $('messages').append(divider);
  }
  async function loadChanges(chatId) {
    const version = state.navigationVersion;
    const checkpoints = await json(`/chats/${chatId}/checkpoints`);
    if (version !== state.navigationVersion || state.chat?.id !== chatId || !checkpoints.length) return;
    bots?.addCheckpoints(chatId, checkpoints);
    document.querySelector('.changes-view')?.remove(); // one list, even when a chat opens twice
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
    else if (background === chatId && previous !== 'done') { background = null; safe(() => openChat(chatId, true)); }
  }
  async function openChat(id, keepEditor = false) {
    if (state.busy) return;
    const version = ++state.navigationVersion;
    const chat = await json(`/chats/${id}`);
    if (version === state.navigationVersion && !state.busy) {
      if (state.chat?.id !== id) await discardIncognito();
      if (version !== state.navigationVersion || state.busy) return;
      if (!keepEditor) bots?.close();
      const resetSearch = !inCurrentWorkspace(chat) && !!$('search').value.trim();
      if (resetSearch) $('search').value = '';
      setChat(chat); background = null;
      if (resetSearch) safe(refreshChats);
      renderActivity();
      if (chat.state && chat.state !== 'done') {
        background = id;
        $('messages').append(el('div', 'turn-status background-turn', chat.state === 'blocked-on-approval'
          ? 'This reply is paused until you answer below.' : 'Kova is still working on this reply. It appears here when it finishes.'));
        safe(() => loadPending(id));
      }
      return true;
    }
  }
  async function discardIncognito() {
    if (state.chat?.ephemeral) {
      const id = state.chat.id;
      await json(`/chats/${id}`, {method: 'DELETE'});
      removeActivity(id); renderActivity();
      // Never carry an unsent private draft into a saved conversation.
      $('prompt').value = ''; resize();
    }
  }
  async function changeWorkspace(app) {
    if (state.busy || bots?.saving()) return;
    const version = ++state.navigationVersion;
    const wasIncognito = !!state.chat?.ephemeral;
    await discardIncognito();
    if (state.busy || version !== state.navigationVersion) return;
    bots?.close();
    const wasSearching = !!$('search').value.trim();
    $('search').value = '';
    shell?.setApp(app);
    setChat(null);
    renderActivity();
    if (wasSearching || wasIncognito) await refreshChats();
    // Kova Bots opens on its roster; the inbox beside it holds every conversation.
    if (app === 'bots') {
      $('bot-directory-title').focus();
      if (!wide?.matches) sidebar(true);
    } else $('prompt').focus();
  }

  async function newChat() {
    if (inBots()) return changeWorkspace('kova');
    if (!state.busy) {
      ++state.navigationVersion;
      const wasIncognito = !!state.chat?.ephemeral;
      await discardIncognito();
      bots?.close(); setChat(null); $('prompt').value = ''; $('prompt').focus();
      if (wasIncognito) await refreshChats();
    }
  }
  let startingBotChat = false;
  async function openBotChat(bot, fresh = false) {
    if (state.busy || !state.connected || startingBotChat || bot.state !== 'ok') return;
    startingBotChat = true;
    const version = ++state.navigationVersion;
    try {
      const latest = fresh ? null : KovaChatList.latestDirectChat(await json('/chats'), bot.id);
      if (version !== state.navigationVersion || state.busy) return;
      if (latest) {
        if (await openChat(latest.id)) $('prompt').focus();
        return;
      }
      await discardIncognito();
      if (version !== state.navigationVersion || state.busy) return;
      const chat = await json('/chats', write('POST', {bot_id: bot.id}));
      if (version === state.navigationVersion && !state.busy) {
        bots.close();
        setChat(chat);
        $('prompt').focus();
      }
      await refreshChats();
    } finally { startingBotChat = false; }
  }
  let startingProjectChat = false;
  async function newProjectChat(project) {
    if (state.busy || !state.connected || startingProjectChat) return;
    startingProjectChat = true;
    const version = ++state.navigationVersion;
    try {
      await discardIncognito();
      if (version !== state.navigationVersion || state.busy) return;
      const chat = await json('/chats', write('POST', {project_id: project.id}));
      if (version === state.navigationVersion && !state.busy) {
        projects.open(project.id);
        bots?.close();
        setChat(chat);
        $('prompt').focus();
      }
      await refreshChats();
    } finally {
      startingProjectChat = false;
    }
  }
  async function newIncognito() {
    if (state.busy || !state.connected) return;
    if (inBots()) await changeWorkspace('kova');
    if (inBots() || state.busy) return;
    ++state.navigationVersion;
    await discardIncognito();
    setChat(await json('/chats', write('POST', {ephemeral: true, title: 'Incognito', mode: 'chat', web: false, ...modelForNewChat()})));
    $('prompt').value = ''; $('prompt').focus(); await refreshChats();
  }
  // An incognito chat has no draft key, so its text never enters browser
  // storage. A normal tab close or reload discards the RAM-only chat as well.
  window.addEventListener('pagehide', () => {
    rememberDraft();
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
      removeActivity(chat.id); renderActivity();
      if (state.chat?.id === chat.id) {
        $('prompt').value = '';
        setChat(null);
      }
      draftComponent()?.clear(chat);
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
    if (state.chat?.id === chat.id) { Object.assign(state.chat, updated); pageTitle(); }
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
  let restartNoticeShown = false;
  async function health() {
    try {
      const status = await json('/health');
      if ((status.model || '') !== defaultModel) {
        defaultModel = status.model || ''; syncComposerModel();
      }
      state.localOnly = Boolean(status.local_only);
      $('local-only-status').hidden = !state.localOnly;
      controls();
      if (!state.connected) return;
      clearNotice('offline');
      if (status.restart_available && !restartNoticeShown) {
        restartNoticeShown = true;
        toast('A newer Kova is installed. Run kova restart when you are done working.', 'Kova update');
      }
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
  // A saved change set keys its files by path; a live review lists them.
  function changeFiles(files) {
    if (Array.isArray(files)) return files;
    return Object.entries(files || {}).map(([path, entry]) => ({...entry, path})).sort((a, b) => a.path.localeCompare(b.path));
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
    if (!checkpointId || typeof KovaDiffPanel === 'undefined' || !window.KovaPanel) return details;
    // The full diff, with Undo or Keep per hunk, lives in the side panel (diff-panel.js).
    const row = el('div', 'change-row');
    const open = iconButton('panel-right', `Open ${file.path} from change set ${checkpointId} in the panel`);
    open.onclick = () => KovaDiffPanel.open({chatId, checkpointId, path: file.path, request: json, returnFocus: open,
      onChange: () => { if (state.chat?.id === chatId) safe(() => loadChanges(chatId)); }});
    row.append(details, open);
    return row;
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
        for (const file of changeFiles(entry.files)) if (!affected.has(file.path)) affected.set(file.path, file.status);
      }
      const dialog = el('dialog', 'restore-dialog'); dialog.setAttribute('aria-label', `Restore to change set ${checkpoint.id}`);
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
          toastSuccess(`Restored ${count} file${count === 1 ? '' : 's'}${result.deleted.length ? `, removed ${result.deleted.length}` : ''}.`);
        } catch (error) { confirm.disabled = false; throw error; }
      });
      actions.append(cancel, confirm); dialog.append(actions);
      document.body.append(dialog); dialog.onclose = () => dialog.remove(); dialog.showModal();
    });
  }
  const REVIEW_TABS = 10;   // the side panel keeps at most this many tabs
  function reviewChanges(chatId, checkpointId, files, opener) {
    if (!chatId) return;
    const refresh = () => { if (state.chat?.id === chatId) safe(() => loadChanges(chatId)); };
    files.slice(0, REVIEW_TABS).forEach((file, index) => {
      safe(() => KovaDiffPanel.open({chatId, checkpointId, path: file.path, request: json, returnFocus: opener,
                                     background: index > 0, onChange: refresh}));
    });
    if (files.length > REVIEW_TABS) toast(`Opened the first ${REVIEW_TABS} of ${files.length} files. The rest are in the list.`);
  }
  function reviewCard(checkpoint) {
    const files = changeFiles(checkpoint.files), skipped = checkpoint.skipped || [];
    const card = el('section', 'changes-card');
    card.setAttribute('aria-label', 'Changes Kova made');
    const totals = changeTotals(files);
    const head = el('div', 'changes-head');
    const changed = `Changed ${files.length} file${files.length === 1 ? '' : 's'}`;
    head.append(icon('pencil'), el('strong', '', checkpoint.live ? (files.length ? changed : 'Changed in this turn') : `Change set ${checkpoint.id}`));
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
      const actions = el('div', 'changes-actions');
      // Every changed file opens as a tab in the side panel, the first one on top (#190).
      if (typeof KovaDiffPanel !== 'undefined' && window.KovaPanel) {
        const review = el('button', 'btn', 'Review changes'); review.type = 'button';
        review.setAttribute('aria-label', `Review ${changed.toLowerCase()} in the side panel`);
        review.onclick = () => reviewChanges(state.chat?.id, checkpoint.id, files, review);
        actions.append(review);
      }
      const restore = el('button', 'btn quiet', 'Restore files'); restore.type = 'button';
      restore.onclick = () => confirmRestore(checkpoint);
      actions.append(restore);
      card.append(actions);
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
  // The approval names the action it is asking about, so the question can be
  // answered without reading the arguments first.
  function approvalQuestion(tool) {
    if (tool === 'run_command') return 'Run this command?';
    if (tool === 'write_file') return 'Write this file?';
    if (tool === 'edit_file') return 'Edit this file?';
    if (tool === 'delegate_analysis') return 'Send these tasks to the model?';
    if (tool === 'job_add') return 'Schedule this job?';
    return `Allow ${String(tool || 'this tool').replaceAll('_', ' ')}?`;
  }
  function approvalCard(event) {
    const card = el('section', 'approval-card');
    card.setAttribute('aria-label', 'Tool approval');
    card.setAttribute('role', 'group'); card.setAttribute('tabindex', '-1');
    const heading = el('div', 'approval-heading');
    heading.append(icon('shield-check'), el('strong', '', approvalQuestion(event.tool)), el('span', `risk-badge risk-${event.risk}`, event.risk));
    card.append(heading, el('small', '', event.tool), el('pre', '', event.args?.command || event.args?.path || event.summary));
    if (event.change_summary) card.append(el('small', '', event.change_summary));
    if (event.args && !['run_command', 'delegate_analysis'].includes(event.tool)) {
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
      if (event.tool === 'delegate_analysis' && (decision === 'allow_session' || decision === 'allow_always')) continue;
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
  function turnView(body, model = '') {
    let current = null, scheduled = false, finished = false, usage = null, toolCount = 0;
    let completed = null;
    const blocks = [], approvals = new Map(), toolGroups = [];
    let toolGroup = null;
    let plan = null;
    // Parallel tools and delegated analyses draw as live trees (#192).
    const chatId = state.chat?.id;
    let authorResolved = state.chat?.kind !== 'bot';
    let failedChildren = 0, failureSummary = null;
    const tasks = window.KovaAgentTree?.turn({insert: node => body.insertBefore(node, activity), label: toolLabel, hint: toolHint,
      onFailure: () => {
        failedChildren += 1;
        if (!failureSummary) {
          failureSummary = el('span', 'agent-tree-failures');
          body.parentNode?.querySelector('.message-label')?.append(failureSummary);
        }
        failureSummary.textContent = `${failedChildren} delegated task${failedChildren === 1 ? '' : 's'} failed`;
      },
      stopTask: id => json(`/chats/${chatId}/tasks/${encodeURIComponent(id)}/stop`, write('POST'))});
    const activityText = el('span', 'shimmer', 'Thinking…');
    // The Kova mark with one small movement (#171): still "working", never a
    // loud spinner. thinking.js picks this reply's animation onto the mark.
    const activityMark = el('span', 'thinking-mark'), activityLogo = el('img', 'thinking-logo thinking-anim');
    activityMark.setAttribute('aria-hidden', 'true'); activityLogo.src = '/app/assets/icon.svg'; activityLogo.alt = '';
    activityMark.append(activityLogo,
      el('span', 'thinking-bolt thinking-anim'),
      el('span', 'thinking-orbit thinking-anim'),
      el('span', 'thinking-collide thinking-collide-a thinking-anim'),
      el('span', 'thinking-collide thinking-collide-b thinking-anim'),
      el('span', 'thinking-fill thinking-anim'));
    window.KovaThinking?.apply(activityMark);
    const activity = el('div', 'activity'); activity.append(activityMark, activityText); body.append(activity);
    // The live node at the end of the trace takes the channel of what runs now.
    activity.dataset.step = 'think';
    function paint() {
      scheduled = false;
      for (const block of blocks) {
        if (block.kind === 'text' && block.dirty) { markdown(block.node, block.text, finished); block.dirty = false; }
        // Only the block being written right now carries the live cursor.
        block.node.classList.toggle('live', !finished && block === current && !block.end);
      }
      follow();
    }
    function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(paint); } }
    // While it thinks, the activity row is the one quiet line. When this burst
    // ends, KovaReasoning folds it into "Thought for Ns" and owns the setting
    // that hides it; app.js never builds the reasoning text as markup.
    function endReasoning() {
      if (current?.kind !== 'reasoning' || current.end) return;
      current.end = performance.now();
      const folded = window.KovaReasoning?.row(current.start, current.end, current.text);
      if (folded) body.insertBefore(folded, activity);
      current = null;
    }
    return {
      accept(event) {
        const kind = event.type;
        if (!authorResolved && (event.author || ['reasoning', 'text', 'tool_call', 'done'].includes(kind))) {
          authorResolved = true;
          bots?.labelReply(body.parentNode.querySelector('.message-label'), event.author, event.author_name, state.persona.name);
        }
        if (kind !== 'reasoning') endReasoning();
        if (tasks?.accept(event)) {
          current = null; toolGroup = null;
          if (kind === 'tool_call') toolCount += 1;
          activity.hidden = kind !== 'tool_result'; activityText.textContent = 'Working…'; activity.dataset.step = 'tool';
          schedule(); return;
        }
        if (kind === 'text' || kind === 'reasoning') {
          // A new answer or thinking burst starts a new region, so any tool
          // card that was open ends here.
          toolGroup = null;
          if (kind === 'reasoning') {
            // The reasoning text waits until the burst ends; until then the
            // thinking row is the whole visible state.
            activity.hidden = false;
            activityText.textContent = 'Thinking…'; activity.dataset.step = 'think';
            if (!current || current.kind !== kind) current = {kind, text: '', start: performance.now(), end: 0};
            current.text += event.text || '';
          } else {
            // The answer is visible itself, so the indicator steps aside.
            activity.hidden = true;
            if (!current || current.kind !== kind) {
              current = {kind, text: '', dirty: true, start: performance.now()};
              current.node = el('div', 'markdown');
              body.insertBefore(current.node, activity); blocks.push(current);
            }
            current.text += event.text; current.dirty = true;
          }
        } else if (kind === 'compact_skipped') {
          toast(event.message);
        } else if (kind === 'compacted') {
          current = null;
          const details = el('details', 'turn-status');
          details.append(el('summary', '', `Compacted ${event.count} messages`), el('pre', '', event.summary));
          body.insertBefore(details, activity);
        } else if (kind === 'tool_call') {
          current = null; activity.hidden = true; toolCount += 1;
          if (!toolGroup) { toolGroup = KovaToolCards.create(); toolGroups.push(toolGroup); body.insertBefore(toolGroup.node, activity); }
          toolGroup.add({...event, label: toolLabel(event.name), hint: toolHint(event.args)});
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
          // Staying on this computer is the default and says nothing; leaving it is news.
          if (hosts.length) body.insertBefore(el('div', 'turn-status', `Network: ${hosts.join(', ')} · ${remote.reduce((n, item) => n + item.bytes_out, 0)} body bytes prepared.`), activity);
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
          // The pinned card owns this plan until the turn ends, then it hands
          // the same node back to the transcript (#193).
          window.KovaPlanCard?.update(event.items, next);
        } else if (kind === 'review') {
          current = null; activity.hidden = true;
          const checkpointId = event.checkpoint_id;
          if (checkpointId && typeof KovaDiffPanel !== 'undefined' && window.KovaPanel) {
            const paths = new Set(changeFiles(event.files).map(file => file.path));
            for (const group of toolGroups) {
              group.enableFileReview(paths, (path, opener) => reviewChanges(chatId, checkpointId, [{path}], opener));
            }
          }
          if (event.files?.length || event.skipped?.length) {
            body.insertBefore(reviewCard({id: event.checkpoint_id, label: event.label, live: true,
                                          files: event.files || [], skipped: event.skipped || []}), activity);
          }
        } else if (kind === 'tool_result') {
          toolGroup?.result(event);
          current = null; activity.hidden = false; activityText.textContent = 'Working…'; activity.dataset.step = 'tool';
        } else if (kind === 'error') {
          // One announcement for a failure: the live region, not a second alert.
          const error = el('div', 'turn-error'); error.append(icon('triangle-alert'), el('span', '', event.message));
          body.insertBefore(error, activity);
          announce(`Error: ${event.message}`);
        } else if (kind === 'group_status' || (kind === 'notice' && ['group_nobody', 'bot_limit'].includes(event.kind))) {
          current = null;
          body.insertBefore(el('div', 'turn-status', event.text || event.notice), activity);
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
          completed = event;
          const budget = event.budget_warning;
          if (budget?.monthly_usd) toast(`This month's estimated spend is ~${money(budget.spent_usd)} of your ${money(budget.monthly_usd)} budget. Nothing is blocked.`, 'Budget', {severity: 'warning'});
        }
        schedule();
      },
      finish() {
        window.KovaPlanCard?.finish();
        tasks?.finish();
        endReasoning(); finished = true; activity.remove();
        for (const approval of approvals.values()) approval.finish();
        for (const group of toolGroups) group.finish();
        for (const block of blocks) block.dirty = true;
        paint();
        const reply = blocks.filter(block => block.kind === 'text').map(block => block.text).join('\n\n');
        if (reply.trim() && body.parentNode) messageActions(body.parentNode, () => reply, 'assistant', usage,
          {model: usage?.model || model, toolCount, created: completed?.created,
            messageId: completed?.message_id, alternatives: completed?.alternatives,
            onAlternative: completed?.alternatives?.turn_id
              ? index => safe(() => switchAlternative(completed.alternatives.turn_id, index)) : null,
            onRetry: () => safe(() => retrySameModel(usage?.model, completed?.message_id))});
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
  async function forkChat(id = state.chat?.id, through = null) {
    if (state.chat?.ephemeral && id === state.chat.id) throw new Error('Incognito chats cannot be forked or exported.');
    if (!id) throw new Error('Open a conversation to fork first.');
    if (state.busy) throw new Error('Wait for the current reply before forking.');
    const suffix = through == null ? '' : `?through=${encodeURIComponent(through)}`;
    const chat = await json(`/chats/${id}/fork${suffix}`, {method: 'POST'});
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
      const chats = args ? (await json('/chats')).filter(inCurrentWorkspace) : null;
      const n = Number(args);
      if (args && (!/^\d+$/.test(args) || n < 1 || !chats[n - 1])) throw new Error('Use /fork [n], with a conversation number from history (newest first).');
      await forkChat(args ? chats[n - 1].id : state.chat?.id);
    } else if (name === 'compact' && !args) await compactChat();
    else if (name === 'mode' && ['chat', 'work'].includes(args)) {
      await chatSetting({mode: args}); $('mode').value = args; controls();
    } else if (name === 'web' && ['on', 'off'].includes(args)) {
      await chatSetting({web: args === 'on'}); setWeb(args === 'on'); controls();
    } else if (name === 'model' && args) {
      await chooseComposerModel(args);
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
  $('compact-now').onclick = () => { $('chat-menu').open = false; safe(compactChat); };
  // Escape closes the chat actions menu and gives focus back to its toggle.
  $('chat-menu').addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !$('chat-menu').open) return;
    event.preventDefault(); event.stopPropagation(); $('chat-menu').open = false; $('chat-menu').querySelector('summary').focus();
  });
  async function send(event) {
    event.preventDefault();
    const chat = state.chat, draft = $('prompt').value;
    let text = draft.trim();
    // Quick ask includes the selection the shell read, delimited so the model
    // can tell it apart from the question. The chip already told the user.
    if (isQuick && quickSelection && text) text = `${text}\n\n<selection>\n${quickSelection.trim()}\n</selection>`;
    if ((!text && !pendingAttachments.length) || readingAttachments || !state.connected) return;
    const command = text ? parseCommand(text) : null;
    if (command) {
      try { await dispatchCommand(command); consumeDraft(chat, draft); commandPicker(); composerModules.history?.reset(); composerModules.mentions?.close(); }
      catch (error) { toastError(error.message); }
      return;
    }
    if (state.comparing) return toast('Keep an answer or stop the comparison first.');
    if (state.busy && pendingAttachments.length) return toast('Send attachments after the current reply.');
    if (state.busy) return safe(() => queueMessage(text));
    await sendText(text || 'Please read the attached files.', {draft});
  }
  async function sendText(text, sendOptions = {}) {
    if ((!text && !sendOptions.action) || state.busy || pickerSaving || !state.connected) return;
    if (inBots() && !state.chat) return;
    ++state.navigationVersion;
    compare?.dismiss();
    state.busy = true; state.turnReady = false; state.stopRequested = false; controls();
    let turn, terminal = false, submittedChat = null;
    try {
      if (!state.chat) state.chat = await createComposerChat();
      const outgoing = sendOptions.queued || sendOptions.action ? [] : pendingAttachments.filter(a => a.chat_id === state.chat.id);
      // Send to the model the chip showed, even if another window just changed
      // the default or the saved chat settings. A placeholder entry with no
      // model id sends null so the server explains what to set up.
      const model = sendOptions.model || sendableModel();
      const action = sendOptions.action || 'messages';
      const payload = action === 'retry' ? {model: sendOptions.model || null, expected_reply: sendOptions.messageId || null} : {text, model,
        attachments: outgoing.map(a => a.id), ...(sendOptions.queued ? {queued: sendOptions.queued} : {})};
      const stream = () => api(`/chats/${state.chat.id}/${action}`, write('POST', payload));
      const replacement = sendOptions.clear ? await stream() : null;
      if (replacement) clearLastTurn(sendOptions.clear === 'turn');
      if (sendOptions.draft !== undefined && $('prompt').value === sendOptions.draft) {
        submittedChat = state.chat;
        consumeDraft(submittedChat, sendOptions.draft);
      }
      if (sendOptions.omitUser) empty(false);
      else {
        empty(false);
        $('messages').querySelectorAll('.message-action-edit').forEach(button => button.remove());
        message('user', text, undefined, outgoing, {created: new Date().toISOString(), edit: true});
        options(false);
      }
      $('messages').querySelectorAll('.message-action-retry').forEach(button => button.remove());
      composerModules.history?.reset(); composerModules.mentions?.close();
      bots?.beginWork(outgoing);
      let reply = message('assistant', undefined, undefined, [], {author: bots?.contact(state.chat)?.id});
      let groupTurns = 0;
      nearBottom = true; turn = turnView(reply, model); follow();
      const response = replacement || await stream();
      submittedChat = null;
      if (!sendOptions.queued && !sendOptions.action) { for (let i = pendingAttachments.length - 1; i >= 0; i--) { if (pendingAttachments[i].chat_id === state.chat.id) pendingAttachments.splice(i, 1); } renderAttachments(); }
      state.turnReady = true;
      if (state.stopRequested) await cancelTurn();
      await readEvents(response, data => {
        if (terminal) return;
        if (data.type === 'model_switch') { modelSwitch(data.model, reply.parentNode); follow(); return; }
        if (data.type === 'done' && !state.chat?.ephemeral && data.usage?.local && data.usage.tokens_per_second) {
          const entry = modelByName(data.model);
          if (entry) entry.last_local_speed = data.usage;
        }
        // A group answers one bot at a time: each bot gets its own reply.
        if (data.type === 'bot_turn_start') {
          // The first placeholder was made before the server said who answers:
          // swap it for one under this bot, so its name and colour show live.
          const placeholder = !groupTurns++ ? reply.closest('article') : null;
          turn.finish();
          const next = message('assistant', undefined, undefined, [], {author: data.author});
          if (placeholder) { placeholder.replaceWith(next.closest('article')); }
          reply = next;
          turn = turnView(reply, model); follow();
          return;
        }
        if (data.type === 'bot_turn_end' && data.status === 'error') {
          turn.accept({type: 'group_status', text: `@${data.author} could not answer: ${data.message || 'error'}`});
          return;
        }
        if (data.type === 'group_limit') {
          const labels = {max_hops: 'reply limit', max_bot_turns: 'bot turn limit', ping_pong: 'back-and-forth limit',
            max_tokens: 'token limit', max_cost_usd: 'cost limit', max_seconds: 'time limit', repeat: 'repeated reply'};
          const left = (data.dropped || []).map(id => '@' + id).join(', ');
          turn.accept({type: 'group_status', text: `The group stopped at its ${labels[data.limit] || data.limit}${left ? `; still waiting: ${left}` : ''}.`});
          return;
        }
        if (['group_plan', 'group_route', 'bot_turn_end'].includes(data.type)) return;
        terminal = ['done', 'error', 'stopped'].includes(data.type);
        bots?.workEvent(data);
        turn.accept(data);
      });
      if (!terminal) throw new Error('Reply connection interrupted. Reopen this conversation to check saved messages; it may still be running.');
    } catch (error) {
      if (submittedChat && submittedChat.id === state.chat?.id && !$('prompt').value) {
        $('prompt').value = sendOptions.draft;
        rememberDraft(); resize();
      }
      if (error.offline) notice(error.message, 'offline');
      if (turn) turn.accept({type: 'error', message: error.message}); else if (!error.offline) toastError(error.message);
    } finally {
      const interrupted = state.stopRequested;
      turn?.finish(); state.busy = false; state.turnReady = false; state.stopRequested = false; controls();
      if (document.activeElement === $('prompt')) $('prompt').focus({preventScroll: true});
      if (state.connected) {
        try {
          // Update title/history without replacing the live reasoning and tool rows.
          if (state.chat) {
            const previous = composerModel();
            state.chat = await json(`/chats/${state.chat.id}`); pageTitle();
            bots?.setWorkChat(state.chat);
            const current = composerModel();
            if (current.model !== previous.model || current.reasoning_effort !== previous.reasoning_effort) syncComposerModel();
          }
          await refreshChats(); await health();
        } catch (error) { report(error); }
      }
      if (!interrupted) {
        try { await runNextQueued(); } catch (error) { report(error); }
      }
      if (isQuick) renderQuickControls();
    }
  }
  async function cancelTurn() {
    try { await json(`/chats/${state.chat.id}/stop`, {method: 'POST'}); }
    catch (error) { state.stopRequested = false; controls(); toastError(error.message); }
  }
  async function stop() {
    if (!state.busy) return;
    state.stopRequested = true; controls();
    // A click while the chat/stream is opening is applied as soon as headers arrive.
    if (state.turnReady) await cancelTurn();
  }
  // Losing Kova itself is said once, by the box (with Retry); anything else is a toast.
  function report(error) { if (error.offline) notice(error.message, 'offline'); else toastError(error.message); }
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
    shell?.setApp('kova');
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
    composerModules.history?.reset();
    resize();
    if (draft.files?.length) await attachFiles(draft.files);
  }
  function fileSize(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }
  function renderAttachments() {
    $('attachment-chips').replaceChildren();
    for (const item of pendingAttachments.filter(a => a.chat_id === state.chat?.id)) {
      const chip = el('span', 'attachment-chip');
      const name = el('span', 'attachment-chip-name', item.name || 'Attached file');
      name.title = item.name || 'Attached file';
      const meta = el('span', 'attachment-chip-meta', fileSize(item.size) + (item.pages ? ` · ${item.pages} pages` : ''));
      const remove = el('button', 'attachment-remove'); remove.type = 'button';
      remove.setAttribute('aria-label', `Remove ${item.name || 'attached file'}`);
      remove.title = `Remove ${item.name || 'attached file'}`;
      remove.append(icon('x'));
      remove.onclick = () => safe(async () => {
        await json(`/chats/${item.chat_id}/attachments/${item.id}`, {method: 'DELETE'});
        pendingAttachments.splice(pendingAttachments.indexOf(item), 1); renderAttachments(); controls();
      });
      chip.append(name, meta, remove); $('attachment-chips').append(chip);
    }
    $('attachment-note').hidden = !pendingAttachments.some(a => a.chat_id === state.chat?.id);
  }
  async function attachFiles(files) {
    if (state.chat?.ephemeral) return toast('Attachments are disabled in incognito.');
    if (readingAttachments || state.busy || pickerSaving || !state.connected) return;
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
      if (!state.chat) state.chat = await createComposerChat();
      const chatId = state.chat.id;
      for (const file of accepted) {
        const form = new FormData(); form.append('file', file);
        pendingAttachments.push(await json(`/chats/${chatId}/attachments`, {method: 'POST', body: form}));
      }
    } catch (error) { toastError(error.message); }
    finally { readingAttachments = false; renderAttachments(); controls(); }
  }
  $('attach').onclick = () => $('attachment-input').click();
  $('attachment-input').onchange = event => { attachFiles([...event.target.files]); event.target.value = ''; };
  $('composer').addEventListener('dragover', event => { event.preventDefault(); });
  $('composer').addEventListener('drop', event => { if (!event.dataTransfer.files.length) return; event.preventDefault(); attachFiles([...event.dataTransfer.files]); });
  // A screenshot on the clipboard arrives as a File in most browsers and as a
  // DataTransferItem in Safari; accept both, and give an unnamed image a name
  // the chip can show.
  function pastedFiles(event) {
    const transfer = event.clipboardData;
    if (!transfer) return [];
    const files = [...(transfer.files || [])];
    if (files.length) return files;
    return [...(transfer.items || [])]
      .filter(item => item.kind === 'file')
      .map(item => item.getAsFile?.())
      .filter(Boolean);
  }
  function namedUpload(file) {
    if (file.name) return file;
    const kind = file.type || 'image/png';
    const ext = (kind.split('/')[1] || 'png').replace('jpeg', 'jpg').replace('+xml', '');
    return new File([file], `Pasted image.${ext}`, {type: kind});
  }
  $('prompt').addEventListener('paste', event => {
    const files = pastedFiles(event);
    if (files.length) { event.preventDefault(); attachFiles(files.map(namedUpload)); }
  });
  // The two composer extras (#197). Their modules draw and move their own
  // surfaces; app.js only supplies the chat history and the path request.
  function promptChanged() { resize(); commandPicker(); }
  composerModules.history = globalThis.KovaComposerHistory?.attach?.($('prompt'), {
    messages: () => state.chat?.messages || [],
    onChange: () => { promptChanged(); composerModules.mentions?.input(); },
  }) || null;
  composerModules.mentions = globalThis.KovaComposerMentions?.attach?.({
    textarea: $('prompt'), listbox: $('mention-list'), onChange: promptChanged,
    paths: async fragment => {
      const base = state.chat ? `/chats/${encodeURIComponent(state.chat.id)}/files` : '/files';
      const result = await json(`${base}?q=${encodeURIComponent(fragment)}&limit=20`);
      return result.paths || [];
    },
  }) || null;
  // The message box is the target, not just its ~35px text line (#201): a
  // click on the card's padding focuses the prompt. Controls inside the card
  // keep their own click.
  $('composer').addEventListener('click', event => {
    if (event.target.closest('button, a, input, select, textarea, label, summary')) return;
    $('prompt').focus();
  });
  $('composer').addEventListener('submit', send);
  $('prompt').addEventListener('input', () => { promptChanged(); rememberDraft(); composerModules.mentions?.input(); });
  $('prompt').addEventListener('keydown', event => {
    // The mention list owns its arrows and Enter while it is open.
    if (composerModules.mentions?.keydown(event)) return;
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $('composer').requestSubmit(); }
    // Like a shell: Up at the start of an empty box brings back what you said.
    composerModules.history?.keydown(event);
  });
  // Compare answers (#96) lives in compare.js; app.js lends it the chat and busy state.
  compare = globalThis.KovaCompare?.create({
    json, api, write, readEvents, markdown, toastError, follow,
    models: () => state.models || [], localOnly: () => !!state.localOnly,
    currentModel: () => composerModel().model, draft: () => $('prompt').value,
    busy: () => state.busy || pickerSaving || !state.connected,
    setBusy: flag => {
      state.busy = state.comparing = flag; state.turnReady = false;
      if (!flag) state.stopRequested = false;
      controls();
    },
    setStreaming: () => { state.turnReady = true; if (state.stopRequested) safe(cancelTurn); },
    ensureChat: async () => (state.chat ||= await createComposerChat()),
    consumeDraft, restoreFocus: () => $('prompt').focus(),
    mount: node => { empty(false); options(false); $('messages').append(node); nearBottom = true; },
    kept: async chatId => { await openChat(chatId); await refreshChats(); },
  }) || null;
  $('compare-open').hidden = !compare;
  $('compare-open').onclick = () => compare?.choose();
  $('stop').onclick = stop; $('new-chat').onclick = newChat; $('new-incognito').onclick = () => safe(newIncognito);
  $('new-workspace-bot').onclick = () => bots?.openEditor(null, $('new-workspace-bot'));
  $('queue').onclick = () => safe(() => queueMessage($('prompt').value.trim()));
  $('steer').onclick = () => safe(steerMessage);
  $('menu').onclick = () => toggleSidebar();
  try { if (localStorage.getItem('kova-sidebar') === 'collapsed') document.body.classList.toggle('sidebar-collapsed', true); } catch (_) {}
  menuState(); wide?.addEventListener?.('change', () => { sidebar(false); });
  $('backdrop').onclick = () => sidebar(false); $('sidebar-close').onclick = () => sidebar(false);
  $('sidebar').addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.isComposing || event.defaultPrevented ||
        !document.body.classList.contains('sidebar-open')) return;
    event.preventDefault();
    event.stopPropagation();
    sidebar(false);
  });
  $('open-shortcuts').onclick = () => { sidebar(false); $('shortcuts-dialog').showModal(); };
  $('shortcuts-close').onclick = () => $('shortcuts-dialog').close();
  // Command palette (#183). palette.js owns the dialog; this is only the registry,
  // so every menu action below stays reachable from the keyboard too.
  // Shortcut labels come from shortcuts.js, so a row shows the keys the user
  // has actually recorded, not the defaults (#200).
  const shortcutKeys = id => globalThis.KovaShortcuts?.current?.labels?.(id) || [];
  let paletteChats = [];
  // Chat-content hits from GET /chats/search, filled in as the user types; the
  // sidebar's own titles-only filter never looked inside a message.
  let paletteMatches = [];
  // MCP servers and installed plugins, so the palette can switch them on or off.
  let paletteMcp = [];
  let palettePlugins = [];
  function paletteItems() {
    const items = [];
    const add = (group, item) => items.push({group, ...item});
    const current = state.chat && !state.chat.ephemeral ? state.chat : null;
    const panel = (button, title, hint, keywords) => ({title, hint, keywords, run: () => $(button).onclick?.()});
    if (!inBots()) add('Actions', {title:'New chat', hint:'Start with an empty message box', keywords:'create start fresh compose', shortcut:shortcutKeys('new-conversation'), run:() => safe(newChat)});
    if (bots) add('Actions', {title: inBots() ? 'Open Kova Chat' : 'Open Kova Bots',
      hint: inBots() ? 'Go to Chat and Work' : 'Browse your bots and their conversations',
      keywords: 'switch workspace', run: () => safe(() => changeWorkspace(inBots() ? 'kova' : 'bots'))});
    if (inBots()) for (const item of bots?.paletteActions() || []) add('Bots', item);
    if (compare && !inBots()) add('Actions', {title:'Compare answers', hint:'Ask several models and keep one', keywords:'side by side models versus', run:() => { if (!state.busy) compare.choose(); }});
    if (!inBots()) add('Actions', {title:'New incognito chat', hint:'Not saved on this PC', keywords:'private ephemeral temporary', shortcut:shortcutKeys('new-incognito'), run:() => safe(newIncognito)});
    add('Actions', {title:'Search conversations', hint:'Filter the conversation list', keywords:'find history chats', shortcut:shortcutKeys('search-conversations'), run:searchConversations});
    add('Actions', {title:'Previous conversation', hint:'Move up the conversation list', keywords:'chat history navigate up previous older', shortcut:shortcutKeys('previous-conversation'), run:() => stepConversation(-1)});
    add('Actions', {title:'Next conversation', hint:'Move down the conversation list', keywords:'chat history navigate down next newer', shortcut:shortcutKeys('next-conversation'), run:() => stepConversation(1)});
    add('Actions', {title:'Keyboard shortcuts', hint:'Every key this window understands', keywords:'keys help cheat sheet', shortcut:shortcutKeys('shortcuts'), run:() => $('shortcuts-dialog').showModal()});
    add('Actions', {title:'Show or hide the sidebar', hint:'Give the conversation more room', keywords:'drawer panel collapse', shortcut:shortcutKeys('toggle-sidebar'), run:() => toggleSidebar()});
    if (window.KovaPanel?.tabCount?.()) add('Actions', {title:'Show or hide the side panel', hint:'Diffs, code and output opened beside the chat', keywords:'panel tabs right side drawer', shortcut:shortcutKeys('toggle-panel'), run:() => window.KovaPanel.toggle()});
    add('Actions', {title:'Move focus to the next pane', hint:'Sidebar, conversation, side panel', keywords:'pane focus region f6 sidebar panel', shortcut:shortcutKeys('next-pane'), run:() => window.KovaPanes?.focusNext?.()});
    if (state.busy) add('Actions', {title:'Stop the reply', hint:'Cancel the turn that is running now', keywords:'cancel halt interrupt', shortcut:shortcutKeys('stop-reply'), run:() => safe(stop)});
    add('Actions', {title:'Sign out of this window', hint:'End this browser session', keywords:'logout sign out leave disconnect', run:() => $('disconnect').onclick?.()});

    add('Appearance', {title:`Switch to ${document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'} theme`, hint:'Light and dark', keywords:'theme colour color mode appearance', shortcut:shortcutKeys('toggle-theme'), run:() => $('theme').onclick?.()});
    add('Appearance', {title:inZen() ? 'Exit zen mode' : 'Enter zen mode', hint:'Show or hide the chat chrome', keywords:'focus distraction sidebar', shortcut:shortcutKeys('toggle-zen'), run:() => toggleZen()});
    const themes = globalThis.KovaTheme;
    for (const option of themes?.choices?.() || []) {
      if (option.id === themes.choice()) continue;
      // Highlighting a theme row shows it, Escape puts the old one back, and
      // Enter is the only thing that saves it (#183).
      add('Appearance', {title:`Theme: ${option.name}`, hint:option.id === 'system' ? 'Follow this device' : option.source === 'file' ? `Installed theme, ${option.appearance}` : `Built-in ${option.appearance} theme`, keywords:`theme colour color appearance ${option.id}`, previewKey:`theme:${option.id}`, preview:() => themes.preview?.(option.id), previewEnd:() => themes.endPreview?.(), run:() => themes.set(option.id)});
    }
    // Dialog-specific theme actions live only inside the Settings view (#183);
    // the palette reaches them directly so every menu action is one command away.
    add('Appearance', {title:'Customize theme', hint:'Change the colours of the current theme', keywords:'theme editor colours colors customize contrast', run:() => window.KovaThemeEditor?.open()});
    add('Appearance', {title:'Browse themes', hint:'Preview and apply a theme from the gallery', keywords:'theme gallery browse install preview', run:() => window.KovaThemeGallery?.open()});

    if (current) {
      add('This conversation', {title:'Compact now', hint:'Summarise older turns to free context', keywords:'summarise summarize shorten context', run:() => safe(compactChat)});
      add('This conversation', {title:'Fork this conversation', hint:'Continue in an independent copy', keywords:'copy duplicate branch', run:() => safe(() => forkChat(current.id))});
      add('This conversation', {title:'Rename this conversation', hint:current.title || 'New conversation', keywords:'title name edit', run:() => safe(() => editChat(current, false))});
      add('This conversation', {title:'Delete this conversation', hint:'Remove it from this computer', keywords:'remove trash discard', run:() => safe(() => editChat(current, true))});
      add('This conversation', {title:current.pinned ? 'Unpin this conversation' : 'Pin this conversation', hint:'Keep it at the top of the list', keywords:'pin pinned top favourite favorite', run:() => safe(() => togglePin(current))});
      add('This conversation', {title:'Colour & icon', hint:'Change how this conversation looks', keywords:'colour color icon appearance avatar', run:() => safe(() => editAppearance(current))});
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

    // Reasoning effort (#183): the same levels the composer's model picker
    // offers for this model, so the palette never invents one the server would
    // reject. An empty level means "let the model decide".
    if (!state.busy) {
      const selection = composerModel();
      const supported = modelByName(selection.model)?.reasoning_efforts || [];
      if (supported.length) {
        const chosen = supported.includes(selection.reasoning_effort) ? selection.reasoning_effort : '';
        for (const level of ['', ...supported]) {
          add('Reasoning', {
            title: `Reasoning effort: ${effortLabels[level] || level}`,
            hint: level === chosen ? 'Current level' : level ? 'For this conversation' : 'Let the model choose',
            keywords: `reasoning effort thinking budget ${level || 'automatic'}`,
            run: () => safe(() => chooseComposerModel(selection.model, level || null)),
          });
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
    add('Settings', {title:'Reload without custom CSS', hint:'Safe mode, in case a stylesheet broke the page', keywords:'safe custom css stylesheet broken reset recovery', run:() => {
      const url = new URL(location.href);
      url.searchParams.set('safe', '1');
      location.assign(url);
    }});
    add('Settings', {title:'Model settings…', hint:'Vision and the active model', keywords:'model vision context', run:() => safe(openModelSettings)});
    add('Settings', {title:'Permission rules…', hint:'Pre-approve tools with patterns', keywords:'rules allow deny approvals', run:() => safe(openRules)});

    for (const model of state.models) {
      if (state.busy || bots?.botModel(state.chat)) break;
      add('Models', {title:`Use ${model.model || model.name}`, hint:model.provider_name || 'Custom provider', keywords:`model provider ${model.name}`, run:() => { $('model').value = model.name; $('model').onchange?.(); }});
    }
    // Model-picker actions (#183): the chip's Favourite and Make default rows.
    if (!state.busy && composerModel().model && modelByName(composerModel().model)) {
      add('Models', {title:'Favourite this model', hint:'Pin it to the top of the model list', keywords:'model favourite favorite pin star', run:() => $('model-favourite').onclick?.()});
      add('Models', {title:'Make this model the default', hint:'Use it for every new chat', keywords:'model default active new', run:() => $('model-make-default').onclick?.()});
    }
    // MCP servers and extensions can be switched from here because the app
    // already has the endpoints the two panels use (#183).
    for (const server of paletteMcp) {
      const tools = Array.isArray(server.tools) ? server.tools.filter(tool => tool.enabled !== false).length : 0;
      add('MCP servers', {
        title: `${server.enabled ? 'Turn off' : 'Turn on'} MCP server: ${server.name}`,
        hint: [server.enabled ? 'On' : 'Off', server.transport, server.state || '', `${tools} tool${tools === 1 ? '' : 's'}`].filter(Boolean).join(' · '),
        keywords: `mcp server ${server.name} tools enable disable switch`,
        run: () => safe(() => paletteSetEnabled(`/mcp/${encodeURIComponent(server.name)}`, {enabled: !server.enabled})),
      });
    }
    for (const plugin of palettePlugins) {
      add('Extensions', {
        title: `${plugin.enabled ? 'Turn off' : 'Turn on'} extension: ${plugin.name}`,
        hint: [plugin.enabled ? 'On' : 'Off', plugin.version ? `v${plugin.version}` : '', plugin.description || ''].filter(Boolean).join(' · '),
        keywords: `extension plugin add-on ${plugin.name} enable disable switch`,
        run: () => safe(() => paletteSetEnabled(`/extensions/plugins/${encodeURIComponent(plugin.name)}`, {enabled: !plugin.enabled})),
      });
    }
    // Content hits come first in their group and inside a conversation title
    // never repeats them: the snippet is the point.
    const shown = paletteMatches.filter(inCurrentWorkspace).slice(0, 20);
    const matched = new Set(shown.map(row => row.id));
    for (const row of shown) {
      const snippet = String(row.snippet || '').replace(/\s+/g, ' ').trim();
      add('In messages', {
        title: row.title || 'New conversation',
        hint: `${row.role === 'user' ? 'You' : 'Kova'}: ${snippet || 'Match in this conversation'}`,
        keywords: `chat conversation history message match ${row.role || ''}`,
        alwaysMatch: true,
        run: () => safe(() => openChat(row.id)),
      });
    }
    for (const chat of paletteChats.filter(inCurrentWorkspace).slice(0, 50)) {
      if (state.busy) break;
      if (matched.has(chat.id)) continue;
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
  async function paletteSetEnabled(url, body) {
    await json(url, write('PATCH', body));
    await paletteLoad();
  }
  async function paletteLoad() {
    const read = url => json(url).catch(() => null);
    const [chats, servers, plugins] = await Promise.all([read('/chats'), read('/mcp'), read('/extensions/plugins')]);
    if (Array.isArray(chats)) paletteChats = chats.filter(chat => !isQuickChat(chat));
    if (Array.isArray(servers)) paletteMcp = servers;
    if (Array.isArray(plugins)) palettePlugins = plugins;
  }
  // Chat content is searched on the server; the registry stays synchronous, so
  // the answer is dropped into a list and the open palette re-renders (#183).
  let paletteSearchTimer, paletteSearchVersion = 0, paletteMatchQuery = '';
  async function paletteRunSearch(query, version) {
    let rows = [];
    try { rows = await json(`/chats/search?workspace=${inBots() ? 'bots' : 'kova'}&q=${encodeURIComponent(query)}&limit=20`); }
    catch (_) { return; }
    if (version !== paletteSearchVersion || !commandPalette?.isOpen()) return;
    paletteMatches = (Array.isArray(rows) ? rows : []).filter(row => !isQuickChat(row));
    commandPalette.refresh();
  }
  function paletteSearch(value) {
    const query = String(value || '').trim();
    clearTimeout(paletteSearchTimer);
    const version = ++paletteSearchVersion;
    // A hit the server already matched is shown whatever the query says, so
    // the list is emptied the moment the query stops matching it.
    if (query !== paletteMatchQuery) { paletteMatches = []; paletteMatchQuery = query; }
    if (query.length < 2) return version;
    paletteSearchTimer = setTimeout(() => safe(() => paletteRunSearch(query, version)), 180);
    return version;
  }
  const commandPalette = globalThis.KovaPalette?.create?.({items: paletteItems, onQuery: paletteSearch, onError: error => toastError(error.message)}) || null;
  async function openPalette() {
    if (!state.connected || !commandPalette) return;
    closeMenu();
    paletteMatches = [];
    paletteSearch('');
    commandPalette.open();
    await paletteLoad();
    commandPalette.refresh();
  }
  const typingIn = node => Boolean(node) && (/^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName || '') || node.isContentEditable);
  // #200: move through the sidebar's conversation list, wrapping at the ends.
  function stepConversation(direction) {
    const chats = (state.chats || []).filter(chat => !chat.ephemeral && inCurrentWorkspace(chat));
    if (!chats.length || state.busy) return;
    const index = chats.findIndex(chat => chat.id === state.chat?.id);
    const next = index < 0
      ? chats[direction > 0 ? 0 : chats.length - 1]
      : chats[(index + direction + chats.length) % chats.length];
    if (next && next.id !== state.chat?.id) safe(() => openChat(next.id));
  }
  // Each chord is a registry entry (shortcuts.js), so a remapped key is
  // dispatched from one place instead of a list of hard-coded compares (#200).
  const shortcutActions = {
    palette: () => safe(openPalette),
    'new-conversation': () => { if (state.connected) safe(newChat); },
    'new-incognito': () => safe(newIncognito),
    'search-conversations': searchConversations,
    'previous-conversation': () => stepConversation(-1),
    'next-conversation': () => stepConversation(1),
    'toggle-sidebar': () => toggleSidebar(),
    'toggle-zen': () => toggleZen(),
    // panes.js owns the side panel and the panes; false lets the key through.
    'toggle-panel': () => window.KovaPanel?.toggle?.() || false,
    'next-pane': () => window.KovaPanes?.focusNext?.() || false,
    'previous-pane': () => window.KovaPanes?.focusPrevious?.() || false,
    'toggle-theme': () => $('theme').onclick?.(),
    'focus-composer': () => $('prompt').focus(),
    'stop-reply': () => {
      if (state.activityOpen) activityPanel(false, true);
      else if (document.body.classList.contains('sidebar-open')) sidebar(false);
      else if (state.busy) safe(stop);
      else if (isQuick) window.kovaQuick?.hide?.();
      else if (inZen()) toggleZen(false);
      else return false;
    },
    shortcuts: () => $('shortcuts-dialog').showModal(),
  };
  // Native menu and tray quick actions (#203). The desktop shell sends an
  // allowlisted action id over its app-action channel; each maps to the same
  // handler the palette and keyboard shortcuts use, so behaviour stays in one
  // place. kova web and the plain browser have no bridge, so this is a no-op.
  const desktopActions = {
    palette: shortcutActions.palette,
    'new-conversation': shortcutActions['new-conversation'],
    'new-incognito': shortcutActions['new-incognito'],
    'search-conversations': shortcutActions['search-conversations'],
    'toggle-sidebar': shortcutActions['toggle-sidebar'],
    'toggle-theme': shortcutActions['toggle-theme'],
    'focus-composer': shortcutActions['focus-composer'],
    'shortcuts': shortcutActions['shortcuts'],
    'open-settings': () => { if (window.KovaSettings?.open) window.KovaSettings.open(); else $('open-settings')?.click(); },
  };
  window.kovaDesktop?.onAction?.(id => { const run = desktopActions[typeof id === 'string' ? id : '']; if (run) run(); });
  // The optional themed title bar keeps the OS window controls, so the shell
  // needs the active theme's own background, text colour and bar height. All
  // three come from the same tokens the page just painted with.
  function reportWindowChrome() {
    const report = window.kovaDesktop?.reportChrome;
    if (typeof report !== 'function') return;
    const styles = getComputedStyle(document.documentElement);
    report({
      background: styles.getPropertyValue('--bg-base').trim(),
      symbolColor: styles.getPropertyValue('--fg-default').trim(),
      height: Math.round($('titlebar').getBoundingClientRect().height || 0),
    });
  }
  reportWindowChrome();
  document.addEventListener('kova:theme', reportWindowChrome);
  document.addEventListener('keydown', event => {
    if (event.isComposing || document.querySelectorAll('dialog[open]').length) return;  // dialogs keep their own keys
    if (document.body.classList.contains('settings-open') || bots?.editing()) return;
    const action = globalThis.KovaShortcuts?.current?.actionFor?.(event, {guardTyping: typingIn(event.target)}) || null;
    const run = action && shortcutActions[action];
    if (run && run(event) !== false) { event.preventDefault(); return; }
    // Plain keys keep their own behaviour: they are typing, not chords.
    const mod = event.metaKey || event.ctrlKey, key = (event.key || '').toLowerCase();
    if (mod || event.altKey || typingIn(event.target)) return;
    if (key === '/') $('prompt').focus();
    else if (key === '?') $('shortcuts-dialog').showModal();
    else return;
    event.preventDefault();
  });
  let searchTimer;
  $('search').addEventListener('input', () => { clearTimeout(searchTimer); ++state.searchVersion; searchTimer = setTimeout(() => safe(refreshChats), 200); });
  async function chatSetting(value) {
    const chat = state.chat;
    if (!chat) return;
    const updated = await json(`/chats/${chat.id}`, write('PATCH', value));
    if (state.chat?.id === chat.id) Object.assign(state.chat, updated);
    safe(refreshChats);
  }
  $('mode').onchange = () => safe(async () => {
    await savePicker(async () => {
      try { await chatSetting({mode: $('mode').value}); }
      catch (error) { $('mode').value = state.chat?.mode || 'work'; throw error; }
    });
  });
  const openRules = () => safe(async () => {
    const dialog = el('dialog', 'rules-dialog'); dialog.setAttribute('aria-label', 'Permission rules');
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
  const modelByName = name => state.models.find(model => model.name === name);
  const providerList = value => Array.isArray(value) ? value : [];
  // The welcome-screen choice, kept across reloads. model-picker.js drops a
  // stored model that has since been removed or lost its reasoning level.
  const pickerModule = () => globalThis.KovaModelPicker || null;
  function restorePickerModel() {
    try {
      const module = pickerModule();
      return module ? module.draft(localStorage.getItem(module.DRAFT_KEY), state.models) : null;
    } catch (_) { return null; }
  }
  function rememberPickerModel(selection) {
    try {
      const module = pickerModule(); if (!module) return;
      if (selection) localStorage.setItem(module.DRAFT_KEY, module.serialize(selection));
      else localStorage.removeItem(module.DRAFT_KEY);
    } catch (_) { /* storage off: the choice still holds for this window */ }
  }
  function composerModel() {
    const assigned = bots?.botModel(state.chat);
    if (assigned) return {model: assigned.name, reasoning_effort: null};
    if (state.chat?.model) return {model: state.chat.model, reasoning_effort: state.chat.reasoning_effort ?? null};
    if (draftModel && modelByName(draftModel.model)?.model) return draftModel;
    return {model: defaultModel, reasoning_effort: modelByName(defaultModel)?.reasoning_effort ?? null};
  }
  // The model name a turn may actually send, or null when nothing usable is
  // configured. A chat's own saved model is always sent, so a removed one is
  // reported by the server; the new-chat default is only sent when it is a real
  // model, never the empty first-run placeholder.
  function sendableModel() {
    const {model} = composerModel();
    if (!model) return null;
    if (state.chat?.model === model) return model;
    return modelByName(model)?.model ? model : null;
  }
  function modelForNewChat() {
    const selection = composerModel();
    return modelByName(selection.model)?.model ? selection : {};
  }
  function createComposerChat() {
    const folder = $('cwd').value.trim();
    const body = {mode: $('mode').value, web: webOn(),
      permission: $('permission').value, ...(folder ? {cwd: folder} : {}), ...modelForNewChat()};
    // Quick ask chats are titled so they stay out of the normal sidebar history
    // (the same trick `kova ask` uses with its [ask] prefix).
    if (isQuick) {
      const firstLine = ($('prompt').value.trim().split('\n')[0] || '').slice(0, 54);
      body.title = QUICK_TITLE_PREFIX + (firstLine || 'quick ask');
    }
    return json('/chats', write('POST', body));
  }
  function syncComposerModel() {
    const selection = composerModel(), select = $('model'), effort = $('reasoning-effort');
    const supported = modelByName(selection.model)?.reasoning_efforts || [];
    select.value = selection.model;
    effort.replaceChildren(...['', ...supported].map(level => {
      const option = el('option', '', effortLabels[level]); option.value = level; return option;
    }));
    effort.value = supported.includes(selection.reasoning_effort) ? selection.reasoning_effort : '';
    $('reasoning-effort-row').hidden = !supported.length || !!bots?.botModel(state.chat);
    paintBotChat();
    controls();
    return refreshCapabilities(selection.model);
  }
  async function savePicker(change) {
    if (pickerSaving || state.busy) return;
    const focused = document.activeElement;
    pickerSaving = true; controls();
    try { await change(); }
    finally {
      pickerSaving = false; controls();
      if (focused && !focused.disabled && document.activeElement === document.body
          && !optionsPanel.hidden && optionsPanel.contains(focused)) focused.focus();
    }
  }
  async function chooseComposerModel(name, effort = modelByName(name)?.reasoning_effort ?? null) {
    if (bots?.botModel(state.chat)) throw new Error('Change this bot’s model in Edit bot.');
    if (!modelByName(name)?.model) throw new Error('Unknown model. Choose a configured model.');
    await savePicker(async () => {
      const next = {model: name, reasoning_effort: effort};
      try {
        if (state.chat) await chatSetting(next);
        else { draftModel = next; rememberPickerModel(next); }
      } finally { await syncComposerModel(); }
    });
  }
  $('model').onchange = () => {
    const name = $('model').value;
    safe(() => chooseComposerModel(name));
  };
  $('reasoning-effort').onchange = () => {
    const name = $('model').value, effort = $('reasoning-effort').value || null;
    safe(() => chooseComposerModel(name, effort));
  };
  $('model-make-default').onclick = () => safe(() => savePicker(async () => {
    const {model} = composerModel();
    const reasoning_effort = $('reasoning-effort').value || null;
    const entry = modelByName(model);
    if (!entry) return;
    await json('/models/active', write('POST', {name: model, reasoning_effort}));
    entry.reasoning_effort = reasoning_effort;
    defaultModel = model;
    // The welcome-screen override would otherwise shadow the new default.
    draftModel = null; rememberPickerModel(null);
    state.modelStatus = '';
    syncMenus();
    toastSuccess('Default saved for new chats.');
    await health();
  }));
  const favouritesKey = 'kova-model-favourites';
  const favourites = new Set();
  try {
    const saved = JSON.parse(localStorage.getItem(favouritesKey) || '[]');
    if (Array.isArray(saved)) for (const name of saved) if (typeof name === 'string') favourites.add(name);
  } catch (_) { /* this window can still remember favourites while it is open */ }
  $('model-favourite').onclick = () => {
    const {model} = composerModel();
    if (!modelByName(model)) return;
    if (favourites.has(model)) favourites.delete(model); else favourites.add(model);
    try { localStorage.setItem(favouritesKey, JSON.stringify([...favourites])); } catch (_) {}
    renderModels(state.models);
  };
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
      const previous = Boolean(model.vision);
      try {
        const updated = await json('/models', write('PATCH', {name, vision: vision.checked}));
        Object.assign(model, updated);
        if ($('model').value === name) await refreshCapabilities(name);
        dialog.close();
        toastSuccess(`Vision ${updated.vision ? 'on' : 'off'} for ${model.model || model.name}.`, 'Model', {
          // Undo is one more PATCH back to the value this dialog opened with.
          action: {label: 'Undo', onClick: async () => {
            const reverted = await json('/models', write('PATCH', {name, vision: previous}));
            Object.assign(model, reverted);
            if ($('model').value === name) await refreshCapabilities(name);
          }},
        });
      } catch (error) { save.disabled = false; throw error; }
    });
    actions.append(cancel, save); form.append(head, intro, fields, toggle, note, actions);
    dialog.append(form); document.body.append(dialog); dialog.onclose = () => dialog.remove(); dialog.showModal();
  }
  // The Settings view links here without owning the dialog; keep one stable
  // trigger so the view never reaches into this closure.
  $('open-model-settings').onclick = () => safe(openModelSettings);
  // theme.js (#174) owns the theme, its shared preference and the window colour;
  // #theme stays the light/dark toggle the shortcut and Settings click.
  function themeButton() {
    const light = (globalThis.KovaTheme?.appearance?.() || document.documentElement.dataset.theme) === 'light';
    const next = light ? 'Dark theme' : 'Light theme';
    $('theme').lastElementChild.textContent = next; $('theme').title = next;
    $('theme-icon').className = `icon i-${light ? 'moon' : 'sun'}`;
  }
  $('theme').onclick = () => {
    if (globalThis.KovaTheme) globalThis.KovaTheme.toggle();
    else document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    themeButton();
  };
  document.addEventListener('kova:theme', themeButton); themeButton();
  $('disconnect').onclick = () => safe(async () => { await discardIncognito(); await json('/app/logout', {method: 'POST'}); setChat(null); $('chat-list').replaceChildren(); disconnected('Disconnected. Open kova web to reconnect.'); });
  async function setupIdentity() {
    state.persona = await json('/persona');
    if (!state.persona.configured) {
      const dialog = el('dialog'); dialog.setAttribute('aria-label', 'Meet your assistant');
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
    pageTitle(); renderWelcome();
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
    source.addEventListener('bot_changed', event => {
      if (state.events !== source || !bots) return;
      alive();
      try { void bots.refresh(JSON.parse(event.data).id); } catch (_) {}
    });
    source.addEventListener('conversation_state', event => {
      if (state.events !== source) return;
      alive();
      try {
        const {chat_id: id, state: next} = JSON.parse(event.data);
        const chat = state.chats.find(item => item.id === id);
        const current = state.chat?.id === id ? state.chat : null;
        const previous = current?.state;
        if (chat) { chat.state = next; history(state.chats); }
        else safe(refreshChats);
        if (current) { current.state = next; chatStateChanged(id, next, previous); }
        noteChatState(id, next, (chat || current)?.title, (chat || current)?.ephemeral, (chat || current)?.kind);
        renderActivity();
      } catch (_) {}
    });
    source.addEventListener('reminder', event => { alive(); try { const reminder = JSON.parse(event.data); toast(reminder.text, 'Reminder', {sticky: true, icon: 'bell'}); window.KovaSounds?.play('reminder'); } catch (_) {} });
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
    if (response?.status === 401) return disconnected('Kova restarted, so this window was signed out. Open kova web on this computer to reconnect.');
    if (!response?.ok) return streamLost(true);
    connectEvents();
  }
  // Anything missed while the stream was down: chat states, the open chat, model status.
  async function resync() {
    await Promise.all([refreshChats(), bots?.refresh()]);
    if (state.chat?.id && !state.busy) await openChat(state.chat.id, true);
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
    if (!state.chats.length) loadingChats();
    // Strip before any asynchronous work, so a consumed ticket never stays in history.
    const fragment = new URLSearchParams(location.hash.slice(1));
    const ticket = fragment.get('ticket'), pairing = fragment.get('pair'), share = fragment.get('share');
    historyReplace();
    try {
      if (ticket) await json('/app/login', write('POST', {ticket}));
      // A phone that scanned a pairing QR code trades its one-use secret for a device cookie.
      if (pairing) { await json('/app/pair', write('POST', {secret: pairing})); toastSuccess('This phone is now paired with Kova.'); }
      await json('/chats'); state.connected = true; clearNotice();
      // Signed in now: the shared theme choice and installed themes are readable.
      globalThis.KovaTheme?.sync?.(); globalThis.KovaTheme?.refresh?.();
      globalThis.KovaTypography?.sync?.();
      globalThis.KovaCustomCss?.sync?.();
      // These read the same document; their first-load read waited for this
      // session instead of asking while the window was still signed out.
      globalThis.KovaMotion?.sync?.(); globalThis.KovaReasoning?.sync?.();
      globalThis.KovaThinking?.sync?.(); globalThis.KovaStarters?.sync?.();
      globalThis.KovaShortcuts?.current?.sync?.();
      if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
        // A window that stayed open across `kova update` reloads once when the new
        // worker takes over, unless a reply is streaming (the next open is fresh anyway).
        const updating = !!navigator.serviceWorker.controller;
        let reloaded = false;
        navigator.serviceWorker.addEventListener?.('controllerchange', () => {
          if (!updating || reloaded || state.busy) return;
          reloaded = true;
          location.reload();
        });
        navigator.serviceWorker.register('/app/sw.js', {scope: '/app'}).catch(() => {});
      }
      await setupIdentity();
      renderStarters();
      const [models, providers, status] = await Promise.all([json('/models'), json('/providers'), json('/health')]);
      state.models = models; state.providers = providerList(providers); defaultModel = status.model || '';
      draftModel = restorePickerModel();
      renderModels(models);
      await syncComposerModel();
      await Promise.all([refreshChats(), bots?.refresh()]); controls(); await health();
      live.lost = false; live.retry = 0; live.pinged = false; connectEvents();
      if (share) {
        const local = await readWorkerShare(share);
        const draft = local || await json(`/app/share/${encodeURIComponent(share)}`, {method: 'POST'});
        await applyShare(draft);
      }
      if (inBots() && !state.chat) {
        $('bot-directory-title').focus();
        if (!wide?.matches) sidebar(true, $('search'));
      } else if (!inBots()) $('prompt').focus();
    } catch (error) { disconnected(error instanceof TypeError ? 'Kova is offline. Reconnect to your computer to load chats and reminders.' : error.message); }
    finally { state.starting = false; }
  }
  function renderModels(models) {
    // model-picker.js decides the groups: favourites first, then one group per
    // provider (Local before Cloud), and every connected provider appears even
    // when no model of it is configured yet.
    const module = pickerModule();
    const plan = module
      ? module.groups(models, {favourites: [...favourites], providers: state.providers})
      : fallbackPickerGroups(models);
    const parents = [];
    if (plan.favourites.length) {
      const pinned = el('optgroup'); pinned.label = 'Favourites';
      for (const model of plan.favourites) pinned.append(modelOption(model, true));
      parents.push(pinned);
    }
    for (const group of plan.groups) {
      const node = el('optgroup');
      node.label = group.label;
      node.dataset.location = group.location;
      node.dataset.provider = group.provider;
      if (group.empty) {
        // A connected provider that has no model yet: say where to fix it
        // instead of leaving it out of the list as if it did not exist.
        node.label = `${group.label} · no models chosen`;
        const option = el('option', '', 'No model chosen for this provider yet');
        option.disabled = true; option.value = '';
        node.append(option);
      } else {
        for (const model of group.models) node.append(modelOption(model, false));
      }
      parents.push(node);
    }
    $('model').replaceChildren(...parents);
    $('model').value = sendableModel() || '';
    syncMenus();
  }
  function modelOption(model, favourite) {
    const option = el('option', '', model.model);
    option.value = model.name; option.title = model.name; option.dataset.provider = model.provider_name || 'Custom';
    // The group heading already says Local or Cloud; only a favourite, which
    // leaves its provider group, carries the label on the row itself.
    if (favourite) option.dataset.location = model.local ? 'Local' : 'Cloud';
    return option;
  }
  // A tiny fallback so the picker still groups by provider if model-picker.js
  // ever fails to load; the real ordering lives in that module.
  function fallbackPickerGroups(models) {
    const pinned = [];
    const groups = new Map();
    for (const model of models) {
      if (!model.model) continue;
      if (favourites.has(model.name)) { pinned.push(model); continue; }
      const key = `${model.local ? 'Local' : 'Cloud'}:${model.provider || model.provider_name}`;
      if (!groups.has(key)) groups.set(key, {label: model.provider_name || 'Custom', location: model.local ? 'Local' : 'Cloud', models: [], empty: false});
      groups.get(key).models.push(model);
    }
    return {favourites: pinned, groups: [...groups.values()]};
  }
  function modelHint(option) {
    const entry = modelByName(option.value);
    if (!entry) return '';
    const speed = entry.last_local_speed;
    return [
      option.dataset.location ? option.dataset.provider : '',
      entry.context ? `${entry.context.toLocaleString()} token context` : '',
      entry.vision ? 'Vision' : 'No vision',
      entry.tools ? 'Tools' : 'No tools',
      entry.local && speed?.tokens_per_second
        ? `${speed.estimated ? '~' : ''}${speed.tokens_per_second} tok/s last reply` : '',
    ].filter(Boolean).join(' · ');
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
    await refreshProviders();
    await health();
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
      await refreshLocalSelection(); closeProvider(); toastSuccess('Model ready on this PC. Start chatting.');
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
  // A second OpenAI account is a normal thing to want. Suggest a free name
  // instead of prefilling one that the server will refuse on save.
  function uniqueConnectionName(base) {
    const taken = new Set(providerList(state.providers).map(provider => provider.id));
    if (!taken.has(base)) return base;
    let index = 2;
    while (taken.has(`${base}-${index}`)) index += 1;
    return `${base}-${index}`;
  }
  function providerKindChanged() {
    clearProviderSecrets();
    const spec = providerSpec();
    if (!spec.id) return;
    const caps = providerCaps();
    $('provider-name').value = uniqueConnectionName(spec.id);
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
    const [data, providers] = await Promise.all([json('/providers/catalog'), json('/providers')]);
    providerSpecs = data.providers; state.providers = providerList(providers);
    const connectedKinds = new Set(providers.map(provider => provider.kind));
    $('provider-kind').replaceChildren(...providerSpecs.map(spec => {
      const notes = [connectedKinds.has(spec.id) ? 'connected' : '',
                     spec.id === 'codex' && !data.harness_available ? 'not found' : ''].filter(Boolean);
      const option = el('option', '', spec.name + (notes.length ? ` (${notes.join(', ')})` : ''));
      option.value = spec.id; return option;
    }));
    // Start from a kind that is not connected yet, so "Add provider" never
    // opens on the connection the user already has.
    const next = providerSpecs.find(spec => !connectedKinds.has(spec.id)) || providerSpecs[0];
    $('provider-kind').value = next?.id || 'openai'; $('provider-details').hidden = false;
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
        await refreshProviders();
        toastSuccess('Provider connected. Ready when you are.'); return;
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

  // Settings > Models and providers: every connection, what it holds, and the
  // two ways to change it. This is the view that answers "which provider am I
  // on, and how do I choose another one?" without the terminal.
  function openProvidersSettings() {
    if (globalThis.KovaSettings?.open) globalThis.KovaSettings.open('models');
    else $('open-settings').click();
  }
  async function refreshProviders() {
    const [providers, models, status] = await Promise.all([json('/providers'), json('/models'), json('/health')]);
    state.providers = providerList(providers); state.models = models; defaultModel = status.model || '';
    renderModels(models);
    renderProviders();
    await syncComposerModel();
  }
  function renderProviders() {
    const host = $('providers-panel'); if (!host) return;
    const configured = state.models.filter(model => model.model);
    host.replaceChildren();
    if (!providerList(state.providers).length) {
      const empty = el('div', 'providers-empty');
      empty.append(el('p', 'providers-row-help', 'No provider is connected yet. Add one and its models appear in the message box.'));
      const add = el('button', 'btn small', 'Add provider'); add.type = 'button';
      add.onclick = () => safe(async () => { closeProvider(); $('add-provider').onclick(); });
      empty.append(add); host.append(empty); return;
    }
    const list = el('ul', 'providers-list');
    for (const provider of providerList(state.providers)) {
      const owned = configured.filter(model => model.provider === provider.id);
      const item = el('li', 'providers-row'), copy = el('div', 'providers-row-copy');
      const title = el('span', 'providers-row-title', provider.name || provider.id);
      const count = owned.length === 1 ? '1 model' : `${owned.length} models`;
      const where = provider.base_url || 'no address';
      copy.append(title, el('p', 'providers-row-help',
        `${provider.kind} · ${where} · ${owned.length ? count : 'no models chosen'}`));
      if (!provider.authenticated) copy.append(el('p', 'providers-row-help', 'No credential is stored. Sign in again from the terminal with kova providers login, or remove it and add it again.'));
      const actions = el('div', 'providers-row-actions');
      const choose = el('button', 'btn small quiet', owned.length ? 'Change models' : 'Choose models');
      choose.type = 'button'; choose.setAttribute('aria-label', `Choose models from ${provider.name || provider.id}`);
      choose.onclick = () => safe(async () => { choose.disabled = true; try { await openProviderModels(provider); } finally { choose.disabled = false; } });
      const remove = el('button', 'btn small quiet', 'Remove');
      remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${provider.name || provider.id}`);
      remove.onclick = () => safe(async () => {
        const what = owned.length ? ` and its ${count}` : '';
        if (!confirm(`Remove ${provider.name || provider.id}${what}? Its key is forgotten; chats that used them fall back to your default model.`)) return;
        remove.disabled = true;
        try {
          await json(`/providers/${encodeURIComponent(provider.id)}`, {method: 'DELETE'});
          await refreshProviders();
          toastSuccess(`${provider.name || provider.id} removed.`, 'Providers');
        } finally { remove.disabled = false; }
      });
      actions.append(choose, remove); item.append(copy, actions); list.append(item);
    }
    const add = el('button', 'btn small', 'Add provider'); add.type = 'button';
    add.onclick = () => safe(async () => { closeProvider(); $('add-provider').onclick(); });
    host.append(list, add);
  }
  // Adding models to a connection that already exists: its key is reused, so
  // this never asks for credentials again. Fetched on open, then filtered.
  async function openProviderModels(provider) {
    const name = provider.name || provider.id;
    const dialog = el('dialog', 'provider-models-dialog');
    dialog.setAttribute('aria-label', `Models from ${name}`);
    const form = el('form'); form.method = 'dialog';
    const head = el('div', 'dialog-head'), title = el('h2', '', `Models from ${name}`);
    const close = iconButton('x', `Close ${name} models`); close.type = 'button'; close.onclick = () => dialog.close();
    head.append(title, close);
    const status = el('p', 'provider-footnote', 'Listing models…'); status.setAttribute('role', 'status');
    const body = el('div');
    form.append(head, status, body, el('p', 'provider-footnote', 'Messages go to the provider you choose. A model that is already added keeps its settings.'));
    const actions = el('div', 'dialog-actions');
    const cancel = el('button', 'btn quiet', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => dialog.close();
    actions.append(cancel);
    form.append(actions); dialog.append(form); document.body.append(dialog);
    dialog.onclose = () => dialog.remove(); dialog.showModal();
    let models = [];
    try {
      const data = await json(`/providers/${encodeURIComponent(provider.id)}/models`);
      models = data.models || [];
    } catch (error) {
      status.textContent = error.message;
      return;
    }
    if (!models.length) { status.textContent = `${name} listed no models. Check the connection and try again.`; return; }
    const owned = new Set(state.models.filter(model => model.model && model.provider === provider.id).map(model => model.model));
    status.textContent = `${models.length} available. Choose the ones to keep in the message box.`;
    const filter = el('input'); filter.type = 'search'; filter.placeholder = 'Search models'; filter.setAttribute('aria-label', 'Search models');
    const list = el('div', 'provider-models', ''); list.setAttribute('role', 'group'); list.setAttribute('aria-label', `Models from ${name}`);
    const choices = models.map((model, index) => {
      const label = el('label', 'provider-check'), input = el('input');
      input.type = 'checkbox'; input.checked = owned.has(model) || (!owned.size && index === 0);
      label.append(input, el('span', '', model));
      return {model, input, label};
    });
    for (const choice of choices) list.append(choice.label);
    const activeRow = el('label', '', 'Default for new chats');
    const activeSelect = el('select'); activeSelect.setAttribute('aria-label', 'Default for new chats');
    const refreshActive = () => {
      const selected = choices.filter(choice => choice.input.checked).map(choice => choice.model);
      const previous = activeSelect.value;
      const keep = el('option', '', 'Keep the current default'); keep.value = '';
      activeSelect.replaceChildren(keep, ...selected.map(model => { const option = el('option', '', model); option.value = model; return option; }));
      // Adding a model must not quietly change which model a device defaults
      // to; the default only moves when it is chosen here.
      activeSelect.value = selected.includes(previous) ? previous : '';
    };
    for (const choice of choices) choice.input.onchange = refreshActive;
    filter.oninput = () => {
      const query = filter.value.trim().toLowerCase();
      for (const choice of choices) choice.label.hidden = !choice.model.toLowerCase().includes(query);
    };
    const save = el('button', 'btn primary', 'Save models'); save.type = 'button';
    save.onclick = () => safe(async () => {
      const selected = choices.filter(choice => choice.input.checked).map(choice => choice.model);
      if (!selected.length) return toast('Choose at least one model.');
      save.disabled = true; save.textContent = 'Saving…';
      try {
        await json(`/providers/${encodeURIComponent(provider.id)}/models`,
          write('POST', {models: selected, ...(activeSelect.value ? {active: activeSelect.value} : {})}));
        dialog.close();
        await refreshProviders();
        toastSuccess(`Models from ${name} are ready.`, 'Providers');
      } finally { save.disabled = false; save.textContent = 'Save models'; }
    });
    body.append(filter, list, activeRow);
    activeRow.append(activeSelect);
    actions.append(save);
    refreshActive();
    filter.focus();
  }
  document.addEventListener('kova:settings-open', () => safe(renderProviders));
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
          try { const result = await json(`/mcp/${enc(row.name)}/test`, {method: 'POST'}); if (result.error) toastError(result.error, 'MCP'); else toastSuccess(`${row.name} is working.`, 'MCP'); await refresh(); }
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
          if (row.error) toastError(row.error, 'MCP');
          name.value = ''; target.value = ''; args.value = ''; expanded.add(row.name); await refresh();
        } finally { save.disabled = false; save.textContent = 'Add server'; }
      });
    };
  }
  // Extensions (#137): the catalog of plugins, skills and bots (#419), and everything that is
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
    for (const [key, label] of [['installed', 'Installed'], ['plugin', 'Plugins'], ['skill', 'Skills'], ['bot', 'Bots'], ['theme', 'Themes']]) {
      const button = el('button', '', label); button.type = 'button'; button.setAttribute('role', 'tab');
      button.onclick = () => {
        if (tab === key) return;
        window.KovaThemeGallery.dismissEmbedded();
        tab = key;
        render();
      };
      buttons[key] = button; tabs.append(button);
    }
    tabs.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const items = Object.values(buttons);
      const step = event.key === 'ArrowRight' ? 1 : -1;
      const next = items[(items.indexOf(buttons[tab]) + step + items.length) % items.length];
      next.click();
      next.focus();
    });
    dialog.append(head, intro, tabs, search, list, footer); document.body.append(dialog);
    buttons.theme.id = 'extensions-themes-tab';
    buttons.theme.setAttribute('aria-controls', 'extensions-themes-panel');
    const themePanel = el('div', 'panel-list ext-list');
    themePanel.id = 'extensions-themes-panel';
    themePanel.setAttribute('role', 'tabpanel');
    themePanel.setAttribute('aria-labelledby', buttons.theme.id);
    dialog.append(themePanel);
    dialog.addEventListener('close', () => window.KovaThemeGallery.dismissEmbedded());
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
    // What a catalog bot would be allowed to do, and its full persona.
    function botAdds(parent, bot, notes) {
      parent.append(el('p', 'ext-label', `${bot.name} · @${bot.handle}`));
      section(parent, 'Tools it may use', bot.tools);
      section(parent, 'Tools it may never use', bot.deny);
      section(parent, 'Settings', [
        bot.ceiling === 'manual' ? 'Asks before each action' : `Approval: ${bot.ceiling}`,
        `Mode: ${bot.mode}, web search ${bot.web ? 'on' : 'off'}`,
        `Model: ${bot.model || 'the active model'}`,
        `Own notes ${bot.memory.own ? 'on' : 'off'}; your memory: ${bot.memory.user_memory}`,
      ]);
      section(parent, 'Changed for this computer', notes || []);
      parent.append(el('p', 'ext-label', 'Persona (its instructions)'), el('pre', 'ext-body', bot.persona));
    }
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
        if (preview.bot) botAdds(box, preview.bot, preview.notes);
        const yes = el('button', 'btn primary', upgrade ? 'Update' : 'Install'); yes.type = 'button';
        const no = el('button', 'btn quiet', 'Cancel'); no.type = 'button';
        const row = el('div', 'dialog-actions'); row.append(no, yes); box.append(row);
        no.onclick = () => { api(`/extensions/catalog/preview/${enc(preview.ticket)}`, {method: 'DELETE'}).catch(() => {}); render(); };
        yes.onclick = () => safe(async () => {
          yes.disabled = true; no.disabled = true; yes.textContent = 'Installing…';
          try {
            const done = await json('/extensions/install', write('POST', {ticket: preview.ticket, upgrade}));
            toastSuccess(preview.bot ? `Installed ${done.installed}. Chat with @${preview.bot.handle} in Kova Bots.` : `Installed ${done.installed}.`, 'Extensions');
            if (preview.bot) bots?.refresh();
            await refresh();
          }
          catch (error) { await refresh(); throw error; }
        });
        text.append(box); button.remove(); yes.focus();
      });
      actions.append(button); item.append(text, actions); list.append(item);
    }
    function render() {
      for (const [key, button] of Object.entries(buttons)) { button.setAttribute('aria-selected', String(key === tab)); button.tabIndex = key === tab ? 0 : -1; }
      const showingThemes = tab === 'theme';
      for (const item of [intro, search, list, footer]) item.hidden = showingThemes;
      themePanel.hidden = !showingThemes;
      if (showingThemes) {
        if (dialog.open && !themePanel.firstChild) {
          window.KovaThemeGallery.open(launch, {mount: themePanel, closeHost: () => dialog.close()});
        }
        return;
      }
      list.replaceChildren();
      const filtered = search.value.trim() !== '';
      if (tab === 'installed') {
        const shownPlugins = data.plugins.filter(plugin => matches(plugin.name, plugin.description));
        const shownSkills = data.skills.filter(skill => matches(skill.name, skill.description, skill.origin, skill.plugin));
        if (shownPlugins.length) { heading('Plugins'); shownPlugins.forEach(renderPlugin); }
        if (shownSkills.length) { heading('Skills'); shownSkills.forEach(renderSkill); }
        if (!shownPlugins.length && !shownSkills.length) list.append(el('p', 'panel-empty', filtered ? 'Nothing matches that search.' : 'Nothing installed yet. Browse Plugins, Skills or Bots, or let Kova learn a skill while you work.'));
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
    launch.onclick = () => safe(async () => { sidebar(false); dialog.showModal(); render(); await refresh(); });
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
        try { await navigator.clipboard.writeText(pair.url); toastSuccess('Link copied. It works once, for two minutes.'); }
        catch (_) { toastError('Clipboard unavailable here.'); }
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
      const choices = optionsOf(owner.select);
      const chosen = choices.find(option => option.value === owner.select.value);
      owner.text.textContent = chosen?.textContent || owner.placeholder;
      owner.button.disabled = owner.select.disabled || (!choices.length && !owner.search);
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
    const selection = composerModel(), model = modelByName(selection.model);
    const effort = $('reasoning-effort').value;
    // A placeholder entry (an empty first-run "local") or a model whose
    // provider was removed is not a model: say so instead of naming it.
    const label = model?.model ? model.model : 'No model';
    $('chat-options-label').textContent = label;
    $('chat-options-effort').hidden = !effort;
    $('chat-options-effort').textContent = effort ? `· ${effortLabels[effort]}` : '';
    const healthDetail = selection.model && !model ? 'Model unavailable' : selection.model === defaultModel ? state.modelStatus : '';
    const detail = [label, effort && `Reasoning: ${effortLabels[effort]}`, model?.model ? (model.local ? 'Local' : 'Cloud') : 'Add a provider to choose one',
      $('mode').value === 'chat' ? 'Chat mode' : 'Work mode', healthDetail, state.localOnly ? 'Local only' : ''].filter(Boolean).join(', ');
    $('chat-options').title = `Chat options: ${detail}`;
    $('chat-options').setAttribute('aria-label', `Chat options. ${detail}`);
    const starred = favourites.has(selection.model);
    $('model-favourite').setAttribute('aria-pressed', String(starred));
    $('model-favourite').title = starred ? 'Remove from favourites' : 'Pin to favourites';
    const noModel = !model?.model || $('model').disabled;
    $('model-favourite').disabled = noModel;
    $('model-make-default').disabled = noModel;
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
    const node = el('div', owner.search ? 'popover model-picker-popover' : 'popover'), list = el('div', 'popover-list');
    list.id = `${select.id}-listbox`; list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', owner.label); list.tabIndex = -1;
    let active = -1, typed = '', typedAt = 0;
    const choose = value => {
      closeMenu(true);
      if (value === select.value) return;
      select.value = value; syncMenus(); select.onchange?.();
    };
    const add = (option, parent) => {
      const index = items.length, item = el('div', `option${option.disabled ? ' option-note-row' : ''}`), text = el('span', 'option-text');
      item.id = `${select.id}-option-${index}`; item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(!option.disabled && option.value === select.value));
      if (option.disabled) item.setAttribute('aria-disabled', 'true');
      const title = el('span', 'option-title', option.textContent);
      text.append(title);
      if (option.dataset.location) text.append(el('span', 'model-location', option.dataset.location));
      const note = option.disabled ? '' : owner.describe?.(option);
      if (note) text.append(el('span', 'option-note', note));
      item.append(text);
      if (!option.disabled) item.append(icon('check'));
      if (!option.disabled) {
        item.onclick = () => choose(option.value);
        item.onpointermove = () => { if (active !== index) activate(index, false); };
      }
      const label = [option.textContent, ...(owner.search ? [option.value, option.dataset.provider, option.dataset.location || parent.dataset.location] : [])];
      parent.append(item);
      items.push({item, value: option.disabled ? null : option.value, disabled: Boolean(option.disabled),
                  label: label.join(' ').toLowerCase()});
    };
    for (const child of select.children) {
      if (child.tagName.toUpperCase() !== 'OPTGROUP') { add(child, list); continue; }
      const group = el('div', 'option-group'), heading = el('div', 'option-group-label', child.label);
      const location = child.dataset.location;
      group.setAttribute('role', 'group'); group.setAttribute('aria-label', location ? `${child.label}, ${location}` : child.label);
      if (location) { group.dataset.location = location; heading.append(el('span', 'model-location', location)); }
      group.append(heading);
      for (const option of child.children) add(option, group);
      list.append(group);
    }
    const none = el('div', 'option-empty', owner.empty || 'No matches');
    // With nothing to pick, the owner's empty state sits above the related
    // actions, so the list never looks broken (here: add a provider).
    const selectable = items.filter(entry => entry.value !== null);
    none.hidden = selectable.length > 0;
    list.append(none);
    // Related actions close the list: reachable with the same arrows and Enter.
    for (const action of owner.actions || []) {
      if (action.when && !action.when()) continue;
      const index = items.length, item = el('div', 'option option-action');
      item.id = `${select.id}-option-${index}`; item.setAttribute('role', 'option'); item.setAttribute('aria-selected', 'false');
      item.append(icon(action.glyph), el('span', 'option-title', action.label));
      item.onclick = () => { closeMenu(true); action.run(); };
      item.onpointermove = () => { if (active !== index) activate(index, false); };
      list.append(item); items.push({item, value: null, label: action.label.toLowerCase()});
    }
    const search = (owner.search ? selectable.length > 0 : items.length > 7) ? el('input', 'popover-search') : null;
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
    const visible = () => items.map((entry, index) => entry.item.hidden || entry.disabled ? -1 : index).filter(index => index >= 0);
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
      else if (event.key === 'Enter' || (event.key === ' ' && !search)) { if (items[active] && !items[active].item.hidden) items[active].item.onclick?.(); }
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
    const selected = items.findIndex(entry => entry.value !== null && entry.value === select.value);
    activate(selected >= 0 ? selected : (visible()[0] ?? 0));
    target.focus({preventScroll: true});
  }
  function menu(id, label, {describe, glyph, align = 'start', width = 190, placeholder = 'Choose…', labelledby, actions, search, empty} = {}) {
    const select = $(id), button = el('button', 'menu-button'), text = el('span', 'menu-value', placeholder);
    button.type = 'button'; button.id = `${id}-button`;
    button.setAttribute('aria-haspopup', 'listbox'); button.setAttribute('aria-expanded', 'false');
    if (labelledby) button.setAttribute('aria-labelledby', `${labelledby} ${id}-button`); else button.setAttribute('aria-label', label);
    if (glyph) button.append(icon(glyph));
    button.append(text, icon('chevron-down'));
    const owner = {select, button, text, label, describe, align, width, placeholder, actions, search, empty};
    button.onclick = () => { if (popup?.owner === owner) closeMenu(); else openMenu(owner); };
    button.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); if (popup?.owner !== owner) openMenu(owner); }
    });
    $(`${id}-slot`).append(button); menus.push(owner); return owner;
  }
  const permissionNotes = {manual: 'Ask before commands and file changes', auto: 'Ask only before destructive commands', full: 'Never ask. Use with care.'};
  menu('permission', 'Permission', {glyph: 'shield-check', width: 270, describe: option => permissionNotes[option.value],
    actions: [{glyph: 'sliders-horizontal', label: 'Edit permission rules…', run: openRules}]});
  const modelMenu = menu('model', 'Model', {align: 'end', width: 280, placeholder: 'Choose model', describe: modelHint, search: true,
    empty: 'No model is set up yet. Add a provider, then choose one of its models here.',
    actions: [
      {glyph: 'sliders-horizontal', label: 'Model settings…', when: () => !!$('model').value, run: () => safe(openModelSettings)},
      {glyph: 'columns-2', label: 'Compare answers…', when: () => !state.busy && !inBots(), run: () => { options(false); compare?.choose(); }},
      {glyph: 'plus', label: 'Add provider…', run: () => { options(false); $('add-provider').onclick(); }},
      {glyph: 'settings', label: 'Manage providers…', run: () => { options(false); openProvidersSettings(); }},
    ]});
  menu('reasoning-effort', 'Reasoning effort', {align: 'end'});
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
  optionsButton.addEventListener('keydown', event => {
    if (event.key !== 'ArrowDown' || modelMenu.button.disabled) return;
    event.preventDefault(); options(true); openMenu(modelMenu);
  });
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
    if (state.activityOpen && !event.target.closest?.('.activity-wrap')) activityPanel(false);
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

  // Quick ask overlay (#71). The shell hands over a clipboard selection and
  // asks this window to open a chat in the main window; this client hides the
  // full chrome and behaves as a one-line ask box. No Node reaches the page:
  // every string is re-checked in the main process before anything is trusted.
  function renderQuickSelection() {
    if (!isQuick) return;
    $('quick-selection').hidden = !quickSelection;
  }
  function renderQuickControls() {
    if (!isQuick) return;
    $('quick-open').hidden = !state.chat;
    $('quick-copy').hidden = ![...document.querySelectorAll('#messages .assistant')].length;
  }
  let quickCopyTimer = null;
  // The reply's own body: the label, timestamp and action bar around it are
  // chrome, not answer text.
  function quickAnswerText() {
    const answers = [...document.querySelectorAll('#messages .assistant')];
    return answers.at(-1)?.querySelector('.message-body')?.textContent?.trim() || '';
  }
  function quickCopyAnswer() {
    const text = quickAnswerText();
    if (!text) return null;
    const button = $('quick-copy');
    clearTimeout(quickCopyTimer);
    button.textContent = 'Copy';
    return copyText(text, () => {
      if (!isQuick) return;
      button.textContent = 'Copied';
      quickCopyTimer = setTimeout(() => { if (isQuick) button.textContent = 'Copy'; }, 1600);
    });
  }
  window.kovaQuick?.onSelection?.(text => {
    quickSelection = typeof text === 'string' && text.trim() ? text : '';
    renderQuickSelection();
  });
  window.kovaQuick?.onOpenChat?.(chatId => { if (chatId) safe(() => openChat(chatId)); });
  $('quick-open').onclick = () => window.kovaQuick?.openChat?.(state.chat?.id || '');
  $('quick-copy').onclick = quickCopyAnswer;
  $('quick-selection-remove').onclick = () => { quickSelection = ''; renderQuickSelection(); };
  if (isQuick) {
    document.body.classList.add('quick');
    $('quick-open').hidden = true; $('quick-copy').hidden = true; $('quick-selection').hidden = true;
  }

  controls(); start();
})();

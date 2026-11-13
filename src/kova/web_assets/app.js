/* Kova is a client of the local API. No agent logic or permanent token lives here. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {chat: null, chats: [], busy: false, connected: false, events: null, models: [], turnReady: false, stopRequested: false, searchVersion: 0, navigationVersion: 0, starting: false};
  const scroll = $('scroll-area');
  let nearBottom = true;
  scroll.addEventListener('wheel', event => { if (event.deltaY < 0) nearBottom = false; }, {passive: true});
  scroll.addEventListener('touchmove', () => { nearBottom = false; }, {passive: true});
  scroll.addEventListener('scroll', () => { nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < (nearBottom ? 100 : 2); });
  function follow() { if (nearBottom) scroll.scrollTop = scroll.scrollHeight; }
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function toast(message, title = 'Kova') {
    const node = el('div', 'toast');
    const body = el('div'); body.append(el('strong', '', title), document.createTextNode(message));
    const close = el('button', '', '×'); close.setAttribute('aria-label', 'Dismiss notification');
    close.onclick = () => node.remove(); node.append(body, close); $('toasts').append(node);
    // Reminders stay until explicitly dismissed.
    if (title !== 'Reminder') setTimeout(() => node.remove(), 9000);
  }
  function banner(message = '') { $('banner').textContent = message; $('banner').hidden = !message; }
  function controls() {
    const busy = state.busy;
    for (const id of ['new-chat', 'mode', 'web', 'model', 'permission', 'cwd', 'add-provider']) $(id).disabled = busy || !state.connected;
    for (const id of ['permission', 'cwd']) $(id).disabled ||= $('mode').value === 'chat';
    $('send').hidden = busy; $('stop').hidden = !busy;
    $('send').disabled = !state.connected || !$('prompt').value.trim();
    $('stop').disabled = state.stopRequested;
    document.querySelectorAll('.history-row button').forEach(button => { button.disabled = busy; });
    $('composer').setAttribute('aria-busy', String(busy));
  }
  function disconnected(message) {
    state.connected = false; retry.hidden = false;
    state.events?.close();
    $('connection').className = 'connection';
    $('connection').lastElementChild.textContent = 'Disconnected';
    banner(message || 'Open kova-web on this computer to connect. No token or account needed.');
    controls();
  }
  async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, {credentials: 'same-origin', ...options,
      headers: {'Content-Type': 'application/json', ...options.headers}}); }
    catch (_) { throw new Error('Cannot reach Kova. Check the server, then retry.'); }
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { const body = await response.json(); if (typeof body.detail === 'string') message = body.detail; else if (Array.isArray(body.detail)) message = body.detail.map(item => `${item.loc?.slice(1).join('.') || 'Field'}: ${item.msg}`).join('; '); } catch (_) {}
      if (response.status === 401) disconnected('Your connection expired. Open kova-web again to reconnect.');
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
    node.querySelectorAll('a').forEach(link => {
      const href = link.getAttribute('href') || '';
      // Restrict navigation to explicit web links, never local API URLs or JS.
      if (!/^https?:\/\//i.test(href)) link.removeAttribute('href');
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    });
    if (final) node.querySelectorAll('pre code').forEach(code => {
      try { if (code.textContent.length < 20000) hljs.highlightElement(code); } catch (_) {}
      const copy = el('button', 'copy-code', 'Copy');
      copy.type = 'button'; copy.setAttribute('aria-label', 'Copy code');
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(code.textContent); copy.textContent = 'Copied'; }
        catch (_) { toast('Clipboard unavailable. Select the code to copy it.'); }
      };
      code.parentNode.append(copy);
    });
  }
  function message(role, content) {
    const article = el('article', `message ${role}`);
    const label = el('div', 'message-label');
    label.append(el('span', 'avatar', role === 'user' ? '↑' : '✳'), document.createTextNode(role === 'user' ? 'You' : 'Kova'));
    const body = el('div', 'message-body');
    if (role === 'user') body.textContent = content || '';
    else if (content) { const md = el('div', 'markdown'); markdown(md, content, true); body.append(md); }
    article.append(label, body); $('messages').append(article);
    return body;
  }
  function history(chats) {
    $('chat-list').replaceChildren(); $('chat-count').textContent = chats.length || '';
    if (!chats.length) $('chat-list').append(el('div', 'history-empty', $('search').value ? 'No conversations found.' : 'A fresh space. Start something new.'));
    for (const chat of chats) {
      const row = el('div', `history-row${state.chat?.id === chat.id ? ' active' : ''}`);
      const button = el('button', 'history-open');
      const title = chat.title || 'New conversation';
      button.append(el('span', 'history-title', title)); button.title = title;
      const date = new Date(chat.last_message || chat.created);
      button.append(el('span', 'history-date', `${chat.mode ? chat.mode + ' · ' : ''}${Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'})}`));
      button.onclick = () => safe(() => openChat(chat.id));
      const actions = el('div', 'history-actions');
      for (const [symbol, name, fn] of [['✎', 'Rename', () => editChat(chat, false)], ['×', 'Delete', () => editChat(chat, true)]]) {
        const action = el('button', '', symbol); action.title = name; action.setAttribute('aria-label', `${name} ${title}`);
        action.onclick = () => safe(fn); actions.append(action);
      }
      row.append(button, actions); $('chat-list').append(row);
    }
    controls();
  }
  async function refreshChats() {
    const version = ++state.searchVersion;
    const query = $('search').value.trim();
    const chats = await json(query ? `/chats/search?q=${encodeURIComponent(query)}` : '/chats');
    if (version !== state.searchVersion) return;
    state.chats = chats; history(chats);
  }
  function sidebar(open) {
    document.body.classList.toggle('sidebar-open', open); $('backdrop').hidden = !open;
    $('menu').setAttribute('aria-expanded', String(open));
  }
  function setChat(chat) {
    state.chat = chat;
    $('chat-title').textContent = chat?.title || 'New conversation';
    $('mode').value = chat?.mode || 'work';
    $('permission').value = chat?.permission || 'manual';
    $('cwd').value = chat?.cwd || '';
    const web = chat?.web ?? true;
    $('web').setAttribute('aria-pressed', String(web)); $('web').lastElementChild.textContent = `Web ${web ? 'on' : 'off'}`;
    $('welcome').hidden = Boolean(chat?.messages?.some(m => m.role === 'user'));
    $('messages').replaceChildren();
    for (const msg of chat?.messages || []) {
      if (msg.role === 'user') message('user', msg.content);
      else if (msg.role === 'assistant') {
        const body = message('assistant', msg.content);
        if (msg.tool_calls?.length) for (const call of msg.tool_calls) body.append(el('div', 'tool-row', `◈ ${call.function?.name || 'Tool'}`));
      }
      // Raw tool payloads stay in the server history, never masquerade as replies.
    }
    if (chat?.plan?.length) $('messages').append(planView(chat.plan));
    nearBottom = true; requestAnimationFrame(follow); history(state.chats); sidebar(false); controls();
  }
  async function openChat(id) {
    if (state.busy) return;
    const version = ++state.navigationVersion;
    const chat = await json(`/chats/${id}`);
    if (version === state.navigationVersion && !state.busy) setChat(chat);
  }
  async function newChat() { if (!state.busy) { ++state.navigationVersion; setChat(null); $('prompt').focus(); } }
  async function editChat(chat, remove) {
    if (state.busy) return;
    const dialog = $('chat-dialog');
    $('dialog-title').textContent = remove ? 'Delete conversation?' : 'Rename conversation';
    $('dialog-description').textContent = remove ? 'This removes the conversation from this computer. It cannot be undone.' : '';
    $('rename-label').hidden = remove; $('rename-input').value = chat.title || '';
    $('dialog-confirm').textContent = remove ? 'Delete' : 'Save';
    dialog.returnValue = ''; dialog.showModal();
    const choice = await new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue), {once: true}));
    if (choice !== 'confirm') return;
    if (remove) {
      await json(`/chats/${chat.id}`, {method: 'DELETE'});
      if (state.chat?.id === chat.id) setChat(null);
    } else {
      const title = $('rename-input').value.trim(); if (!title) return toast('Give your conversation a name.');
      await json(`/chats/${chat.id}`, write('PATCH', {title}));
      if (state.chat?.id === chat.id) { state.chat.title = title; $('chat-title').textContent = title; }
    }
    await refreshChats();
  }
  async function health() {
    try {
      const status = await json('/health');
      if (!state.connected) return;
      retry.hidden = true;
      const configured = Boolean(state.models.find(model => model.name === status.model)?.model);
      const ready = status.model_ok && configured;
      $('connection').className = `connection${ready ? ' online' : ''}`;
      $('connection').lastElementChild.textContent = ready ? 'Model connected' : configured ? 'Model offline' : 'Model not set up';
      banner(ready ? '' : configured ? 'Your space is ready. Model offline: start your model server, or run kova setup, then try again.' : 'Your space is ready. Choose a model first: run kova setup, then reopen Kova.');
    } catch (_) {
      if (!state.connected) return;
      retry.hidden = false;
      $('connection').className = 'connection'; $('connection').lastElementChild.textContent = 'Server offline';
      banner('Cannot reach Kova. Open kova-web to restart the server, then reload this window.');
    }
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
  function planView(items) {
    const section = el('section', 'plan-view'); section.setAttribute('aria-label', 'Plan');
    section.append(el('strong', '', 'Plan'));
    const list = el('ul');
    for (const item of items) {
      const row = el('li', `plan-${item.status}`);
      const symbol = item.status === 'done' ? '✓' : item.status === 'in_progress' ? '◉' : '○';
      const icon = el('span', '', symbol); icon.setAttribute('aria-label', item.status.replace('_', ' '));
      row.append(icon, document.createTextNode(item.text)); list.append(row);
    }
    section.append(list); return section;
  }
  function approvalCard(event) {
    const card = el('section', 'approval-card'); card.setAttribute('aria-label', 'Tool approval');
    const heading = el('div', 'approval-heading');
    heading.append(el('strong', '', 'Kova needs your approval'), el('span', `risk-badge risk-${event.risk}`, event.risk));
    card.append(heading, el('pre', '', event.args?.command || event.summary));
    if (event.args && event.tool !== 'run_command') {
      const details = el('details', 'approval-arguments');
      details.append(el('summary', '', 'Requested changes'), el('pre', '', JSON.stringify(event.args, null, 2)));
      card.append(details);
    }
    if (state.chat?.cwd) card.append(el('small', 'approval-folder', state.chat.cwd));
    const label = el('label', 'approval-message', 'Optional: what to do instead');
    const input = el('input'); input.maxLength = 4000; label.append(input); card.append(label);
    const actions = el('div', 'approval-actions'), status = el('div', 'approval-status');
    status.setAttribute('role', 'status');
    const buttons = [];
    for (const [decision, text] of [['allow', 'Allow'], ['allow_session', 'Allow for this chat'], ['deny', 'Deny']]) {
      const button = el('button', 'control', text); button.type = 'button';
      if (event.risk === 'destructive' && decision === 'allow_session') {
        button.disabled = true; button.title = 'Destructive commands always need individual approval';
      }
      button.onclick = () => safe(async () => {
        buttons.forEach(b => { b.disabled = true; }); input.disabled = true;
        try {
          await json(`/chats/${state.chat.id}/approvals/${event.id}`, write('POST', {decision, message: input.value || null}));
          status.textContent = decision === 'deny' ? 'Denied' : 'Allowed';
        } catch (error) {
          buttons.forEach(b => { b.disabled = event.risk === 'destructive' && b === buttons[1]; });
          input.disabled = false; throw error;
        }
      });
      actions.append(button); buttons.push(button);
    }
    card.append(actions, status);
    return {card, resolve(decision, message) {
      buttons.forEach(b => { b.disabled = true; }); input.disabled = true;
      status.textContent = decision === 'allow_session' ? 'Allowed for this chat' : decision === 'allow' ? 'Allowed' : message || 'Denied';
    }, finish() {
      buttons.forEach(b => { b.disabled = true; }); input.disabled = true;
      if (!status.textContent) status.textContent = 'No longer pending';
    }};
  }
  function turnView(body) {
    let current = null, scheduled = false, finished = false;
    const blocks = [], pendingTools = [], approvals = new Map();
    let plan = null;
    const activity = el('div', 'activity'); activity.append(el('span', 'spinner'), document.createTextNode('Thinking…')); body.append(activity);
    function paint() {
      scheduled = false;
      for (const block of blocks) {
        if (block.kind === 'text' && block.dirty) { markdown(block.node, block.text, finished); block.dirty = false; }
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
          activity.hidden = kind === 'text';
          if (!current || current.kind !== kind) {
            current = {kind, text: '', dirty: true, start: performance.now()};
            if (kind === 'text') current.node = el('div', 'markdown');
            else { current.node = el('details', 'thinking'); current.summary = el('summary', '', 'Thinking…'); current.pre = el('pre'); current.node.append(current.summary, current.pre); }
            body.insertBefore(current.node, activity); blocks.push(current);
          }
          current.text += event.text; current.dirty = true;
        } else if (kind === 'tool_call') {
          current = null; activity.hidden = true;
          const row = el('div', 'tool-row'); const icon = el('span', 'spinner'); const detail = el('span', 'tool-detail', 'Running…');
          row.append(icon, el('strong', '', event.name), detail); body.insertBefore(row, activity);
          pendingTools.push({name: event.name, row, icon, detail, done: false});
        } else if (kind === 'approval') {
          activity.hidden = true;
          const approval = approvalCard(event); approvals.set(event.id, approval);
          body.insertBefore(approval.card, activity);
        } else if (kind === 'approval_resolved') {
          approvals.get(event.id)?.resolve(event.decision, event.message);
        } else if (kind === 'plan') {
          const next = planView(event.items);
          if (plan) plan.replaceWith(next); else body.insertBefore(next, activity);
          plan = next;
        } else if (kind === 'tool_result') {
          const tool = pendingTools.find(tool => tool.name === event.name && !tool.done);
          if (tool) { tool.done = true; tool.icon.className = ''; tool.icon.textContent = event.ok ? '✓' : '✗'; tool.row.classList.toggle('failed', !event.ok); tool.detail.textContent = (event.summary || '').replace(/\s+/g, ' ').slice(0, 200); tool.detail.title = event.summary || ''; }
          current = null; activity.hidden = false;
        } else if (kind === 'error') {
          body.insertBefore(el('div', 'turn-error', event.message), activity);
        } else if (kind === 'stopped') {
          body.insertBefore(el('div', 'turn-status', 'Reply stopped.'), activity);
        }
        schedule();
      },
      finish() {
        endReasoning(); finished = true; activity.remove();
        for (const approval of approvals.values()) approval.finish();
        for (const tool of pendingTools.filter(t => !t.done)) { tool.icon.className = ''; tool.icon.textContent = '·'; tool.detail.textContent = 'Interrupted'; }
        for (const block of blocks) block.dirty = true;
        paint();
      }
    };
  }
  async function send(event) {
    event.preventDefault(); const text = $('prompt').value.trim();
    if (!text || state.busy || !state.connected) return;
    ++state.navigationVersion;
    state.busy = true; state.turnReady = false; state.stopRequested = false; controls();
    let turn, terminal = false;
    try {
      if (!state.chat) state.chat = await json('/chats', write('POST', {mode: $('mode').value, web: $('web').getAttribute('aria-pressed') === 'true', permission: $('permission').value, ...($('cwd').value.trim() ? {cwd: $('cwd').value.trim()} : {})}));
      $('welcome').hidden = true; message('user', text); $('prompt').value = ''; resize();
      nearBottom = true; turn = turnView(message('assistant')); follow();
      const response = await api(`/chats/${state.chat.id}/messages`, write('POST', {text, model: $('model').value || null}));
      state.turnReady = true;
      if (state.stopRequested) await cancelTurn();
      await readEvents(response, data => {
        if (terminal) return;
        terminal = ['done', 'error', 'stopped'].includes(data.type);
        turn.accept(data);
      });
      if (!terminal) throw new Error('Reply connection interrupted. Reopen this conversation to check saved messages; it may still be running.');
    } catch (error) {
      if (turn) turn.accept({type: 'error', message: error.message}); else toast(error.message);
    } finally {
      turn?.finish(); state.busy = false; state.turnReady = false; state.stopRequested = false; controls();
      if (document.activeElement === $('prompt')) $('prompt').focus({preventScroll: true});
      if (state.connected) {
        try {
          // Update title/history without replacing the live reasoning and tool rows.
          if (state.chat) { state.chat = await json(`/chats/${state.chat.id}`); $('chat-title').textContent = state.chat.title || 'New conversation'; }
          await refreshChats(); await health();
        } catch (error) { toast(error.message); }
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
  function safe(fn) { Promise.resolve().then(fn).catch(error => toast(error.message)); }
  function resize() { $('prompt').style.height = 'auto'; $('prompt').style.height = `${Math.min($('prompt').scrollHeight, 180)}px`; controls(); }
  $('composer').addEventListener('submit', send);
  $('prompt').addEventListener('input', resize);
  $('prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); $('composer').requestSubmit(); } });
  $('stop').onclick = stop; $('new-chat').onclick = newChat;
  $('menu').onclick = () => sidebar(!document.body.classList.contains('sidebar-open'));
  $('backdrop').onclick = () => sidebar(false);
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); if (state.connected) safe(newChat); }
    if (event.key === 'Escape') sidebar(false);
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
  $('permission').onchange = () => safe(async () => {
    try { await chatSetting({permission: $('permission').value}); }
    catch (error) { $('permission').value = state.chat?.permission || 'manual'; throw error; }
  });
  $('cwd').onchange = () => safe(async () => {
    try { await chatSetting({cwd: $('cwd').value.trim()}); }
    catch (error) { $('cwd').value = state.chat?.cwd || ''; throw error; }
  });
  $('web').onclick = () => safe(async () => {
    const web = $('web').getAttribute('aria-pressed') !== 'true';
    await chatSetting({web}); $('web').setAttribute('aria-pressed', String(web)); $('web').lastElementChild.textContent = `Web ${web ? 'on' : 'off'}`;
  });
  let selectedModel = '';
  $('model').onchange = () => safe(async () => {
    try { await json('/models/active', write('POST', {name: $('model').value})); selectedModel = $('model').value; await health(); }
    catch (error) { $('model').value = selectedModel; throw error; }
  });
  function theme(value) {
    document.documentElement.dataset.theme = value;
    $('theme').lastElementChild.textContent = value === 'light' ? 'Dark theme' : 'Light theme';
    try { localStorage.setItem('kova-theme', value); } catch (_) {}
  }
  $('theme').onclick = () => theme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  try { theme(localStorage.getItem('kova-theme') || 'dark'); } catch (_) { theme('dark'); }
  $('disconnect').onclick = () => safe(async () => { await json('/app/logout', {method: 'POST'}); setChat(null); $('chat-list').replaceChildren(); disconnected('Disconnected. Open kova-web to reconnect.'); });
  document.querySelectorAll('[data-prompt]').forEach(button => { button.onclick = () => { $('prompt').value = button.dataset.prompt; resize(); $('prompt').focus(); }; });
  async function start() {
    if (state.starting) return;
    state.starting = true; state.events?.close();
    // Strip before any asynchronous work, so a consumed ticket never stays in history.
    const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
    historyReplace();
    try {
      if (ticket) await json('/app/login', write('POST', {ticket}));
      await json('/chats'); state.connected = true;
      const [models, status] = await Promise.all([json('/models'), json('/health')]);
      state.models = models;
      renderModels(models);
      $('model').value = status.model; selectedModel = status.model;
      await refreshChats(); controls(); await health();
      state.events = new EventSource('/events');
      state.events.addEventListener('reminder', event => { try { toast(JSON.parse(event.data).text, 'Reminder'); } catch (_) {} });
      state.events.onerror = () => { if (state.connected) health(); };
      $('prompt').focus();
    } catch (error) { disconnected(error.message); }
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
  }

  let providerSpecs = [], providerDraft = null, providerChoices = [], providerLogin = null, providerTimer = null;
  let providerVersion = 0;
  function clearProviderSecrets() {
    $('provider-key').value = ''; $('provider-headers').value = '';
    providerDraft = null;
  }
  function closeProvider() {
    providerVersion++; clearTimeout(providerTimer);
    if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {});
    providerLogin = null; clearProviderSecrets(); providerChoices = [];
    $('provider-dialog').close();
  }
  function providerKindChanged() {
    clearProviderSecrets();
    const spec = providerSpecs.find(p => p.id === $('provider-kind').value);
    if (!spec) return;
    $('provider-name').value = spec.id;
    const custom = spec.id === 'custom', local = spec.id === 'local', oauth = spec.id === 'chatgpt';
    $('provider-url-row').hidden = !custom && !local;
    $('provider-url').value = ''; $('provider-url').placeholder = local ? 'homelab:8080' : 'https://example.com/v1';
    $('provider-key-row').hidden = oauth || spec.id === 'codex';
    $('provider-key').required = Boolean(spec.key_url);
    $('provider-headers-row').hidden = !custom;
    $('provider-consent-row').hidden = spec.id !== 'codex'; $('provider-consent').checked = false;
    $('provider-key-link').hidden = !spec.key_url; $('provider-key-link').href = spec.key_url || '#';
    $('provider-next').textContent = oauth ? 'Continue with ChatGPT' : 'List models';
    $('provider-status').textContent = '';
  }
  function providerActiveOptions() {
    const previous = $('provider-active').value;
    const selected = providerChoices.filter(c => c.input.checked);
    $('provider-active').replaceChildren(...selected.map(c => { const option = el('option', '', c.model); option.value = c.model; return option; }));
    if (selected.some(c => c.model === previous)) $('provider-active').value = previous;
    $('provider-next').disabled = !selected.length;
  }
  function showProviderModels(models) {
    if (!models.length) throw new Error('No models listed. Check access or load a model first.');
    providerChoices = models.map((model, index) => {
      const label = el('label', 'provider-check'), input = el('input');
      input.type = 'checkbox'; input.checked = index === 0;
      label.append(input, document.createTextNode(model));
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
    $('provider-models-step').hidden = true; $('provider-back').hidden = true;
    $('provider-next').disabled = false; $('provider-details').querySelectorAll('input,select,textarea').forEach(field => { field.disabled = false; }); providerKindChanged(); $('provider-dialog').showModal();
  });
  $('provider-kind').onchange = providerKindChanged;
  $('provider-cancel').onclick = closeProvider;
  $('provider-dialog').addEventListener('cancel', event => { event.preventDefault(); closeProvider(); });
  $('provider-back').onclick = () => {
    providerVersion++; clearTimeout(providerTimer);
    if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {});
    providerLogin = null; clearProviderSecrets(); providerChoices = [];
    $('provider-models-step').hidden = true; $('provider-details').hidden = false;
    $('provider-back').hidden = true; $('provider-next').disabled = false; $('provider-details').querySelectorAll('input,select,textarea').forEach(field => { field.disabled = false; }); providerKindChanged();
  };
  $('provider-form').onsubmit = async event => {
    event.preventDefault(); const version = ++providerVersion;
    // Open synchronously with the gesture so browser sign-in is not popup-blocked.
    const signIn = !providerDraft && $('provider-kind').value === 'chatgpt';
    const popup = signIn ? window.open('about:blank', '_blank') : null;
    if (popup) popup.opener = null;
    $('provider-next').disabled = true;
    $('provider-details').querySelectorAll('input,select,textarea').forEach(field => { field.disabled = true; });
    try {
      if (providerDraft) {
        const selected = providerChoices.filter(c => c.input.checked).map(c => c.model);
        await json('/providers', write('POST', {...providerDraft, models: selected, active: $('provider-active').value}));
        if (version !== providerVersion) return;
        providerLogin = null; closeProvider();
        const [models, status] = await Promise.all([json('/models'), json('/health')]);
        state.models = models; renderModels(models); $('model').value = status.model; selectedModel = status.model;
        await health(); toast('Provider connected. Ready when you are.'); return;
      }
      const kind = $('provider-kind').value;
      const body = {kind, id: $('provider-name').value, name: $('provider-name').value,
        api_key: $('provider-key').value || null, consent: $('provider-consent').checked};
      if (kind === 'custom' || kind === 'local') body.base_url = $('provider-url').value;
      if (kind === 'custom' && $('provider-headers').value.trim()) {
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
          const link = el('a', '', 'Continue with ChatGPT ↗'); link.href = login.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
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
          } catch (error) { if (version === providerVersion) { $('provider-status').textContent = error.message; $('provider-next').disabled = false; $('provider-details').querySelectorAll('input,select,textarea').forEach(field => { field.disabled = false; }); if (providerLogin) api(`/providers/auth/${providerLogin}`, {method: 'DELETE'}).catch(() => {}); providerLogin = null; } }
        };
        await poll(); return;
      }
      const data = await json('/providers/validate', write('POST', body));
      if (version !== providerVersion) return;
      showProviderModels(data.models); providerDraft = body; clearProviderFieldValues();
    } catch (error) {
      popup?.close(); if (version === providerVersion) $('provider-status').textContent = error.message;
    } finally { if (version === providerVersion && providerDraft) providerActiveOptions();
      if (version === providerVersion && !providerDraft && (!signIn || !providerLogin)) { $('provider-next').disabled = false; $('provider-details').querySelectorAll('input,select,textarea').forEach(field => { field.disabled = false; }); } }
  };
  function clearProviderFieldValues() { $('provider-key').value = ''; $('provider-headers').value = ''; }
  // Keep these API-backed panels in the client; credentials never enter storage.
  const panels = el('div', 'footer-actions'); $('disconnect').parentNode.parentNode.append(panels);
  for (const kind of ['memory', 'reminders']) {
    const launch = el('button', 'quiet', kind === 'memory' ? 'Memory' : 'Reminders');
    launch.type = 'button'; panels.append(launch);
    const dialog = el('dialog'); dialog.setAttribute('aria-label', launch.textContent);
    const heading = el('h2', '', launch.textContent), close = el('button', 'quiet', 'Close');
    close.type = 'button'; close.onclick = () => dialog.close();
    const search = el('input'); search.type = 'search'; search.placeholder = 'Search memory'; search.setAttribute('aria-label', 'Search memory');
    const list = el('div'), form = el('form'), title = el('input'), body = el('textarea');
    title.required = true; body.required = true;
    title.placeholder = kind === 'memory' ? 'Title' : 'When (e.g. in 10 min)';
    title.setAttribute('aria-label', title.placeholder);
    body.placeholder = kind === 'memory' ? 'Note' : 'Reminder'; body.setAttribute('aria-label', body.placeholder);
    const save = el('button', 'primary', 'Add'); save.type = 'submit';
    form.append(title, body, save); dialog.append(heading, close, list, form); document.body.append(dialog);
    if (kind === 'memory') dialog.insertBefore(search, list);
    search.oninput = () => { ++version; safe(refresh); };
    let version = 0;
    async function refresh() {
      const current = ++version, rows = await json(`/${kind}${kind === 'memory' && search.value.trim() ? '?q=' + encodeURIComponent(search.value.trim()) : ''}`);
      if (current !== version) return;
      list.replaceChildren();
      if (!rows.length) list.append(el('p', '', 'Nothing here yet.'));
      for (const row of rows) {
        const item = el('section'), remove = el('button', 'quiet', 'Delete'); remove.type = 'button';
        item.append(el('strong', '', row.title || row.text), el('p', '', kind === 'memory' ? row.body : new Date(row.when || row.reminder_at).toLocaleString()), remove);
        remove.onclick = () => safe(async () => {
          remove.disabled = true;
          try { await json(`/${kind}/${encodeURIComponent(row.slug ?? row.id)}`, {method: 'DELETE'}); await refresh(); }
          finally { remove.disabled = false; }
        }); list.append(item);
      }
    }
    launch.onclick = () => safe(async () => { dialog.showModal(); await refresh(); });
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
  const retry = el('button', 'quiet', 'Retry connection');
  retry.hidden = true; retry.type = 'button'; retry.onclick = () => safe(start);
  $('connection').parentNode.append(retry);
  window.addEventListener('online', () => safe(start));
  function historyReplace() { if (location.hash) window.history.replaceState(null, '', location.pathname + location.search); }
  window.addEventListener('beforeunload', () => state.events?.close());
  setInterval(() => { if (state.connected) health(); }, 30000);
  window.addEventListener('hashchange', () => { if (location.hash.includes('ticket=') && !state.busy) { state.events?.close(); start(); } });
  controls(); start();
})();

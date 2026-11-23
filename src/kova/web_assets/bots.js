/* Saved bot contacts. The API, not the editor, determines what a bot may do. */
(() => {
  'use strict';

  const idPattern = /^[a-z0-9][a-z0-9-]{0,31}$/;
  const reserved = new Set(['kova', 'all', 'everyone', 'here', 'you']);
  const avatarTypes = new Set(['image/png', 'image/jpeg', 'image/webp']);
  const avatarLimit = 256 * 1024;

  function suggestHandle(name, taken) {
    let base = name.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 32);
    if (!base || reserved.has(base)) base = 'bot';
    let handle = base;
    for (let number = 2; taken.has(handle); number += 1) {
      const suffix = `-${number}`;
      handle = base.slice(0, 32 - suffix.length) + suffix;
    }
    return handle;
  }

  const editorMarkup = `
    <header class="bot-editor-bar">
      <div class="bot-editor-width">
        <button type="button" class="btn quiet bot-editor-back">
          Back
        </button>
        <h1 id="bot-editor-title" tabindex="-1"></h1>
        <button type="submit" form="bot-form" class="btn primary bot-save">Create bot</button>
      </div>
    </header>
    <div class="bot-editor-scroll">
      <div class="bot-editor-width">
        <p class="bot-load-status bot-note" role="status" hidden></p>
        <button type="button" class="btn quiet bot-retry" hidden>Retry</button>
        <form id="bot-form" hidden>
          <fieldset class="bot-fields">
            <section class="bot-form-section" aria-labelledby="bot-identity-title">
              <h2 id="bot-identity-title">Identity</h2>
              <div class="bot-photo">
                <div id="bot-preview" aria-hidden="true"></div>
                <div class="bot-photo-actions">
                  <div class="bot-photo-buttons">
                    <input id="bot-image" type="file" accept="image/png,image/jpeg,image/webp" hidden>
                    <button type="button" class="btn bot-upload">Upload image</button>
                    <button type="button" class="btn quiet bot-clear" hidden>Remove image</button>
                  </div>
                  <p class="bot-note">PNG, JPG or WebP, up to 256 KB.</p>
                  <p class="bot-error bot-image-error" role="alert" hidden></p>
                </div>
              </div>
              <div class="bot-identity-fields">
                <label>Name<input id="bot-name" name="name" required maxlength="40" autocomplete="off"></label>
                <label>Handle
                  <span class="bot-handle-field">
                    <span aria-hidden="true">@</span>
                    <input id="bot-handle" name="handle" required maxlength="32"
                           pattern="[a-z0-9][a-z0-9\\-]{0,31}" title="Use lowercase letters, numbers, or hyphens"
                           autocomplete="off" autocapitalize="none" spellcheck="false">
                  </span>
                </label>
              </div>
              <label class="bot-field">Description
                <input id="bot-description" name="description" maxlength="120" placeholder="What is this bot for?">
              </label>
            </section>
            <section class="bot-form-section" aria-labelledby="bot-persona-title">
              <h2 id="bot-persona-title"><label for="bot-persona">Persona</label></h2>
              <p id="bot-persona-hint" class="bot-note">Tell this bot how to respond and what to focus on.</p>
              <textarea id="bot-persona" name="persona" rows="3" maxlength="8000" aria-describedby="bot-persona-hint"
                        placeholder="Example: Explain trade-offs and end with a short recommendation."></textarea>
            </section>
            <section class="bot-form-section" aria-labelledby="bot-capabilities-title">
              <h2 id="bot-capabilities-title">Model and tools</h2>
              <div class="bot-field">
                <label for="bot-model">Model</label>
                <select id="bot-model" name="model"></select>
              </div>
              <p class="bot-note bot-model-note" hidden></p>
              <label class="bot-tools-row">
                <span><strong id="bot-tools-label">Enable tools</strong><span id="bot-tools-note" class="bot-note"></span></span>
                <input id="bot-tools" name="tools" type="checkbox" aria-labelledby="bot-tools-label" aria-describedby="bot-tools-note">
              </label>
              <div class="bot-field">
                <label for="bot-ceiling">Approval limit</label>
                <select id="bot-ceiling" name="ceiling" aria-describedby="bot-ceiling-note">
                  <option value="manual">Manual</option>
                  <option value="auto">Auto</option>
                  <option value="full">Full access</option>
                </select>
              </div>
              <p class="bot-note" id="bot-ceiling-note"></p>
            </section>
          </fieldset>
          <p class="bot-error bot-save-error" role="alert" tabindex="-1" hidden></p>
        </form>
      </div>
    </div>`;

  function create({el, icon, json, write, safe, models, activeModel, chat, available,
                   onChange, onEditorOpen, openChat, focusChat, toolLabel, toolHint, changeFiles, reviewChanges, directory, editor, header, welcome}) {
    const records = new Map();
    const avatarVersion = new Map();
    let listState = 'loading', listVersion = 0, editing = null;
    let contactsKey, headerKey;
    const get = id => records.get(id);
    const contact = chat => chat?.kind === 'bot' && get(chat.bot_id)?.state === 'ok' ? get(chat.bot_id) : null;
    const botModel = chat => {
      const name = contact(chat)?.model;
      return models().find(entry => entry.name === name && entry.model) || null;
    };
    const botPath = id => `/bots/${encodeURIComponent(id)}`;

    function button(text, kind = 'btn quiet', glyph) {
      const node = el('button', kind);
      node.type = 'button';
      if (glyph) node.append(icon(glyph));
      if (text) node.append(el('span', '', text));
      return node;
    }

    function avatar(bot, {preview, name = bot.name} = {}) {
      const mark = el('span', 'bot-avatar avatar');
      mark.setAttribute('aria-hidden', 'true');
      const look = window.KovaChatAppearance?.appearanceOf(bot);
      if (look?.colour) mark.dataset.chatColour = look.colour;
      // No icon and no picture: the bot gets its character face (characters.js).
      const face = !look?.icon && !(preview ?? bot.image) && window.KovaCharacters?.face(bot.id || name || '');
      if (look?.icon) mark.append(icon(look.icon));
      else if (face) { mark.classList.add('has-character'); mark.append(face); }
      else mark.textContent = Array.from((name || bot.id || '').trim())[0]?.toUpperCase() || '?';
      const image = preview ?? (bot.image && bot.id ? `${botPath(bot.id)}/avatar?v=${avatarVersion.get(bot.id) || 0}` : '');
      if (image) {
        const img = el('img');
        img.alt = '';
        img.addEventListener('error', () => img.remove());
        img.src = image;
        mark.append(img);
      }
      return mark;
    }

    const contacts = el('div', 'bot-contacts');
    const body = el('div', 'bot-contacts-body');
    contacts.append(body);
    const ordered = () => window.KovaChatList.contacts([...records.values()]).bots;
    const when = new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit'});
    const day = new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'});

    function renderContacts(list, chats, query) {
      const term = query.toLocaleLowerCase();
      const people = ordered().filter(bot => !term || [bot.name, bot.id, bot.handle].some(
        name => name?.toLocaleLowerCase().includes(term.replace(/^@/, ''))));
      const rows = people.map(bot => [bot, window.KovaChatList.latestDirectChat(chats, bot.id)]);
      rows.sort((a, b) => Number(!!b[1]?.pinned) - Number(!!a[1]?.pinned)
        || (b[1]?.last_message || '').localeCompare(a[1]?.last_message || ''));
      const key = JSON.stringify([listState, rows, [...avatarVersion], chat()?.id]);
      if (key !== contactsKey) {
        contactsKey = key;
        body.replaceChildren();
        for (const [bot, recent] of rows) {
          const name = bot.name || bot.id;
          const row = el('div', 'bot-contact');
          row.dataset.botId = bot.id;
          const open = button('', 'bot-open');
          open.setAttribute('aria-label', name);
          if (bot.id === chat()?.bot_id) open.setAttribute('aria-current', 'true');
          const details = el('span', 'bot-contact-details');
          const top = el('span', 'bot-contact-line');
          top.append(el('strong', 'bot-contact-name', name));
          const date = new Date(recent?.last_message || '');
          if (recent?.message_count && Number.isFinite(date.getTime())) {
            top.append(el('time', 'bot-contact-time',
              (window.KovaChatList.bucket(recent) === 'today' ? when : day).format(date)));
          }
          const activity = recent?.state;
          if (activity === 'working' || activity === 'blocked-on-approval') row.dataset.state = activity;
          const preview = activity === 'working' ? 'Working…'
            : activity === 'blocked-on-approval' ? 'Waiting for your approval'
            : bot.state !== 'ok' ? 'Unavailable'
            : recent?.message_preview || (recent?.message_count ? recent.title : bot.description) || 'Start a conversation';
          details.append(top, el('span', 'bot-contact-preview', preview));
          open.append(avatar(bot), details);
          open.onclick = () => safe(() => openChat(bot));
          row.append(open);
          if (bot.state === 'ok') {
            const actions = el('details', 'bot-actions history-actions');
            const more = el('summary', 'icon-button');
            more.setAttribute('aria-label', `Actions for ${name}`);
            more.append(icon('ellipsis'));
            const start = button('New chat', 'btn');
            start.onclick = () => { actions.open = false; safe(() => openChat(bot, true)); };
            const edit = button('Edit bot', 'btn');
            edit.onclick = () => { actions.open = false; openEditor(bot.id, more); };
            actions.append(more, start, edit);
            row.append(actions);
          }
          body.append(row);
        }
      }
      if (rows.length) list.append(contacts);
      return rows.length > 0;
    }

    const inboxAction = directory.querySelector('.bot-inbox-action');
    const inboxNote = directory.querySelector('.bot-directory-body');
    const inboxTitle = directory.querySelector('h1');
    function renderDirectory() {
      const bot = ordered().find(item => item.state === 'ok');
      inboxTitle.textContent = !bot && listState === 'ready' ? 'Create your first bot' : 'Pick up a conversation';
      inboxNote.textContent = {
        loading: 'Loading bots…', error: 'Could not load bots.', offline: 'Bots are offline.',
      }[listState] || (bot ? 'Pick a bot from your inbox.' : 'Give a bot its own instructions and model.');
      inboxAction.hidden = listState === 'loading' || listState === 'offline';
      inboxAction.textContent = listState === 'error' ? 'Retry' : bot ? `Open ${bot.name}` : 'New bot';
      inboxAction.onclick = listState === 'error' ? () => safe(refresh)
        : bot ? () => safe(() => openChat(bot)) : () => openEditor(null, inboxAction);
    }

    function decorateHistory(title, chat) {
      const bot = contact(chat);
      if (bot) title.prepend(avatar(bot));
    }

    function refreshControls() {
      const enabled = available();
      inboxAction.disabled = !enabled;
      for (const row of body.querySelectorAll('.bot-contact')) {
        row.firstElementChild.disabled = !enabled || get(row.dataset.botId)?.state !== 'ok';
        for (const action of row.querySelectorAll('.bot-actions button')) action.disabled = !enabled;
      }
      editHeader.disabled = !enabled;
    }

    function paletteActions() {
      if (!available()) return [];
      const group = window.KovaChatList.contacts([...records.values()]);
      return [
        {title: 'New bot', hint: 'Save an assistant with its own persona and model', keywords: 'create contact assistant',
          run: () => openEditor()},
        ...group.bots.filter(bot => bot.state === 'ok').map(bot => ({
          title: `Chat with ${bot.name}`, hint: `@${bot.handle}`, keywords: `bot contact ${bot.handle}`,
          run: () => safe(() => openChat(bot)),
        })),
      ];
    }

    async function refresh(changedId) {
      if (changedId) avatarVersion.set(changedId, (avatarVersion.get(changedId) || 0) + 1);
      const version = ++listVersion;
      try {
        const rows = await json('/bots');
        if (version !== listVersion) return;
        records.clear();
        for (const bot of rows) records.set(bot.id, bot);
        listState = 'ready';
      } catch (_) {
        if (version !== listVersion) return;
        listState = 'error';
      }
      onChange();
    }

    const identity = el('div', 'bot-chat-identity');
    const editHeader = button('', 'icon-button bot-edit-header', 'pencil');
    editHeader.setAttribute('aria-label', 'Edit bot');
    editHeader.title = 'Edit bot';
    editHeader.onclick = () => {
      const bot = contact(chat());
      if (bot) openEditor(bot.id);
    };
    header.append(identity, editHeader);

    const workTools = new Map();
    const workFiles = new Map();
    const workNode = el('div', 'bot-work');
    const workTabs = el('div', 'bot-work-tabs');
    workTabs.setAttribute('role', 'group');
    workTabs.setAttribute('aria-label', 'Work panel view');
    const activityTab = button('Activity', 'btn quiet');
    const filesTab = button('Files', 'btn quiet');
    const workContent = el('div', 'bot-work-content');
    const workPreview = el('section', 'bot-work-preview');
    workPreview.setAttribute('aria-label', 'Tool result preview');
    const workActions = el('div', 'bot-work-actions');
    workActions.hidden = true;
    const workToggle = button('Work panel', 'btn quiet bot-work-toggle', 'columns-2');
    const focusButton = button('Open bot', 'btn quiet bot-focus-button', 'external-link');
    workToggle.setAttribute('aria-controls', 'side-panel');
    workToggle.setAttribute('aria-label', 'Work panel');
    workTabs.append(activityTab, filesTab);
    workNode.append(workTabs, workPreview, workContent);
    workActions.append(focusButton, workToggle);
    header.after(workActions);

    let workChatId, showingFiles = false, focused = false, selectedStep;

    function paintWorkPreview() {
      const selected = workTools.get(selectedStep);
      const step = selected?.preview ? selected : [...workTools.values()].reverse().find(item => item.preview);
      workPreview.hidden = showingFiles || !step;
      if (!step) return;
      const header = el('div', 'bot-work-preview-head');
      header.append(icon('paperclip'), el('strong', '', step.hint || step.label));
      workPreview.replaceChildren(header, el('pre', '', step.preview));
    }
    function paintWork() {
      paintWorkPreview();
      activityTab.setAttribute('aria-pressed', String(!showingFiles));
      filesTab.setAttribute('aria-pressed', String(showingFiles));
      const list = el('ol', 'bot-work-list');
      if (showingFiles) {
        for (const [name, review] of workFiles) {
          const row = el('li', 'bot-work-file');
          if (review) {
            const open = button(name, 'btn quiet bot-work-open-file', 'external-link');
            open.onclick = () => safe(() => reviewChanges(workChatId, review.checkpoint, [review.file], open));
            row.append(open);
          } else row.append(icon('paperclip'), el('span', '', name));
          list.append(row);
        }
      } else {
        for (const [id, step] of Array.from(workTools).slice(-24).reverse()) {
          const row = el('li', 'bot-work-step');
          const label = el('span', 'bot-work-label');
          const status = {running: 'Running', done: 'Done', failed: 'Failed', waiting: 'Needs approval', stopped: 'Stopped'}[step.state];
          row.dataset.state = step.state;
          label.append(el('strong', '', step.label));
          if (step.hint) label.append(el('span', '', step.hint));
          const target = step.preview ? button('', 'bot-work-preview-button') : row;
          if (step.preview) {
            target.setAttribute('aria-label', `Preview ${step.hint || step.label}`);
            target.onclick = () => { selectedStep = id; paintWorkPreview(); };
            row.append(target);
          }
          target.append(el('span', 'bot-work-indicator'), label, el('span', 'bot-work-state', status));
          list.append(row);
        }
      }
      workContent.replaceChildren(list.children.length ? list : el('p', 'bot-work-empty',
        showingFiles ? 'Files attached or changed in this thread will appear here.'
          : 'Tool calls and approvals will appear here as the bot works.'));
    }

    function addFiles(files) {
      for (const file of files || []) {
        const name = file.name || file.path;
        if (name && !workFiles.has(name)) workFiles.set(name, null);
      }
    }

    function addCheckpoints(chatId, checkpoints) {
      if (chatId !== workChatId) return;
      for (const entry of [...checkpoints].reverse()) {
        for (const file of changeFiles(entry.files)) {
          workFiles.set(file.path, {checkpoint: entry.id, file});
        }
      }
      paintWork();
    }

    function addWork(id, name, args, state = 'running') {
      if (id && name) workTools.set(id, {name, label: toolLabel(name), hint: toolHint(args), state});
    }

    function settleWork(id, state, output) {
      const item = workTools.get(id);
      if (!item) return;
      item.state = state;
      if (state !== 'done' || !['read_file', 'list_files'].includes(item.name) || typeof output !== 'string') return;
      let content = output;
      try {
        const value = JSON.parse(output);
        content = typeof value?.content === 'string' ? value.content
          : Array.isArray(value?.files) ? value.files.map(file => file?.path || file).join('\n') : '';
      } catch (_) { /* some tools return plain text */ }
      item.preview = content.trim().slice(0, 420);
    }

    function setWorkChat(current) {
      const id = ['bot', 'group'].includes(current?.kind) ? current.id : null;
      if (workChatId && workChatId !== id) {
        if (!id) window.KovaPanel?.closeTab('bot-work');
        document.body.classList.remove('bot-focus');
        focused = false;
        showingFiles = false;
        selectedStep = null;
      }
      const previousId = workChatId;
      workChatId = id;
      workActions.hidden = !id;
      focusButton.hidden = current?.kind !== 'bot';
      focusButton.querySelector('span:last-child').textContent = focused ? 'Back to inbox' : 'Open bot';
      focusButton.setAttribute('aria-label', focused ? 'Back to inbox' : 'Open bot');
      workTools.clear();
      if (id !== previousId) workFiles.clear();
      if (!id) return;
      for (const msg of current.messages || []) {
        addFiles(msg.attachments);
        for (const call of msg.tool_calls || []) {
          addWork(call.id, call.function?.name, call.function?.arguments,
            current.state === 'working' ? 'running' : 'stopped');
        }
        if (msg.role === 'tool') {
          let ok = true;
          try { const result = JSON.parse(msg.content); ok = result?.ok !== false && !result?.error; }
          catch (_) { /* saved plain-text tool output */ }
          settleWork(msg.tool_call_id, ok ? 'done' : 'failed', msg.content);
        }
      }
      paintWork();
    }

    function beginWork(attachments) {
      if (!workChatId) return;
      addFiles(attachments);
      paintWork();
    }

    function workEvent(event) {
      if (!workChatId) return;
      if (event.type === 'tool_call') addWork(event.id, event.name, event.args);
      else if (event.type === 'tool_result') settleWork(event.id, event.ok === false ? 'failed' : 'done', event.output);
      else if (event.type === 'approval') addWork(event.id, event.name || 'Approval', event.args, 'waiting');
      else if (event.type === 'approval_resolved') settleWork(event.id, event.decision === 'deny' ? 'stopped' : 'done');
      else if (event.type === 'review') {
        if (event.checkpoint_id) addCheckpoints(workChatId, [{id: event.checkpoint_id, files: event.files}]);
        else addFiles(changeFiles(event.files));
      }
      else if (['done', 'error', 'stopped'].includes(event.type)) {
        for (const step of workTools.values()) {
          if (step.state === 'running' || step.state === 'waiting') step.state = 'stopped';
        }
      } else return;
      paintWork();
    }

    activityTab.onclick = () => { showingFiles = false; paintWork(); };
    filesTab.onclick = () => { showingFiles = true; paintWork(); };
    workToggle.onclick = () => {
      const panel = window.KovaPanel;
      if (!panel || !workChatId) return;
      if (panel.isOpen() && workNode.closest('[role="tabpanel"]:not([hidden])')) {
        panel.close();
        return;
      }
      paintWork();
      panel.open({key: 'bot-work', title: 'Work panel', node: workNode, returnFocus: workToggle});
    };
    focusButton.onclick = () => {
      focused = !focused;
      document.body.classList.toggle('bot-focus', focused);
      focusButton.querySelector('span:last-child').textContent = focused ? 'Back to inbox' : 'Open bot';
      focusButton.setAttribute('aria-label', focused ? 'Back to inbox' : 'Open bot');
    };

    function renderChat(current, title, empty) {
      const direct = current?.kind === 'bot';
      const bot = contact(current);
      header.hidden = !bot;
      title.hidden = !!bot;
      welcome.hidden = !direct || !empty;
      const key = JSON.stringify([current?.bot_id, bot, avatarVersion.get(bot?.id)]);
      if (key !== headerKey) {
        headerKey = key;
        if (bot) {
          const name = el('strong', 'bot-chat-name', bot.name);
          const model = el('span', 'bot-chat-model', bot.description || `@${bot.handle}`);
          const details = el('div', 'bot-chat-details');
          name.title = bot.name; model.title = model.textContent;
          details.append(name, model);
          identity.replaceChildren(avatar(bot), details);
          const heading = el('h1', '', `Chat with ${bot.name}`);
          const description = el('p', 'welcome-greeting', bot.description || 'Send a message to start the conversation.');
          welcome.replaceChildren(avatar(bot), heading, description);
        } else if (direct) {
          welcome.replaceChildren(el('h1', '', 'Bot unavailable'),
            el('p', 'welcome-greeting', 'The default assistant will reply in this chat.'));
        }
      }
      return bot;
    }

    function labelReply(label, author, recordedName, defaultName) {
      const bot = author && get(author);
      const name = recordedName || bot?.name || author || defaultName;
      const mark = author ? avatar(bot || {id: author}, {name}) : el('span', 'kova-mark avatar');
      mark.setAttribute('aria-hidden', 'true');
      const article = label.parentNode;
      if (article) {
        article.classList.toggle('bot-reply', !!author);
        if (mark.dataset.chatColour) article.dataset.chatColour = mark.dataset.chatColour;
        else delete article.dataset.chatColour;
      }
      label.replaceChildren(mark, el('span', 'bot-author-name', name));
    }

    editor.innerHTML = editorMarkup;
    editor.setAttribute('aria-labelledby', 'bot-editor-title');
    const field = selector => editor.querySelector(selector);
    const form = field('#bot-form'), fields = field('.bot-fields'), title = field('#bot-editor-title');
    const save = field('.bot-save'), back = field('.bot-editor-back'), status = field('.bot-load-status');
    const retry = field('.bot-retry'), preview = field('#bot-preview'), imageError = field('.bot-image-error');
    const error = field('.bot-save-error'), upload = field('.bot-upload'), clear = field('.bot-clear');
    const {name, handle, description, persona, model, tools, ceiling} = form.elements;
    const image = field('#bot-image');

    function close(restoreFocus = false) {
      if (!editing) return;
      const opener = editing.opener;
      editing = null;
      editor.hidden = true;
      document.body.classList.remove('bot-editor-open');
      if (restoreFocus) {
        if (opener?.getClientRects().length) opener.focus();
        else focusChat();
      }
    }

    function setError(node, message = '') {
      node.textContent = message;
      node.hidden = !message;
    }

    function toolsEnabled(bot) {
      const permissions = bot?.definition?.permissions;
      return !permissions || (permissions.tools.length > 0 && !permissions.deny.includes('*'));
    }

    function updateHints() {
      const note = field('.bot-model-note');
      note.hidden = !model.selectedOptions[0]?.dataset.missing;
      note.textContent = note.hidden ? '' : 'The saved model is unavailable. This bot will use the chat’s model or the active model.';
      field('#bot-tools-note').textContent = tools.checked
        ? 'Uses this bot’s allowed tools. New bots can search, read files and use memory.'
        : 'Conversation only. No tools will be offered to this bot.';
      const detail = {manual: 'Asks before commands and file changes.',
        auto: 'Asks before destructive commands.', full: 'Does not ask for ordinary tool actions.'};
      field('#bot-ceiling-note').textContent = `${detail[ceiling.value]} A chat can require more approvals.`;
    }

    function paintPreview(draft) {
      const look = {...draft.original, name: name.value};
      const pendingImage = draft.removeImage || draft.file ? draft.preview || '' : undefined;
      preview.replaceChildren(avatar(look, {preview: pendingImage}));
      clear.hidden = !(draft.file || (draft.original?.image && !draft.removeImage));
    }

    function fill(draft) {
      const bot = draft.original;
      form.reset();
      name.value = bot?.name || '';
      handle.value = bot?.handle || '';
      description.value = bot?.description || '';
      persona.value = bot?.persona || '';
      const active = models().find(entry => entry.name === activeModel() && entry.model);
      const fallback = active ? `Use active model (${active.model})` : 'Use active model';
      model.replaceChildren(new Option(fallback, ''));
      for (const entry of models()) {
        if (entry.model) model.append(new Option(entry.model === entry.name ? entry.name : `${entry.name} — ${entry.model}`, entry.name));
      }
      if (bot?.model && ![...model.options].some(option => option.value === bot.model)) {
        const missing = new Option(`${bot.model} (unavailable)`, bot.model);
        missing.dataset.missing = 'true';
        model.append(missing);
      }
      model.value = bot?.model || '';
      tools.checked = toolsEnabled(bot);
      ceiling.value = bot?.ceiling || 'manual';
      form.hidden = false;
      save.disabled = false;
      setError(status);
      updateHints();
      paintPreview(draft);
    }

    async function load(draft) {
      form.hidden = true; retry.hidden = true; save.disabled = true;
      setError(status, 'Loading bot…');
      try {
        draft.original = await json(botPath(draft.id));
        if (editing === draft) fill(draft);
      } catch (err) {
        if (editing !== draft) return;
        setError(status, err.message);
        retry.hidden = false;
      }
    }

    function openEditor(id = null, opener = document.activeElement) {
      if (!available() || editing?.saving) return;
      editing = {id, opener, original: null, autoHandle: !id, file: null, preview: '', removeImage: false};
      onEditorOpen();
      editor.hidden = false;
      document.body.classList.add('bot-editor-open');
      title.textContent = id ? 'Edit bot' : 'New bot';
      save.textContent = id ? 'Save changes' : 'Create bot';
      retry.hidden = true;
      setError(status); setError(error); setError(imageError);
      field('.bot-editor-scroll').scrollTop = 0;
      if (id) void load(editing);
      else fill(editing);
      title.focus();
    }

    back.onclick = () => close(true);
    retry.onclick = () => { if (editing) void load(editing); };
    editor.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.isComposing || editing?.saving) return;
      event.preventDefault();
      close(true);
    });
    name.oninput = () => {
      if (editing.autoHandle) {
        const taken = new Set([...records.values()].flatMap(bot => [bot.id, bot.handle]));
        handle.value = name.value.trim() ? suggestHandle(name.value, taken) : '';
      }
      paintPreview(editing);
    };
    handle.oninput = () => { editing.autoHandle = false; };
    model.onchange = tools.onchange = ceiling.onchange = updateHints;
    upload.onclick = () => image.click();
    image.onchange = () => {
      const [file] = image.files, draft = editing;
      image.value = '';
      if (!file || !draft) return;
      if (!avatarTypes.has(file.type) || file.size > avatarLimit) {
        setError(imageError, 'Choose a PNG, JPG or WebP image up to 256 KB.');
        return;
      }
      draft.file = file; draft.preview = ''; draft.removeImage = false;
      setError(imageError);
      paintPreview(draft);
      const reader = new FileReader();
      reader.onload = () => {
        if (editing !== draft || draft.file !== file) return;
        draft.preview = reader.result;
        paintPreview(draft);
      };
      reader.readAsDataURL(file);
    };
    clear.onclick = () => {
      editing.file = null; editing.preview = ''; editing.removeImage = true;
      setError(imageError);
      paintPreview(editing);
      upload.focus();
    };

    function changes(draft) {
      const bot = draft.original;
      const payload = {};
      if (!bot) payload.id = handle.value;
      if (!bot || bot.name !== name.value.trim()) payload.name = name.value.trim();
      if (!bot || bot.handle !== handle.value) payload.handle = handle.value;
      if (!bot || bot.description !== description.value.trim()) payload.description = description.value.trim();
      if (!bot || bot.persona !== persona.value) payload.persona = persona.value;
      if (!bot || bot.model !== model.value) payload.model = {name: model.value};
      if (!bot || bot.ceiling !== ceiling.value) payload.permissions = {ceiling: ceiling.value};
      if (toolsEnabled(bot) !== tools.checked) {
        const rules = bot?.definition.permissions;
        payload.permissions ||= {};
        if (!bot) payload.permissions.tools = [];
        // Keep a custom allowlist when switching tools off. The deny list
        // wins over it; switching them back on restores the previous tools.
        else if (!tools.checked) payload.permissions.deny = [...rules.deny, '*'];
        else {
          payload.permissions.deny = rules.deny.filter(rule => rule !== '*');
          if (!rules.tools.length) payload.permissions.tools = ['web_*', 'read_file', 'list_files', 'memory_*'];
        }
      }
      return payload;
    }

    form.onsubmit = async event => {
      event.preventDefault();
      const draft = editing;
      if (!draft || draft.saving || !available()) return;
      if (!idPattern.test(handle.value) || reserved.has(handle.value)) {
        setError(error, 'Choose a handle with lowercase letters, numbers or hyphens. Reserved handles cannot be used.');
        handle.focus();
        return;
      }
      let definitionSaved = false;
      const creating = !draft.id || draft.created;
      draft.saving = save.disabled = back.disabled = fields.disabled = true;
      setError(error);
      try {
        const payload = changes(draft);
        if (Object.keys(payload).length) {
          const saved = await json(draft.id ? botPath(draft.id) : '/bots', write(draft.id ? 'PATCH' : 'POST', payload));
          draft.id = saved.id; draft.original = saved; draft.created = creating;
          definitionSaved = true;
        }
        if (draft.file) {
          const data = new FormData();
          data.append('file', draft.file);
          await json(`${botPath(draft.id)}/avatar`, {method: 'PUT', body: data});
        } else if (draft.removeImage && draft.original?.image) {
          await json(`${botPath(draft.id)}/avatar`, {method: 'DELETE'});
        }
        await refresh(draft.id);
      } catch (err) {
        if (definitionSaved) await refresh(draft.id);
        if (editing === draft) {
          if (definitionSaved) {
            title.textContent = 'Edit bot'; save.textContent = 'Save changes';
          }
          const prefix = definitionSaved ? 'Bot saved, but the image change failed. ' : '';
          setError(error, prefix + err.message);
          error.focus();
        }
        return;
      } finally {
        draft.saving = false;
        if (editing === draft) save.disabled = back.disabled = fields.disabled = false;
      }
      if (editing !== draft) return;
      const bot = get(draft.id) || draft.original;
      close(!creating);
      if (creating) safe(() => openChat(bot));
    };

    function reset() {
      ++listVersion;
      records.clear(); avatarVersion.clear();
      listState = 'offline';
      setWorkChat(null);
      close();
    }

    return {refresh, reset, renderContacts, renderDirectory, decorateHistory, hasBot: id => get(id)?.state === 'ok',
      setWorkChat, beginWork, workEvent, addCheckpoints, refreshControls, paletteActions, renderChat, contact,
      botModel, labelReply, openEditor, close, editing: () => !!editing, saving: () => !!editing?.saving};
  }

  window.KovaBots = {create};
})();

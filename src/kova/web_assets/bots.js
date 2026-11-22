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
                   onChange, onEditorOpen, openChat, focusChat, editor, header, welcome}) {
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
      if (look?.icon) mark.append(icon(look.icon));
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

    const contacts = el('details', 'bot-contacts');
    contacts.open = true;
    const summary = el('summary', 'bot-contacts-summary');
    const body = el('div', 'bot-contacts-body');
    const add = button('New bot', 'btn quiet bot-new', 'plus');
    add.onclick = () => openEditor();
    contacts.append(summary, body);

    function renderContacts(list, chats, searching) {
      if (searching) return;
      if (contactsKey !== JSON.stringify([listState, [...records.values()], [...avatarVersion]])) {
        contactsKey = JSON.stringify([listState, [...records.values()], [...avatarVersion]]);
        const group = window.KovaChatList.contacts([...records.values()]);
        summary.textContent = group.label;
        summary.append(icon('chevron-down'));
        body.replaceChildren();
        if (listState === 'loading') {
          const status = el('p', 'bot-note bot-contact-note', 'Loading contacts…');
          status.setAttribute('role', 'status');
          body.append(status);
        } else if (listState === 'error') {
          const retry = button('Retry');
          retry.onclick = () => safe(refresh);
          body.append(el('p', 'bot-note bot-contact-note', 'Could not load contacts.'), retry);
        } else if (!group.bots.length) {
          body.append(el('p', 'bot-note bot-contact-note', 'No bots yet. Create a saved assistant with its own persona, model and tools.'));
        }
        for (const bot of group.bots) {
          const name = bot.name || bot.id;
          const row = el('div', 'bot-contact');
          row.dataset.botId = bot.id;
          const open = button('', 'bot-open');
          open.append(avatar(bot), el('span', 'bot-contact-name', name));
          open.title = bot.state === 'ok' ? name : (bot.error || 'Bot unavailable');
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
            actions.addEventListener('keydown', event => {
              if (event.key !== 'Escape' || !actions.open) return;
              event.stopPropagation(); actions.open = false; more.focus();
            });
            actions.append(more, start, edit);
            row.append(actions);
          } else {
            const problem = el('span', 'bot-contact-problem', 'Unavailable');
            problem.title = open.title;
            row.append(problem);
          }
          body.append(row);
        }
        body.append(add);
      }
      const states = new Map();
      for (const row of chats) {
        if (row.kind !== 'bot' || states.get(row.bot_id) === 'working') continue;
        if (row.state === 'working' || row.state === 'blocked-on-approval') states.set(row.bot_id, row.state);
      }
      for (const row of body.querySelectorAll('.bot-contact')) {
        const id = row.dataset.botId;
        const open = row.firstElementChild, bot = get(id);
        if (id === chat()?.bot_id) open.setAttribute('aria-current', 'true');
        else open.removeAttribute('aria-current');
        let activity = row.querySelector('.bot-contact-activity');
        if (activity) activity.remove();
        const state = states.get(id);
        if (state && bot.state === 'ok') {
          const detail = state === 'working' ? 'Working' : 'Waiting for approval';
          activity = el('span', 'bot-contact-activity');
          activity.dataset.state = state;
          activity.append(el('span', 'sr-only', detail));
          activity.title = detail;
          open.append(activity);
        }
      }
      list.append(contacts);
    }

    function decorateHistory(title, chat) {
      const bot = contact(chat);
      if (bot) title.prepend(avatar(bot));
    }

    function refreshControls() {
      const enabled = available();
      add.disabled = !enabled;
      document.getElementById('new-bot').disabled = !enabled;
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
        {title: 'New bot', keywords: 'create contact assistant', run: () => openEditor()},
        ...group.bots.filter(bot => bot.state === 'ok').map(bot => ({
          title: `Chat with ${bot.name}`, keywords: `bot contact ${bot.handle}`,
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

    function renderChat(current, title, modelName, empty) {
      const direct = current?.kind === 'bot';
      const bot = contact(current);
      header.hidden = !bot;
      title.hidden = !!bot;
      welcome.hidden = !direct || !empty;
      const key = JSON.stringify([current?.bot_id, bot, modelName, avatarVersion.get(bot?.id)]);
      if (key !== headerKey) {
        headerKey = key;
        if (bot) {
          const name = el('strong', 'bot-chat-name', bot.name);
          const model = el('span', 'bot-chat-model', modelName || 'No model configured');
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
    const field = selector => editor.querySelector(selector);
    const form = field('#bot-form'), fields = field('.bot-fields'), title = field('#bot-editor-title');
    const save = field('.bot-save'), back = field('.bot-editor-back'), status = field('.bot-load-status');
    const retry = field('.bot-retry'), preview = field('#bot-preview'), imageError = field('.bot-image-error');
    const error = field('.bot-save-error'), upload = field('.bot-upload'), clear = field('.bot-clear');
    const {name, handle, persona, model, tools, ceiling} = form.elements;
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
      listState = 'loading';
      close();
    }

    return {refresh, reset, renderContacts, decorateHistory, refreshControls, paletteActions, renderChat, contact,
      botModel, labelReply, openEditor, close, editing: () => !!editing};
  }

  window.KovaBots = {create};
})();

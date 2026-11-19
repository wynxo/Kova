/* Compare answers (#96): ask two to four models the same message and keep one.

   The server streams every model at once (POST /chats/{id}/compare) and holds
   the finished answers in memory; nothing reaches the chat until "Keep this
   one" (POST /chats/{id}/compare/keep) saves the message and that answer as a
   normal turn. app.js passes in what this module needs and stays the owner of
   the chat, the composer and the busy state.

   The pure helpers at the top (eligible, defaults, apply, stats) carry the
   logic and are what tests/web_compare.mjs checks without a browser. */
(() => {
  'use strict';

  const MIN = 2, MAX = 4;

  /* Which configured models can be compared, and why the others cannot. */
  function eligible(models, {localOnly = false} = {}) {
    return (models || []).filter(model => model && model.model).map(model => ({
      name: model.name,
      local: !!model.local,
      blocked: localOnly && !model.local ? 'Not on this computer, and Local only is on' : '',
    }));
  }

  /* Start from the model the composer shows, then fill up to two. */
  function defaults(choices, current) {
    const open = choices.filter(choice => !choice.blocked).map(choice => choice.name);
    const picked = open.includes(current) ? [current] : [];
    for (const name of open) { if (picked.length >= MIN) break; if (!picked.includes(name)) picked.push(name); }
    return picked;
  }

  /* Fold one stream event into the view state; returns the column it touched. */
  function apply(view, event) {
    const column = event.model ? view.columns[event.model] : null;
    switch (event.type) {
      case 'start':
        view.compareId = event.compare_id; view.phase = 'running';
        for (const name of event.models || []) view.columns[name] ||= blank(name);
        return null;
      case 'text':
        if (!column) return null;
        column.text += event.text || ''; column.phase = 'writing'; return column;
      case 'reasoning':
        if (!column) return null;
        if (column.phase === 'waiting') column.phase = 'thinking';
        return column;
      case 'model_done':
        if (!column) return null;
        column.phase = 'done'; column.usage = event.usage || null;
        if (typeof event.text === 'string') column.text = event.text;
        column.keepable = !!column.text.trim(); return column;
      case 'model_error':
        if (!column) return null;
        column.phase = 'error'; column.error = event.message || 'This model could not answer.'; return column;
      case 'done': case 'stopped':
        view.phase = event.type;
        for (const name of Object.keys(view.columns)) {
          const item = view.columns[name];
          if (!['done', 'error'].includes(item.phase)) item.phase = 'stopped';
          item.keepable = (event.kept || []).includes(name) && !!item.text.trim();
        }
        return null;
      case 'error':
        view.phase = 'error'; view.error = event.message || 'The comparison failed.';
        for (const item of Object.values(view.columns)) if (!['done', 'error'].includes(item.phase)) item.phase = 'stopped';
        return null;
      default:
        return null;
    }
  }

  function blank(name) {
    return {name, text: '', phase: 'waiting', usage: null, error: '', keepable: false};
  }

  function shortCount(count) {
    count = Number(count) || 0;
    if (count < 1000) return String(count);
    if (count < 10000) return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    return `${Math.round(count / 1000)}k`;
  }

  /* Speed, length and time, only from what the server measured. */
  function stats(usage) {
    if (!usage) return '';
    const parts = [];
    if (usage.tokens_per_second) parts.push(`${usage.tokens_per_second} tok/s`);
    if (usage.completion_tokens) parts.push(`${usage.estimated ? '~' : ''}${shortCount(usage.completion_tokens)} tokens`);
    if (usage.ms_total) parts.push(`${(usage.ms_total / 1000).toFixed(1)} s`);
    return parts.join(' · ');
  }

  const PHASES = {waiting: 'Waiting…', thinking: 'Thinking…', writing: 'Writing…', stopped: 'Stopped', error: 'Failed'};

  function create(deps) {
    const {json, api, write, readEvents, markdown, toastError, follow} = deps;
    let current = null;      // {view, root, columns: {name: {card, body, status, keep}}, chatId, finished}

    function node(tag, cls, text) {
      const result = document.createElement(tag);
      if (cls) result.className = cls;
      if (text !== undefined) result.textContent = text;
      return result;
    }
    function button(label, cls) {
      const result = node('button', cls, label); result.type = 'button'; return result;
    }

    /* --- choosing models -------------------------------------------------- */
    function choose() {
      const choices = eligible(deps.models(), {localOnly: deps.localOnly()});
      if (choices.filter(choice => !choice.blocked).length < MIN) {
        toastError(deps.localOnly()
          ? 'Compare needs two models on this computer while Local only is on. Add a local model in Model settings.'
          : 'Compare needs at least two models. Add another in Model settings.');
        return;
      }
      const dialog = node('dialog', 'compare-dialog');
      dialog.setAttribute('aria-labelledby', 'compare-title');
      const form = node('form'); form.method = 'dialog';
      const title = node('h2', '', 'Compare answers'); title.id = 'compare-title';
      const intro = node('p', 'compare-intro', `Ask ${MIN} to ${MAX} models the same message. Each answers without tools or memory, and nothing is saved until you keep one.`);
      const label = node('label', 'compare-text-label', 'Message');
      const text = node('textarea', 'compare-text'); text.rows = 3; text.maxLength = 100000;
      const draft = deps.draft();
      text.value = draft.trim(); label.append(text);
      const list = node('fieldset', 'compare-models');
      list.append(node('legend', '', 'Models'));
      const picked = new Set(defaults(choices, deps.currentModel()));
      const boxes = [];
      const count = node('p', 'compare-count'); count.setAttribute('aria-live', 'polite');
      for (const choice of choices) {
        const row = node('label', 'compare-model');
        const box = node('input'); box.type = 'checkbox'; box.value = choice.name;
        box.checked = picked.has(choice.name); box.disabled = !!choice.blocked;
        const name = node('span', 'compare-model-name', choice.name);
        const where = node('span', 'compare-model-note', choice.blocked || (choice.local ? 'On this computer' : 'Cloud'));
        row.append(box, name, where); list.append(row); boxes.push({box, choice});
      }
      const actions = node('div', 'dialog-actions');
      const cancel = button('Cancel', 'btn quiet'); cancel.value = 'cancel';
      const go = button('Compare', 'btn primary'); go.type = 'submit'; go.value = 'compare';
      actions.append(cancel, go);
      function sync() {
        const chosen = boxes.filter(item => item.box.checked).length;
        for (const item of boxes) item.box.disabled = !!item.choice.blocked || (!item.box.checked && chosen >= MAX);
        go.disabled = chosen < MIN || chosen > MAX || !text.value.trim();
        count.textContent = chosen < MIN ? `Choose at least ${MIN} models.` : `${chosen} of ${MAX} models chosen.`;
      }
      list.addEventListener('change', sync); text.addEventListener('input', sync);
      cancel.onclick = () => dialog.close('cancel');
      form.append(title, intro, label, list, count, actions); dialog.append(form);
      document.body.append(dialog);
      dialog.addEventListener('close', () => {
        const models = boxes.filter(item => item.box.checked).map(item => item.choice.name);
        const message = text.value.trim();
        dialog.remove();
        if (dialog.returnValue === 'compare' && models.length >= MIN && message) run(message, models, draft);
        else deps.restoreFocus?.();
      });
      sync();
      dialog.showModal();
      (text.value.trim() ? go : text).focus();
    }

    /* --- the side-by-side view -------------------------------------------- */
    function render(text, models) {
      const root = node('section', 'compare');
      root.setAttribute('aria-label', 'Compared answers');
      const asked = node('article', 'message user');
      asked.append(node('div', 'message-label', 'You'), node('div', 'message-body', text));
      const bar = node('div', 'compare-bar');
      const heading = node('h2', 'compare-heading', `Comparing ${models.length} models`);
      const stop = button('Stop', 'btn small compare-stop');
      const discard = button('Discard', 'btn small quiet compare-discard'); discard.hidden = true;
      bar.append(heading, stop, discard);
      const status = node('p', 'sr-only'); status.setAttribute('role', 'status');
      const grid = node('div', 'compare-grid');
      grid.tabIndex = 0;
      grid.setAttribute('role', 'group');
      grid.setAttribute('aria-label', 'Answers side by side. On a phone, swipe or use the arrow keys to see the next one.');
      const columns = {};
      models.forEach((name, index) => {
        const card = node('article', 'compare-card');
        card.setAttribute('aria-label', `${name}, answer ${index + 1} of ${models.length}`);
        const head = node('header', 'compare-card-head');
        const title = node('h3', 'compare-card-title', name);
        const place = node('span', 'compare-card-index', `${index + 1}/${models.length}`);
        place.setAttribute('aria-hidden', 'true');
        head.append(title, place);
        const state = node('p', 'compare-card-status', PHASES.waiting);
        const body = node('div', 'compare-card-body markdown');
        const keep = button('Keep this one', 'btn small compare-keep');
        keep.disabled = true; keep.setAttribute('aria-label', `Keep the answer from ${name}`);
        keep.onclick = () => keepOne(name);
        card.append(head, state, body, keep);
        grid.append(card);
        columns[name] = {card, body, status: state, keep};
      });
      root.append(asked, bar, status, grid);
      return {root, columns, stop, discard, heading, status};
    }

    function paint(column) {
      const parts = current?.parts.columns[column.name];
      if (!parts) return;
      parts.card.dataset.phase = column.phase;
      if (column.phase === 'done') parts.status.textContent = stats(column.usage) || 'Done';
      else if (column.phase === 'error') parts.status.textContent = column.error;
      else parts.status.textContent = PHASES[column.phase] || '';
      markdown(parts.body, column.text, column.phase !== 'writing');
      parts.keep.disabled = !column.keepable || current.keeping;
    }

    function announce(message) { if (current) current.parts.status.textContent = message; }

    function finish() {
      if (!current) return;
      const {view, parts} = current;
      parts.stop.hidden = true; parts.discard.hidden = false;
      const keepable = Object.values(view.columns).filter(column => column.keepable).length;
      parts.heading.textContent = keepable ? 'Choose the answer to keep' : 'No answer to keep';
      for (const column of Object.values(view.columns)) paint(column);
      announce(view.phase === 'stopped' ? 'Comparison stopped.' : keepable ? 'All answers are in.' : 'No model answered.');
    }

    async function run(text, models, draft = text) {
      if (deps.busy()) return;
      deps.setBusy(true);
      let chat;
      try {
        chat = await deps.ensureChat();
      } catch (error) { deps.setBusy(false); toastError(error.message); return; }
      dismiss();
      if (draft.trim() === text) deps.consumeDraft(chat, draft);
      const parts = render(text, models);
      const view = {compareId: null, phase: 'starting', columns: Object.fromEntries(models.map(name => [name, blank(name)]))};
      current = {view, parts, chatId: chat.id, keeping: false, finished: null};
      parts.stop.onclick = () => stopAll();
      parts.discard.onclick = () => { dismiss(); deps.restoreFocus?.(); };
      deps.mount(parts.root);
      announce(`Comparing ${models.length} models.`);
      follow();
      const mine = current;
      mine.finished = (async () => {
        try {
          const response = await api(`/chats/${chat.id}/compare`, write('POST', {text, models}));
          deps.setStreaming(true);
          await readEvents(response, event => {
            if (current !== mine) return;
            const column = apply(view, event);
            if (column) {
              paint(column);
              if (column.phase === 'done' || column.phase === 'error') announce(`${column.name} ${column.phase === 'done' ? 'finished' : 'failed'}.`);
              follow();
            }
          });
          if (!['done', 'stopped', 'error'].includes(view.phase)) apply(view, {type: 'error', message: 'The comparison was interrupted.'});
        } catch (error) {
          apply(view, {type: 'error', message: error.message});
          if (current === mine) toastError(error.message);
        } finally {
          deps.setBusy(false);
          if (current === mine) finish();
        }
      })();
      await mine.finished;
    }

    async function stopAll() {
      if (!current || current.view.phase !== 'running') return;
      current.parts.stop.disabled = true;
      try { await json(`/chats/${current.chatId}/stop`, {method: 'POST'}); }
      catch (error) { toastError(error.message); if (current) current.parts.stop.disabled = false; }
    }

    async function keepOne(name) {
      const mine = current;
      if (!mine || mine.keeping || !mine.view.compareId) return;
      mine.keeping = true;
      for (const column of Object.values(mine.view.columns)) paint(column);
      try {
        await json(`/chats/${mine.chatId}/compare/keep`, write('POST', {compare_id: mine.view.compareId, model: name}));
      } catch (error) {
        mine.keeping = false;
        for (const column of Object.values(mine.view.columns)) paint(column);
        toastError(error.message);
        return;
      }
      await mine.finished;   // keeping stops the others; let the stream end first
      if (current === mine) { current = null; mine.parts.root.remove(); }
      await deps.kept(mine.chatId, name);
    }

    /* Take the view away: a new message, another chat or Discard. */
    function dismiss() {
      if (!current) return;
      const old = current; current = null;
      old.parts.root.remove();
    }

    return {choose, run, dismiss, active: () => !!current};
  }

  window.KovaCompare = {create, eligible, defaults, apply, stats, MIN, MAX};
})();

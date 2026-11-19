/* Project sidebar sections, editor and chat moves. app.js still owns chat rows and navigation. */
(function (global) {
  'use strict';

  // The same colour and icon keys a chat uses (#213); chat-appearance.js holds
  // the canonical lists, and these copies keep projects working without it.
  const COLOURS = ['accent', 'success', 'warning', 'danger', 'info', 'keyword', 'string', 'function'];
  const ICONS = ['bell', 'brain', 'compass', 'folder', 'globe', 'lightbulb',
    'message-square', 'moon', 'puzzle', 'shield-check', 'square-terminal', 'sun'];
  const colours = () => global.KovaChatAppearance?.COLOURS || COLOURS;
  const icons = () => global.KovaChatAppearance?.ICONS || ICONS;
  const label = key => key.charAt(0).toUpperCase() + key.slice(1).replaceAll('-', ' ');

  function create({el, json, write, safe, refresh, newChat, models = () => []}) {
    let projects = [], loadFailed = false;
    const expanded = new Set(), rendered = new Map();

    async function load() {
      try { return await json('/projects'); }
      catch (_) { return null; }
    }

    function setProjects(rows) {
      loadFailed = !Array.isArray(rows);
      if (!loadFailed) projects = rows;
    }

    function open(id) {
      expanded.add(id);
      const group = rendered.get(id);
      if (group) group.open = true;
    }

    function button(text, action, classes = 'btn quiet') {
      const node = el('button', classes, text);
      node.type = 'button';
      node.onclick = action;
      return node;
    }

    function field(text, input) {
      const row = el('label', '', text);
      row.append(input);
      return row;
    }

    // A <select> with a "None"/default first option; the value '' means unset.
    function choice(name, none, keys, current, text = label) {
      const select = el('select');
      select.name = name;
      const empty = el('option', '', none); empty.value = '';
      select.append(empty);
      for (const key of keys) {
        const option = el('option', '', text(key));
        option.value = key;
        select.append(option);
      }
      select.value = current && keys.includes(current) ? current : '';
      return select;
    }

    function dialogShell(heading) {
      const dialog = el('dialog', 'project-dialog');
      const title = el('h2', '', heading);
      dialog.setAttribute('aria-label', heading);
      const form = el('form');
      const error = el('p', 'project-error');
      error.setAttribute('role', 'alert');
      error.hidden = true;
      return {dialog, title, form, error};
    }

    function show(dialog, busy, first) {
      dialog.addEventListener('cancel', event => { if (busy()) event.preventDefault(); });
      dialog.addEventListener('close', () => dialog.remove());
      document.body.append(dialog);
      dialog.showModal();
      first?.focus();
    }

    function edit(project) {
      const {dialog, title, form, error} = dialogShell(project ? 'Edit project' : 'New project');
      const name = el('input'), instructions = el('textarea'), cwd = el('input');
      name.name = 'name'; name.value = project?.name ?? ''; name.required = true; name.maxLength = 200;
      instructions.name = 'instructions'; instructions.value = project?.instructions ?? ''; instructions.maxLength = 16384;
      cwd.name = 'cwd'; cwd.value = project?.cwd ?? ''; cwd.autocomplete = 'off';
      cwd.placeholder = 'Absolute path on the computer running Kova';
      const configured = models().filter(entry => entry?.model).map(entry => entry.name);
      const current = project?.model ?? '';
      const modelKeys = current && !configured.includes(current) ? [...configured, current] : configured;
      const colour = choice('colour', 'No colour', colours(), project?.colour);
      const icon = choice('icon', 'No icon', icons(), project?.icon);
      const model = choice('model', 'The active model', modelKeys, current, key => key);
      const look = el('div', 'project-look');
      look.append(field('Colour', colour), field('Icon', icon));

      const actions = el('div', 'dialog-actions');
      const cancel = button('Cancel', () => dialog.close());
      const submit = el('button', 'btn primary', project ? 'Save' : 'Create project');
      submit.type = 'submit';
      actions.append(cancel, submit);

      form.onsubmit = async event => {
        event.preventDefault();
        if (submit.disabled) return;
        const values = {name: name.value.trim(), instructions: instructions.value, cwd: cwd.value.trim() || null};
        // Colour, icon and model are sent only when they change, so the server
        // keeps a default model whose entry is missing on this device.
        for (const [key, select] of [['colour', colour], ['icon', icon], ['model', model]]) {
          if ((select.value || '') !== (project?.[key] || '')) values[key] = select.value || null;
        }
        error.hidden = true;
        cancel.disabled = submit.disabled = true;
        let saved;
        try {
          const path = project ? `/projects/${encodeURIComponent(project.id)}` : '/projects';
          saved = await json(path, write(project ? 'PATCH' : 'POST', values));
        } catch (failure) {
          error.textContent = failure.message;
          error.hidden = false;
          return;
        } finally {
          cancel.disabled = submit.disabled = false;
        }
        open(saved.id);
        dialog.close();
        try {
          await refresh();
          rendered.get(saved.id)?.children[0].focus();
        } catch (failure) {
          safe(() => { throw failure; });
        }
      };

      form.append(title, field('Name', name), field('Instructions', instructions),
        field('Working folder (optional)', cwd), field('Default model', model), look, error, actions);
      dialog.append(form);
      show(dialog, () => submit.disabled, name);
    }

    // Move one saved chat into a project or out of all of them. Only
    // membership changes; the server keeps the chat's folder and model.
    function move(chat) {
      const chatTitle = chat.title || 'New conversation';
      const {dialog, title, form, error} = dialogShell(`Move "${chatTitle}"`);
      const options = el('fieldset', 'project-move-options');
      options.append(el('legend', '', 'Project'));
      const radios = [];
      for (const [value, text] of [['', 'No project'], ...projects.map(project => [project.id, project.name])]) {
        const input = el('input');
        input.type = 'radio'; input.name = 'project'; input.value = value;
        input.checked = (chat.project_id || '') === value;
        radios.push(input);
        const row = el('label', 'project-move-option');
        row.append(input, el('span', '', text));
        options.append(row);
      }
      const note = el('p', 'project-note', 'The chat keeps its working folder and model. Its next reply follows the new project’s instructions.');
      const actions = el('div', 'dialog-actions');
      const cancel = button('Cancel', () => dialog.close());
      const submit = el('button', 'btn primary', 'Move chat');
      submit.type = 'submit';
      actions.append(cancel, submit);

      form.onsubmit = async event => {
        event.preventDefault();
        if (submit.disabled) return;
        const target = radios.find(input => input.checked)?.value ?? '';
        if (target === (chat.project_id || '')) { dialog.close(); return; }
        error.hidden = true;
        cancel.disabled = submit.disabled = true;
        try {
          await json(`/chats/${encodeURIComponent(chat.id)}`, write('PATCH', {project_id: target || null}));
        } catch (failure) {
          error.textContent = failure.message;
          error.hidden = false;
          return;
        } finally {
          cancel.disabled = submit.disabled = false;
        }
        if (target) open(target);
        dialog.close();
        try { await refresh(); } catch (failure) { safe(() => { throw failure; }); }
      };
      form.append(title, options, note, error, actions);
      dialog.append(form);
      show(dialog, () => submit.disabled, radios.find(input => input.checked) || radios[0]);
    }

    // The section's leading mark: its icon tinted with its colour, or a dot.
    function mark(summary, project) {
      const colour = colours().includes(project.colour) ? project.colour : null;
      const symbol = icons().includes(project.icon) ? project.icon : null;
      if (!colour && !symbol) return;
      if (colour) summary.dataset.chatColour = colour;
      const node = el('span', symbol ? `chat-mark icon i-${symbol}` : 'chat-mark chat-dot');
      node.setAttribute('aria-hidden', 'true');
      summary.append(node);
    }

    // The project at a glance: folder, default model and the start of its
    // instructions. Only what is set is shown.
    function overview(project) {
      const facts = [
        ['Folder', project.cwd],
        ['Model', project.model],
        ['Instructions', (project.instructions || '').trim()],
      ].filter(([, value]) => value);
      if (!facts.length) return null;
      const list = el('dl', 'project-overview');
      for (const [term, value] of facts) {
        const row = el('div', `project-fact${term === 'Instructions' ? ' project-instructions' : ''}`);
        row.append(el('dt', '', term), el('dd', '', value));
        row.title = value;
        list.append(row);
      }
      return list;
    }

    function render(list, chats, {searching, disabled}) {
      // Keep disclosure state through chat refreshes and temporary search results.
      for (const [id, group] of rendered) {
        if (group.open) expanded.add(id);
        else expanded.delete(id);
      }
      rendered.clear();
      if (searching) return {sections: [], ungrouped: chats};

      const add = button('New project', () => edit(null), 'btn quiet project-action');
      add.disabled = disabled;
      list.append(add);
      if (loadFailed) {
        const retry = button('Retry', () => safe(refresh));
        const status = el('div', 'project-status', 'Could not load projects.');
        status.append(retry);
        list.append(status);
      }

      const members = new Map(projects.map(project => [project.id, []]));
      const ungrouped = [];
      for (const chat of chats) (members.get(chat.project_id) ?? ungrouped).push(chat);

      const sections = projects.map(project => {
        const group = el('details', 'project-group');
        group.open = expanded.has(project.id);
        const summary = el('summary', 'project-name');
        mark(summary, project);
        summary.append(el('span', 'project-title', project.name));
        group.append(summary);
        const body = el('div', 'project-body'), actions = el('div', 'project-actions');
        const start = button('New chat in project', () => safe(() => newChat(project)), 'btn quiet project-action');
        const settings = button('Edit project', () => edit(project), 'btn quiet project-action');
        start.disabled = settings.disabled = disabled;
        actions.append(start, settings);
        const facts = overview(project);
        if (facts) body.append(facts);
        body.append(actions); group.append(body); list.append(group);
        rendered.set(project.id, group);
        return {container: body, chats: members.get(project.id)};
      });
      return {sections, ungrouped};
    }

    const hasProjects = () => projects.length > 0;
    return {load, setProjects, render, open, move, hasProjects};
  }

  global.KovaProjects = {create};
})(globalThis);

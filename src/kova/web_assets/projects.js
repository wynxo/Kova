/* Project sidebar sections and editor. app.js still owns chat rows and navigation. */
(function (global) {
  'use strict';

  function create({el, json, write, safe, refresh, newChat}) {
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

    function button(label, action, classes = 'btn quiet') {
      const node = el('button', classes, label);
      node.type = 'button';
      node.onclick = action;
      return node;
    }

    function edit(project) {
      const dialog = el('dialog', 'project-dialog');
      const title = el('h2', '', project ? 'Edit project' : 'New project');
      dialog.setAttribute('aria-label', title.textContent);
      const form = el('form');
      const name = el('input'), instructions = el('textarea'), cwd = el('input');
      name.name = 'name'; name.value = project?.name ?? ''; name.required = true; name.maxLength = 200;
      instructions.name = 'instructions'; instructions.value = project?.instructions ?? ''; instructions.maxLength = 16384;
      cwd.name = 'cwd'; cwd.value = project?.cwd ?? ''; cwd.autocomplete = 'off';
      cwd.placeholder = 'Absolute path on the computer running Kova';

      function field(label, input) {
        const row = el('label', '', label);
        row.append(input);
        return row;
      }
      const error = el('p', 'project-error');
      error.setAttribute('role', 'alert');
      error.hidden = true;
      const actions = el('div', 'dialog-actions');
      const cancel = button('Cancel', () => dialog.close());
      const submit = el('button', 'btn primary', project ? 'Save' : 'Create project');
      submit.type = 'submit';
      actions.append(cancel, submit);

      form.onsubmit = async event => {
        event.preventDefault();
        if (submit.disabled) return;
        const values = {name: name.value.trim(), instructions: instructions.value, cwd: cwd.value.trim() || null};
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
        field('Working folder (optional)', cwd), error, actions);
      dialog.append(form);
      dialog.addEventListener('cancel', event => { if (submit.disabled) event.preventDefault(); });
      dialog.addEventListener('close', () => dialog.remove());
      document.body.append(dialog);
      dialog.showModal();
      name.focus();
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
        group.append(el('summary', 'project-name', project.name));
        const body = el('div', 'project-body'), actions = el('div', 'project-actions');
        const start = button('New chat in project', () => safe(() => newChat(project)), 'btn quiet project-action');
        const settings = button('Edit project', () => edit(project), 'btn quiet project-action');
        start.disabled = settings.disabled = disabled;
        actions.append(start, settings);
        body.append(actions); group.append(body); list.append(group);
        rendered.set(project.id, group);
        return {container: body, chats: members.get(project.id)};
      });
      return {sections, ungrouped};
    }

    return {load, setProjects, render, open};
  }

  global.KovaProjects = {create};
})(globalThis);

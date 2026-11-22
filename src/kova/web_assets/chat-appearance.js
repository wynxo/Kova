/* Per-chat colour and icon (issue #213). Dependency-free: app.js calls in,
 * so the sidebar keeps working when this file is missing. Colours are keys
 * into the server's palette (theme tokens, never hex); icons are vendored
 * .i-<name> classes from app.css. */
(() => {
  'use strict';
  const COLOURS = ['accent', 'success', 'warning', 'danger', 'info', 'keyword', 'string', 'function'];
  const ICONS = ['bell', 'brain', 'compass', 'folder', 'globe', 'lightbulb',
    'message-square', 'moon', 'puzzle', 'shield-check', 'square-terminal', 'sun'];

  function appearanceOf(chat) {
    const colour = typeof chat?.colour === 'string' && COLOURS.includes(chat.colour) ? chat.colour : null;
    const icon = typeof chat?.icon === 'string' && ICONS.includes(chat.icon) ? chat.icon : null;
    return {colour, icon};
  }

  // The top bar carries the same colour as one thin rule under the title, so a
  // chat is recognisable without adding another filled surface. The rule is
  // the same semantic token the row uses; a theme recolours both.
  function decorateTopbar(chat) {
    if (typeof document === 'undefined') return;
    const bar = document.querySelector?.('.topbar');
    if (!bar) return;
    const {colour} = appearanceOf(chat);
    if (colour) bar.dataset.chatColour = colour;
    else delete bar.dataset.chatColour;
  }

  // Draw the row's leading mark: the chosen icon tinted with the chosen
  // colour, or a plain dot when only a colour is set.
  function decorateRow(row, chat) {
    if (!row || typeof document === 'undefined') return;
    row.querySelectorAll(':scope > .chat-mark').forEach(node => node.remove());
    delete row.dataset.chatColour;
    const {colour, icon} = appearanceOf(chat);
    if (!colour && !icon) return;
    if (colour) row.dataset.chatColour = colour;
    const mark = document.createElement('span');
    mark.className = icon ? `chat-mark icon i-${icon}` : 'chat-mark chat-dot';
    mark.setAttribute('aria-hidden', 'true');
    row.prepend(mark);
  }

  // A small picker dialog. Resolves to a PATCH body ({colour, icon} with ""
  // clearing a field) or null when cancelled.
  function openDialog(chat) {
    const {colour, icon} = appearanceOf(chat);
    let pickedColour = colour || '';
    let pickedIcon = icon || '';
    const dialog = document.createElement('dialog');
    dialog.className = 'appearance-dialog';
    dialog.setAttribute('aria-labelledby', 'appearance-title');
    const title = document.createElement('h2');
    title.id = 'appearance-title';
    title.textContent = 'Chat colour & icon';
    dialog.append(title);

    const swatches = document.createElement('div');
    swatches.className = 'appearance-swatches';
    swatches.setAttribute('role', 'group');
    swatches.setAttribute('aria-label', 'Colour');
    const paintSwatches = () => {
      swatches.replaceChildren();
      const none = document.createElement('button');
      none.type = 'button';
      none.className = 'appearance-swatch';
      none.textContent = 'Default';
      none.setAttribute('aria-pressed', String(pickedColour === ''));
      none.onclick = () => { pickedColour = ''; paintSwatches(); };
      swatches.append(none);
      for (const name of COLOURS) {
        const swatch = document.createElement('button');
        swatch.type = 'button';
        swatch.className = 'appearance-swatch';
        swatch.title = name;
        swatch.setAttribute('aria-label', `Colour ${name}`);
        swatch.setAttribute('aria-pressed', String(pickedColour === name));
        const dot = document.createElement('span');
        dot.className = 'dot';
        dot.setAttribute('aria-hidden', 'true');
        dot.style.background = {
          accent: 'var(--accent-default)', success: 'var(--status-success)',
          warning: 'var(--status-warning)', danger: 'var(--status-danger)',
          info: 'var(--status-info)', keyword: 'var(--syntax-keyword)',
          string: 'var(--syntax-string)', function: 'var(--syntax-function)',
        }[name];
        swatch.append(dot);
        swatch.onclick = () => { pickedColour = name; paintSwatches(); };
        swatches.append(swatch);
      }
    };
    paintSwatches();
    dialog.append(swatches);

    const icons = document.createElement('div');
    icons.className = 'appearance-icons';
    icons.setAttribute('role', 'group');
    icons.setAttribute('aria-label', 'Icon');
    const paintIcons = () => {
      icons.replaceChildren();
      const none = document.createElement('button');
      none.type = 'button';
      none.className = 'appearance-icon';
      none.textContent = 'None';
      none.setAttribute('aria-pressed', String(pickedIcon === ''));
      none.onclick = () => { pickedIcon = ''; paintIcons(); };
      icons.append(none);
      for (const name of ICONS) {
        const glyph = document.createElement('button');
        glyph.type = 'button';
        glyph.className = 'appearance-icon';
        glyph.title = name;
        glyph.setAttribute('aria-label', `Icon ${name}`);
        glyph.setAttribute('aria-pressed', String(pickedIcon === name));
        const mark = document.createElement('span');
        mark.className = `icon i-${name}`;
        mark.setAttribute('aria-hidden', 'true');
        glyph.append(mark);
        glyph.onclick = () => { pickedIcon = name; paintIcons(); };
        icons.append(glyph);
      }
    };
    paintIcons();
    dialog.append(icons);

    const actions = document.createElement('div');
    actions.className = 'dialog-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn quiet';
    cancel.textContent = 'Cancel';
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn primary';
    save.textContent = 'Save';
    actions.append(cancel, save);
    dialog.append(actions);
    document.body.append(dialog);

    return new Promise(resolve => {
      const done = value => { dialog.remove(); resolve(value); };
      cancel.onclick = () => done(null);
      dialog.addEventListener('cancel', () => done(null));
      save.onclick = () => done({colour: pickedColour, icon: pickedIcon});
      dialog.showModal();
      (dialog.querySelector('[aria-pressed="true"]') || save).focus();
    });
  }

  window.KovaChatAppearance = {COLOURS, ICONS, appearanceOf, decorateRow, decorateTopbar, openDialog};
})();

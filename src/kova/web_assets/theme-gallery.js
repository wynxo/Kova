/* The bundled gallery previews on the real app; only Use changes preferences. */
(() => {
  'use strict';

  let gallery;

  function element(tag, className = '', text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className = 'btn') {
    const node = element('button', className, label);
    node.type = 'button';
    return node;
  }

  async function open(opener) {
    if (gallery) return;
    const theme = window.KovaTheme;
    const dialog = element('dialog', 'theme-gallery');
    const title = element('h2', '', 'Theme gallery');
    title.id = 'theme-gallery-title';
    dialog.setAttribute('aria-labelledby', title.id);
    const done = button('Close');
    const header = element('header', 'theme-gallery-header');
    header.append(title, done);
    const status = element('p', 'theme-gallery-status', 'Loading themes…');
    status.setAttribute('role', 'status');
    const grid = element('ul', 'theme-gallery-grid');
    grid.setAttribute('role', 'list');
    dialog.append(header, status, grid);

    let hovered;
    let focused;
    let pinned;
    let shown;

    function endPreview() {
      if (!shown) return;
      if (shown.installed) theme.endPreview();
      else theme.clearPalettePreview();
      shown = null;
    }

    function syncPreview() {
      if (!dialog.open) return;
      const next = (hovered || focused || pinned)?.entry;
      if (next === shown?.entry && next?.installed === shown?.installed) return;
      endPreview();
      if (!next) return;
      shown = {entry: next, installed: next.installed};
      if (next.installed) theme.preview(next.id);
      else theme.previewPalette(next);
    }

    function close() {
      endPreview();
      dialog.close();
    }

    function addCard(entry) {
      const card = element('li', 'theme-gallery-card');
      const heading = element('div', 'theme-gallery-heading');
      heading.append(element('h3', '', entry.name), element('span', '', entry.appearance === 'light' ? 'Light' : 'Dark'));
      card.append(heading);
      // A live preview card from the theme's resolved tokens (#175), not the
      // four-colour swatch strip: surfaces, text, an accent chip and status dots.
      if (window.KovaThemePreview) {
        card.append(window.KovaThemePreview.card(entry.tokens));
      } else {
        const swatches = element('div', 'theme-gallery-swatches');
        swatches.setAttribute('aria-hidden', 'true');
        for (const colour of entry.swatches) {
          const sample = element('span');
          sample.style.backgroundColor = colour;
          swatches.append(sample);
        }
        card.append(swatches);
      }
      if (entry.description) card.append(element('p', '', entry.description));

      const preview = button('Preview');
      preview.setAttribute('aria-label', `Preview ${entry.name}`);
      preview.setAttribute('aria-pressed', 'false');
      const action = button('');
      const actions = element('div', 'theme-gallery-actions');
      actions.append(preview, action);
      card.append(actions);
      grid.append(card);

      const target = {entry, preview};
      let installing = false;
      function updateAction() {
        const label = entry.installed ? 'Use' : 'Install';
        action.textContent = label;
        action.setAttribute('aria-label', `${label} ${entry.name}`);
      }
      updateAction();

      card.addEventListener('pointerenter', event => {
        if (event.pointerType === 'touch') return;
        hovered = target;
        syncPreview();
      });
      card.addEventListener('pointerleave', () => {
        if (hovered === target) hovered = null;
        syncPreview();
      });
      card.addEventListener('focusin', event => {
        if (!event.target.matches(':focus-visible')) return;
        // Tabbing previews the focused card even if the pointer is parked on another.
        hovered = null;
        focused = target;
        syncPreview();
      });
      card.addEventListener('focusout', event => {
        if (focused === target && !card.contains(event.relatedTarget)) focused = null;
        syncPreview();
      });
      preview.addEventListener('click', () => {
        pinned?.preview.setAttribute('aria-pressed', 'false');
        pinned = pinned === target ? null : target;
        pinned?.preview.setAttribute('aria-pressed', 'true');
        syncPreview();
      });
      action.addEventListener('click', async () => {
        if (installing) return;
        if (entry.installed) {
          close();
          theme.set(entry.id);
          return;
        }

        installing = true;
        action.setAttribute('aria-disabled', 'true');
        status.textContent = `Installing ${entry.name}…`;
        try {
          const response = await fetch(`/themes/gallery/${encodeURIComponent(entry.id)}/install`, {method: 'POST'});
          if (!response.ok) {
            const {detail} = await response.json();
            throw new Error(Array.isArray(detail) ? detail.join(' ') : detail);
          }
          entry.installed = true;
          await theme.refresh();
          if (!dialog.open) return;
          updateAction();
          syncPreview();
          status.textContent = `Installed ${entry.name}.`;
        } catch (error) {
          if (dialog.open) status.textContent = `Could not install ${entry.name}. ${error.message}`;
        } finally {
          installing = false;
          action.removeAttribute('aria-disabled');
        }
      });
    }

    done.addEventListener('click', close);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    dialog.addEventListener('close', () => {
      endPreview();
      dialog.remove();
      gallery = null;
      opener.focus();
    });
    gallery = dialog;
    document.body.append(dialog);
    dialog.showModal();
    done.focus();

    try {
      const response = await fetch('/themes/gallery');
      if (!response.ok) throw new Error();
      const entries = await response.json();
      if (!dialog.open) return;
      for (const entry of entries) addCard(entry);
      status.textContent = entries.length ? '' : 'No themes in the gallery.';
    } catch (_) {
      if (dialog.open) status.textContent = 'Could not load the theme gallery. Close it and try again.';
    }
  }

  window.KovaThemeGallery = {open};
})();

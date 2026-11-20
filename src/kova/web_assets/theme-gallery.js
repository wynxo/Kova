/* The theme gallery previews on the real app; only Use changes preferences.

   Besides browsing it can search, Try a theme for 30 seconds (it reverts unless
   kept), install, update an installed theme to a newer gallery copy and remove
   an installed theme file. Everything it shows is data from /themes/gallery. */
(() => {
  'use strict';

  const TRY_SECONDS = 30;
  let gallery;
  let dismissGallery;
  let trial;   // the theme being tried: {entry, banner, timer, ticker, ...}

  async function failure(response) {
    try {
      const {detail} = await response.json();
      return Array.isArray(detail) ? detail.join(' ') : String(detail || response.statusText || 'Request failed.');
    } catch (_) {
      return response.statusText || 'Request failed.';
    }
  }

  async function send(url, method) {
    const response = await fetch(url, {method});
    if (!response.ok) throw new Error(await failure(response));
    return response;
  }

  const route = (entry, verb) => `/themes/gallery/${encodeURIComponent(entry.id)}/${verb}`;

  async function installEntry(entry) {
    await send(route(entry, 'install'), 'POST');
    entry.installed = true;
    entry.update = false;
    await window.KovaTheme.refresh();
  }

  /** Whether a card matches the search: every word in its name, id, author, description or look. */
  function matches(entry, query) {
    const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return true;
    const text = [entry.id, entry.name, entry.author, entry.description, entry.appearance]
      .filter(Boolean).join(' ').toLowerCase();
    return words.every(word => text.includes(word));
  }

  function endTrial({keep = false} = {}) {
    if (!trial) return;
    const current = trial;
    trial = null;
    clearTimeout(current.timer);
    clearInterval(current.ticker);
    current.banner.remove();
    if (!keep) {
      if (current.installed) window.KovaTheme.endPreview();
      else window.KovaTheme.clearPalettePreview();
    }
    current.opener?.focus?.();
  }

  /* Show a theme for TRY_SECONDS on the real app. Nothing is saved: Keep installs
     it if needed and chooses it, anything else puts the previous theme back. */
  function startTrial(entry, opener) {
    endTrial();
    const theme = window.KovaTheme;
    const installed = !!entry.installed;
    if (installed) theme.preview(entry.id);
    else theme.previewPalette(entry);

    const banner = element('div', 'theme-try');
    banner.setAttribute('role', 'group');
    banner.setAttribute('aria-label', `Trying ${entry.name}`);
    const note = element('span', 'theme-try-note', `Trying ${entry.name}. It reverts unless you keep it.`);
    note.setAttribute('role', 'status');
    const left = element('span', 'theme-try-left', `${TRY_SECONDS} s`);
    left.setAttribute('aria-hidden', 'true');
    const keep = button('Keep', 'btn primary');
    keep.setAttribute('aria-label', `Keep ${entry.name}`);
    const revert = button('Revert', 'btn quiet');
    revert.setAttribute('aria-label', `Revert from ${entry.name}`);
    banner.append(note, left, keep, revert);

    const current = {entry, banner, installed, opener, remaining: TRY_SECONDS};
    current.timer = setTimeout(() => endTrial(), TRY_SECONDS * 1000);
    current.ticker = setInterval(() => {
      current.remaining = Math.max(0, current.remaining - 1);
      left.textContent = `${current.remaining} s`;
    }, 1000);
    trial = current;

    let keeping = false;
    keep.addEventListener('click', async () => {
      if (keeping || trial !== current) return;
      keeping = true;
      keep.setAttribute('aria-disabled', 'true');
      clearTimeout(current.timer);  // a slow install must not revert mid-way
      clearInterval(current.ticker);
      try {
        if (!entry.installed) await installEntry(entry);
        if (trial !== current) return;
        endTrial({keep: true});
        theme.set(entry.id);
      } catch (error) {
        if (trial !== current) return;
        note.textContent = `Could not keep ${entry.name}. ${error.message}`;
        keeping = false;
        keep.removeAttribute('aria-disabled');
        current.timer = setTimeout(() => endTrial(), TRY_SECONDS * 1000);
      }
    });
    revert.addEventListener('click', () => endTrial());
    banner.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); endTrial(); }
    });
    document.body.append(banner);
    keep.focus();
  }

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

  async function open(opener, {mount, closeHost} = {}) {
    if (gallery) return;
    const theme = window.KovaTheme;
    const dialog = element(mount ? 'section' : 'dialog', 'theme-gallery');
    const isOpen = () => mount ? dialog.isConnected : dialog.open;
    let done;
    if (!mount) {
      const title = element('h2', '', 'Theme gallery');
      title.id = 'theme-gallery-title';
      dialog.setAttribute('aria-labelledby', title.id);
      done = button('Close');
      const header = element('header', 'theme-gallery-header');
      header.append(title, done);
      dialog.append(header);
    }
    const search = element('input', 'theme-gallery-search');
    search.type = 'search';
    search.placeholder = 'Search themes';
    search.setAttribute('aria-label', 'Search themes');
    search.autocomplete = 'off';
    const status = element('p', 'theme-gallery-status', 'Loading themes…');
    status.setAttribute('role', 'status');
    const grid = element('ul', 'theme-gallery-grid');
    grid.setAttribute('role', 'list');
    dialog.append(search, status, grid);
    const cards = [];

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
      if (!isOpen()) return;
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
      if (mount) {
        dismissGallery();
        closeHost();
      } else {
        dialog.close();
      }
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
      const meta = element('p', 'theme-gallery-meta');
      card.append(meta);

      const preview = button('Preview');
      preview.setAttribute('aria-label', `Preview ${entry.name}`);
      preview.setAttribute('aria-pressed', 'false');
      const tryButton = button('Try');
      tryButton.setAttribute('aria-label', `Try ${entry.name} for ${TRY_SECONDS} seconds`);
      const action = button('');
      const update = button('Update', 'btn');
      const remove = button('Remove', 'btn quiet');
      const actions = element('div', 'theme-gallery-actions');
      actions.append(preview, tryButton, update, action, remove);
      card.append(actions);
      grid.append(card);
      cards.push({entry, card});

      const target = {entry, preview};
      let installing = false;
      let armed = null;   // a pending "Remove?" confirmation timer
      function disarm() {
        clearTimeout(armed);
        armed = null;
        remove.textContent = 'Remove';
        remove.setAttribute('aria-label', `Remove ${entry.name}`);
      }
      function updateAction() {
        const label = entry.installed ? 'Use' : 'Install';
        action.textContent = label;
        action.setAttribute('aria-label', `${label} ${entry.name}`);
        update.hidden = !(entry.installed && entry.update);
        update.setAttribute('aria-label', `Update ${entry.name} to version ${entry.version}`);
        remove.hidden = !entry.installed;
        disarm();
        const parts = [entry.author ? `By ${entry.author}` : '', entry.version ? `Version ${entry.version}` : '',
          entry.installed && entry.update ? `Installed ${entry.installed_version || 'older copy'}` : ''];
        meta.textContent = parts.filter(Boolean).join(' · ');
        meta.hidden = !meta.textContent;
      }
      updateAction();
      card.updateAction = updateAction;

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
      tryButton.addEventListener('click', () => {
        endPreview();       // forget the hover preview first, so the trial is the only change
        pinned = hovered = focused = null;
        close();
        startTrial(entry, opener);
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
          await installEntry(entry);
          if (!isOpen()) return;
          updateAction();
          syncPreview();
          status.textContent = `Installed ${entry.name}.`;
        } catch (error) {
          if (isOpen()) status.textContent = `Could not install ${entry.name}. ${error.message}`;
        } finally {
          installing = false;
          action.removeAttribute('aria-disabled');
        }
      });
      update.addEventListener('click', async () => {
        if (installing) return;
        installing = true;
        update.setAttribute('aria-disabled', 'true');
        status.textContent = `Updating ${entry.name}…`;
        try {
          await send(route(entry, 'update'), 'POST');
          entry.update = false;
          entry.installed_version = entry.version;
          await theme.refresh();
          // The chosen theme repaints from the new file; a preview shows it too.
          if (theme.choice() === entry.id) await theme.set(entry.id);
          if (!isOpen()) return;
          updateAction();
          endPreview();
          syncPreview();
          status.textContent = `Updated ${entry.name} to version ${entry.version}.`;
        } catch (error) {
          if (isOpen()) status.textContent = `Could not update ${entry.name}. ${error.message}`;
        } finally {
          installing = false;
          update.removeAttribute('aria-disabled');
        }
      });
      remove.addEventListener('focusout', event => {
        if (event.relatedTarget !== remove) disarm();
      });
      remove.addEventListener('click', async () => {
        if (installing) return;
        if (!armed) {
          // Naming what is destroyed, then asking once more, in place.
          remove.textContent = 'Confirm remove';
          remove.setAttribute('aria-label', `Confirm: remove ${entry.name}`);
          status.textContent = `Select Confirm remove to delete ${entry.name}. It stays in the gallery to install again.`;
          armed = setTimeout(disarm, 6000);
          return;
        }
        installing = true;
        disarm();
        remove.setAttribute('aria-disabled', 'true');
        try {
          await send(`/themes/${encodeURIComponent(entry.id)}`, 'DELETE');
          const wasChosen = theme.choice() === entry.id;
          entry.installed = false;
          entry.update = false;
          endPreview();
          pinned?.entry === entry && (pinned = null);
          if (wasChosen) await theme.set('system');   // never leave a deleted theme chosen
          await theme.refresh();
          if (!isOpen()) return;
          preview.setAttribute('aria-pressed', 'false');
          updateAction();
          status.textContent = `Removed ${entry.name}.`;
        } catch (error) {
          if (isOpen()) status.textContent = `Could not remove ${entry.name}. ${error.message}`;
        } finally {
          installing = false;
          remove.removeAttribute('aria-disabled');
        }
      });
    }

    dismissGallery = () => {
      endPreview();
      dialog.remove();
      gallery = null;
      dismissGallery = null;
      if (!mount) opener?.focus?.();
    };
    gallery = dialog;
    if (mount) {
      mount.append(dialog);
    } else {
      done.addEventListener('click', close);
      dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
      dialog.addEventListener('close', dismissGallery, {once: true});
      document.body.append(dialog);
      dialog.showModal();
      done.focus();
    }

    try {
      const response = await fetch('/themes/gallery');
      if (!response.ok) throw new Error();
      const entries = await response.json();
      if (!isOpen()) return;
      for (const entry of entries) addCard(entry);
      status.textContent = entries.length ? '' : 'No themes in the gallery.';
      search.addEventListener('input', () => {
        let shown = 0;
        for (const item of cards) {
          const visible = matches(item.entry, search.value);
          item.card.hidden = !visible;
          if (visible) shown += 1;
        }
        status.textContent = !entries.length ? 'No themes in the gallery.'
          : shown ? (search.value.trim() ? `${shown} of ${entries.length} themes` : '')
            : `No theme matches "${search.value.trim()}".`;
      });
    } catch (_) {
      if (isOpen()) status.textContent = 'Could not load the theme gallery. Close it and try again.';
    }
  }

  window.KovaThemeGallery = {open, matches, TRY_SECONDS, dismissEmbedded: () => {
    if (gallery?.tagName === 'SECTION') dismissGallery();
  }};
})();

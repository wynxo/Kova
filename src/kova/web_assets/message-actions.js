/* Message actions (#188): copy, retry, edit and details under a message.

   app.js owns the turn and its stream; this module only draws the quiet bar
   and reports intent through callbacks. Copy needs no server round trip, and
   details read only the usage the server already sent with the reply, so a
   missing field is left out instead of guessed. */
(() => {
  'use strict';

  const RESET = 1600;

  function node(tag, cls, text) {
    const result = document.createElement(tag);
    result.className = cls;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function icon(name) {
    const mark = node('span', `icon i-${name}`);
    mark.setAttribute('aria-hidden', 'true');
    return mark;
  }

  function action(name, label, cls, onClick) {
    const button = node('button', `${cls} message-action`);
    button.type = 'button';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.append(icon(name));
    if (onClick) button.onclick = onClick;
    return button;
  }

  function shortTokens(count) {
    count = Number(count) || 0;
    if (count < 1000) return String(count);
    if (count < 10000) return `${(count / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    if (count < 1e6) return `${Math.round(count / 1000)}k`;
    return `${(count / 1e6).toFixed(1)}M`;
  }

  function seconds(ms) {
    return `${(Math.max(0, Number(ms) || 0) / 1000).toFixed(1)} s`;
  }

  function money(value) {
    return value < 0.01 ? `$${value.toFixed(4).replace(/0+$/, '')}` : `$${value.toFixed(2)}`;
  }

  // Only fields that were actually sent become rows; nothing is inferred.
  function detailRows({usage, model, toolCount}) {
    const u = usage || {};
    const rows = [];
    const named = u.model || model;
    if (named) rows.push(['Model', named]);
    if (u.prompt_tokens != null || u.completion_tokens != null) {
      const mark = u.estimated ? '~' : '';
      rows.push(['Tokens', `${mark}${shortTokens(u.prompt_tokens)} in · ${mark}${shortTokens(u.completion_tokens)} out`]);
    }
    if (u.tokens_per_second) rows.push(['Speed', `${u.tokens_per_second} tok/s`]);
    const times = [];
    if (u.ms_first_token != null) times.push(`first token ${seconds(u.ms_first_token)}`);
    if (u.ms_total != null) times.push(`${seconds(u.ms_total)} total`);
    if (times.length) rows.push(['Time', times.join(' · ')]);
    if (Number.isFinite(toolCount)) rows.push(['Tools', `${toolCount}`]);
    if (!u.local && u.cost_usd != null) rows.push(['Cost', `~${money(u.cost_usd)}`]);
    if (u.estimated) rows.push(['Note', 'Token counts are estimated: the server sent none.']);
    return rows;
  }

  function detailsAction(info) {
    const rows = detailRows(info);
    if (!rows.length) return null;
    const card = node('details', 'message-details');
    const summary = node('summary', 'message-details-toggle');
    summary.append(icon('info'), document.createTextNode('Details'));
    const list = node('dl', 'message-details-list');
    for (const [label, value] of rows) {
      list.append(node('dt', '', label), node('dd', '', value));
    }
    card.append(summary, list);
    return card;
  }

  function copyAction(text, note, onError) {
    const glyph = icon('copy');
    const button = node('button', 'message-action-copy message-action');
    button.type = 'button';
    button.title = 'Copy as Markdown';
    button.setAttribute('aria-label', 'Copy as Markdown');
    button.append(glyph);
    let timer = null;
    button.onclick = async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(typeof text === 'function' ? text() : text);
      } catch (_) {
        onError?.('Clipboard unavailable. Select the text to copy it.');
        return;
      }
      glyph.className = 'icon i-check';
      button.title = 'Copied';
      button.setAttribute('aria-label', 'Copied');
      note.textContent = 'Copied';
      clearTimeout(timer);
      timer = setTimeout(() => {
        glyph.className = 'icon i-copy';
        button.title = 'Copy as Markdown';
        button.setAttribute('aria-label', 'Copy as Markdown');
        note.textContent = '';
      }, RESET);
    };
    return button;
  }

  // One bar per message. Callbacks are optional: an older cached shell, a
  // message with no usage, or a turn that cannot be retried simply shows less.
  function bar(options = {}) {
    const {role, text, usage = null, model = '', toolCount = null, onRetry, onEdit,
           editLabel = 'Edit and resend', onError} = options;
    const bar = node('div', 'message-actions');
    const note = node('span', 'message-action-note');
    note.setAttribute('role', 'status');
    bar.append(copyAction(text, note, onError));
    if (role === 'user' && onEdit) bar.append(action('pencil', editLabel, 'message-action-edit', onEdit));
    if (role === 'assistant' && onRetry) bar.append(action('refresh', 'Retry this reply', 'message-action-retry', onRetry));
    if (role === 'assistant') {
      const details = detailsAction({usage, model, toolCount});
      if (details) bar.append(details);
    }
    bar.append(note);
    return bar;
  }

  // A details card closes when attention leaves it, like any other popover,
  // and Escape returns focus to its summary.
  if (typeof document !== 'undefined' && !globalThis.__kovaMessageDetails) {
    globalThis.__kovaMessageDetails = true;
    document.addEventListener('click', event => {
      for (const open of document.querySelectorAll('details.message-details[open]')) {
        if (!open.contains(event.target)) open.open = false;
      }
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      for (const open of document.querySelectorAll('details.message-details[open]')) {
        open.open = false;
        open.querySelector('summary')?.focus();
      }
    });
  }

  globalThis.KovaMessageActions = {bar, detailRows};
})();

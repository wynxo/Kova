/* Pinned plan card (#193).

   While a turn runs and its latest plan still has open steps, one compact row
   sits just above the message box: "Step 2 of 5 · Writing the summary", a
   progress bar and a disclosure for the whole checklist. The same `plan`
   events that draw the transcript feed it, so it never invents progress.

   At the end of the turn the pinned card gives the plan back to the
   transcript — one plan on screen, never two. Dismissing hides the card for
   this turn only and lets the transcript plan stand in meanwhile; the next
   turn starts with the card available again. Step changes are announced once,
   never per streamed token. */
const KovaPlanCard = (() => {
  'use strict';
  let card = null, slot = null, toggle = null, countNode = null, stepNode = null;
  let bar = null, fill = null, list = null, status = null;
  let items = [], transcript = null, dismissed = false, open = false, announced = '';

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function icon(name) {
    const node = el('span', `icon i-${name}`);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }
  function button(cls, label) {
    const node = el('button', cls);
    node.type = 'button';
    node.setAttribute('aria-label', label);
    node.title = label;
    return node;
  }

  // The wrapper sits inside .compose-wrap, immediately above the form, so the
  // card scrolls with the composer on a phone keyboard instead of floating.
  function mount() {
    if (card) return card;
    const composer = document.getElementById('composer');
    const wrap = document.querySelector('.compose-wrap') || composer?.parentNode;
    if (!wrap) return null;
    slot = el('div', 'plan-card-slot');
    slot.id = 'plan-card-slot';
    card = el('section', 'plan-card');
    card.hidden = true;
    card.setAttribute('aria-label', 'Plan progress');

    const head = el('div', 'plan-card-head');
    toggle = button('plan-card-toggle', 'Show all plan steps');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', 'plan-card-list');
    countNode = el('span', 'plan-card-count');
    stepNode = el('span', 'plan-card-step');
    toggle.append(countNode, el('span', 'plan-card-separator', '·'), stepNode,
                  icon('chevron-down'));
    toggle.onclick = () => setOpen(!open);
    const dismiss = button('plan-card-dismiss', 'Hide the plan for this turn');
    dismiss.append(icon('x'));
    dismiss.onclick = () => hide();
    head.append(toggle, dismiss);

    bar = el('div', 'plan-card-bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', 'Plan progress');
    fill = el('span', 'plan-card-fill');
    bar.append(fill);

    list = el('div', 'plan-card-list');
    list.id = 'plan-card-list';
    list.hidden = true;
    status = el('span', 'sr-only');
    status.setAttribute('role', 'status');

    card.append(head, bar, list, status);
    card.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !open) return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      toggle.focus();
    });
    slot.append(card);
    wrap.insertBefore(slot, composer || null);
    return card;
  }

  function label() {
    const verb = open ? 'Hide' : 'Show';
    return `${verb} all plan steps. ${countNode.textContent}: ${stepNode.textContent}`;
  }

  function setOpen(next) {
    open = next;
    list.hidden = !next;
    toggle.setAttribute('aria-expanded', String(next));
    if (countNode && stepNode) toggle.setAttribute('aria-label', label());
    card.classList.toggle('is-open', next);
  }

  function showTranscript() { if (transcript) transcript.hidden = false; }
  function hideTranscript() { if (transcript) transcript.hidden = true; }

  // The card can disappear under a keyboard reader (dismiss, or the turn
  // ending); focus goes to the message box instead of the page root.
  function releaseFocus() {
    const active = typeof document !== 'undefined' ? document.activeElement : null;
    if (card && active && card.contains?.(active)) {
      document.getElementById('prompt')?.focus?.({preventScroll: true});
    }
  }

  function hide() {
    releaseFocus();
    if (card) { setOpen(false); card.hidden = true; }
    dismissed = true;
    showTranscript();
  }

  function currentStep() {
    let index = items.findIndex(item => item.status === 'in_progress');
    if (index < 0) index = items.findIndex(item => item.status !== 'done');
    if (index < 0) index = items.length - 1;
    return {index, item: items[index] || null};
  }

  function row(item) {
    const status = ['done', 'in_progress', 'pending'].includes(item.status) ? item.status : 'pending';
    const line = el('li', `plan-${status}`);
    const mark = el('span', 'plan-mark');
    mark.setAttribute('role', 'img');
    mark.setAttribute('aria-label', status.replace('_', ' '));
    if (status === 'done') mark.append(icon('check'));
    line.append(mark, el('span', '', String(item.text ?? '')));
    return line;
  }

  function paint() {
    const total = items.length;
    const done = items.filter(item => item.status === 'done').length;
    const {index, item} = currentStep();
    const text = item ? String(item.text ?? '') : 'Plan complete';
    countNode.textContent = `Step ${Math.min(index + 1, total)} of ${total}`;
    stepNode.textContent = text;
    toggle.setAttribute('aria-label', label());
    fill.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', String(total));
    bar.setAttribute('aria-valuenow', String(done));
    bar.setAttribute('aria-valuetext', `${done} of ${total} steps done`);
    list.replaceChildren(...items.map(row));
    // One sentence when the current step or the count changes; a reworded
    // step or another streamed event must not speak again.
    const signature = `${index}:${item?.status || ''}:${done}:${total}`;
    if (signature !== announced) {
      announced = signature;
      status.textContent = `Step ${Math.min(index + 1, total)} of ${total}. ${text}`;
    }
  }

  function update(nextItems, transcriptNode) {
    transcript = transcriptNode || transcript;
    items = Array.isArray(nextItems)
      ? nextItems.filter(item => item && String(item.text || '').trim())
      : [];
    if (!items.length) { hideCardOnly(); return; }
    if (dismissed) { showTranscript(); return; }
    // A finished checklist belongs to the transcript, not above the composer.
    if (items.every(item => item.status === 'done')) { hideCardOnly(); return; }
    if (!mount()) return;
    paint();
    card.hidden = false;
    hideTranscript();
  }

  function hideCardOnly() {
    if (card) { setOpen(false); card.hidden = true; }
    showTranscript();
  }

  function finish() {
    releaseFocus();
    hideCardOnly();
    dismissed = false;
    announced = '';
    items = [];
  }

  function reset() {
    dismissed = false;
    announced = '';
    items = [];
    transcript = null;
    if (card) { setOpen(false); card.hidden = true; }
  }

  return {update, finish, reset, isOpen: () => Boolean(card && !card.hidden)};
})();
window.KovaPlanCard = KovaPlanCard;

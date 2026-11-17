/* Settings: the one place for everything that is not the conversation.
   Loaded after app.js. The sidebar keeps a single Settings row; every panel
   that used to sit beside it is a row here. Rows run the action targets
   app.js and the panel scripts already bind (index.html #settings-actions),
   so their logic stays where it is and this file only arranges and searches. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);

  // Each item is one setting: a link to the dialog that owns it (`action`), an
  // inline control (`control`), or an existing widget hosted in place (`host`).
  // The copy lives here so search matches words people use ("appearance",
  // "boot", "plugins"), not only the section labels.
  const SECTIONS = [
    {
      id: 'general', label: 'General', icon: 'settings',
      keywords: 'appearance theme light dark sounds startup sign out keyboard',
      items: [
        {title: 'Theme', help: 'Dark or light.',
         keywords: 'appearance colour color light dark mode', control: 'theme'},
        {title: 'Sounds', help: 'Quiet cues for finished replies, reminders and approvals.',
         keywords: 'sound audio chime volume alert cue', action: 'open-sounds', actionLabel: 'Choose'},
        {title: 'Keyboard shortcuts', help: 'Every key this window understands.',
         keywords: 'keys keyboard hotkeys', action: 'open-shortcuts', actionLabel: 'Show'},
        {title: 'Shortcut keys', help: 'Record a new key for an action. A key already in use is refused.',
         keywords: 'remap rebind change custom keys hotkeys record', host: 'shortcut-remap'},
        {title: 'Startup', help: 'Keep reminders and scheduled jobs running after you sign in.',
         keywords: 'start login sign in boot autostart service status bar', host: 'settings'},
        {title: 'Sign out', help: 'End this session in this window. On a paired phone, this unpairs it.',
         keywords: 'logout sign out disconnect unpair leave', action: 'disconnect', actionLabel: 'Sign out'},
      ],
    },
    {
      id: 'models', label: 'Models and providers', icon: 'square-terminal',
      keywords: 'model provider api key vision local ollama',
      items: [
        {title: 'Providers', help: 'Add a hosted provider, or run a model on this computer.',
         keywords: 'add provider api key openai chatgpt custom local ollama',
         action: 'add-provider', actionLabel: 'Connect'},
        {title: 'Model settings', help: 'Vision and details for the model in the message box.',
         keywords: 'vision image model settings context', action: 'open-model-settings', actionLabel: 'Open'},
      ],
    },
    {
      id: 'memory', label: 'Memory', icon: 'brain',
      keywords: 'notes remember recall obsidian',
      items: [
        {title: 'Memory notes', help: 'Browse, search, edit or forget what Kova remembers.',
         keywords: 'notes remember recall obsidian forget', action: 'open-memory', actionLabel: 'Open'},
      ],
    },
    {
      id: 'reminders', label: 'Reminders', icon: 'bell',
      keywords: 'alarms schedule jobs',
      items: [
        {title: 'Reminders', help: 'Everything scheduled to remind you.',
         keywords: 'alarms schedule jobs timer', action: 'open-reminders', actionLabel: 'Open'},
      ],
    },
    {
      id: 'extensions', label: 'Extensions and MCP', icon: 'puzzle',
      keywords: 'plugins skills mcp tools catalog',
      items: [
        {title: 'Extensions and skills', help: 'See what each plugin adds, and edit your own skills.',
         keywords: 'plugins skills catalog install add-ons', action: 'open-extensions', actionLabel: 'Open'},
        {title: 'MCP servers', help: 'Add tools from Model Context Protocol servers.',
         keywords: 'mcp tools servers', action: 'open-mcp', actionLabel: 'Open'},
      ],
    },
    {
      id: 'privacy', label: 'Privacy', icon: 'lock',
      keywords: 'local only outbound report traffic secrets network',
      items: [
        {title: 'Local only and traffic report',
         help: 'Keep every request on this computer, and see what left it.',
         keywords: 'local only report outbound requests network', action: 'open-privacy', actionLabel: 'Open'},
      ],
    },
    {
      id: 'phone', label: 'Phone', icon: 'smartphone',
      keywords: 'phone pair device remote mobile qr',
      items: [
        {title: 'Connect your phone', help: 'Chat from your phone on the same private network.',
         keywords: 'phone pair device remote mobile qr', action: 'connect-phone', actionLabel: 'Connect'},
      ],
    },
    {
      id: 'install', label: 'Install and notifications', icon: 'download',
      keywords: 'pwa install notifications alerts app',
      items: [
        {title: 'Install and notifications', help: 'Add Kova to this device and choose how it notifies you.',
         keywords: 'pwa install notification alerts app', action: 'install-settings', actionLabel: 'Open'},
      ],
    },
  ];

  const sections = [];
  let view = null, search = null, status = null, noResults = null;
  let activeId = 'general', query = '', opener = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(name) {
    const node = element('span', `icon i-${name}`);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function iconButton(name, label) {
    const button = element('button', 'icon-button');
    button.type = 'button';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.append(icon(name));
    return button;
  }

  // Rebuild a text node with <mark> around every case-insensitive match, so a
  // searched word is visible in place and never inserted as markup.
  function marked(text, needle) {
    const parts = [];
    const lower = text.toLowerCase();
    let at = 0;
    if (!needle) return [document.createTextNode(text)];
    while (at < text.length) {
      const found = lower.indexOf(needle, at);
      if (found < 0) break;
      if (found > at) parts.push(document.createTextNode(text.slice(at, found)));
      const mark = element('mark', '', text.slice(found, found + needle.length));
      parts.push(mark);
      at = found + needle.length;
    }
    if (at < text.length) parts.push(document.createTextNode(text.slice(at)));
    return parts;
  }

  function paint(entry, needle) {
    entry.titleNode.replaceChildren(...marked(entry.item.title, needle));
    entry.helpNode.replaceChildren(...marked(entry.item.help, needle));
  }

  function activate(id, moveFocus = false) {
    const target = sections.find(section => section.id === id) || sections[0];
    if (!target) return;
    activeId = target.id;
    for (const section of sections) {
      const selected = section.id === target.id;
      section.tab.setAttribute('aria-selected', String(selected));
      section.tab.tabIndex = selected ? 0 : -1;
      section.panel.hidden = !selected;
      if (selected && moveFocus) section.tab.focus();
    }
  }

  function visibleTabs() {
    return sections.filter(section => !section.tab.hidden);
  }

  function filter(raw) {
    query = String(raw || '').trim().toLowerCase();
    let matches = 0;
    for (const section of sections) {
      const sectionHit = Boolean(query) &&
        `${section.label} ${section.keywords || ''}`.toLowerCase().includes(query);
      let visible = 0;
      for (const entry of section.entries) {
        const hit = (!query || sectionHit || entry.search.includes(query)) && entry.available();
        entry.row.hidden = !hit;
        paint(entry, hit ? query : '');
        if (hit) visible += 1;
      }
      const show = visible > 0;
      section.panel.hidden = !show || section.id !== activeId;
      section.tab.hidden = !show;
      if (show) matches += visible;
    }
    if (query) {
      status.textContent = matches
        ? `${matches} ${matches === 1 ? 'setting matches' : 'settings match'} \u201c${raw.trim()}\u201d.`
        : `No settings match \u201c${raw.trim()}\u201d.`;
    } else {
      status.textContent = '';
    }
    noResults.hidden = Boolean(matches) || !query;
    const visible = visibleTabs();
    if (visible.length && !visible.some(section => section.id === activeId)) activate(visible[0].id);
    if (!visible.length) {
      for (const section of sections) section.panel.hidden = true;
    }
  }

  function open(sectionId) {
    if (!view) return;
    opener = document.activeElement;
    syncControls();
    search.value = '';
    filter('');
    activate(sectionId || activeId);
    view.hidden = false;
    document.body.classList.add('settings-open');
    setBackground(true);
    search.focus();
  }

  function close() {
    if (!view || view.hidden) return;
    view.hidden = true;
    document.body.classList.remove('settings-open');
    setBackground(false);
    opener?.focus?.();
  }

  function setBackground(unavailable) {
    const nodes = [byId('sidebar'), document.querySelector?.('main')].filter(Boolean);
    for (const node of nodes) node.inert = unavailable;
  }

  function tabStops() {
    const nodes = view.querySelectorAll('button, input, [tabindex="0"]');
    return [...nodes].filter(node => {
      if (node.disabled) return false;
      for (let at = node; at; at = at.parentNode) if (at.hidden) return false;
      return true;
    });
  }

  // Light and dark as a two-option radio group. app.js owns the theme (and its
  // Ctrl+Shift+L toggle) through #theme; this only reflects and flips it.
  const themeRadios = [];
  function currentTheme() {
    return document.documentElement?.dataset?.theme === 'light' ? 'light' : 'dark';
  }
  function themeControl() {
    const group = element('div', 'settings-segmented');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', 'Theme');
    for (const [value, label, glyph] of [['dark', 'Dark', 'moon'], ['light', 'Light', 'sun']]) {
      const radio = element('button', 'settings-segment');
      radio.type = 'button';
      radio.dataset.value = value;
      radio.setAttribute('role', 'radio');
      radio.append(icon(glyph), element('span', '', label));
      radio.addEventListener('click', () => chooseTheme(value));
      radio.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
        event.preventDefault();
        const next = value === 'dark' ? 'light' : 'dark';
        chooseTheme(next);
        themeRadios.find(item => item.dataset.value === next)?.focus();
      });
      themeRadios.push(radio);
      group.append(radio);
    }
    return group;
  }
  function chooseTheme(value) {
    if (currentTheme() !== value) byId('theme')?.click();
    syncTheme();
  }
  function syncTheme() {
    const value = currentTheme();
    for (const radio of themeRadios) {
      const checked = radio.dataset.value === value;
      radio.setAttribute('aria-checked', String(checked));
      radio.tabIndex = checked ? 0 : -1;
    }
  }

  // Rows follow their target: a control another script hides (Connect your
  // phone, on the phone itself) or disables (while offline) does the same here.
  const actionButtons = [];
  function syncControls() {
    syncTheme();
    for (const {button, item} of actionButtons) {
      const target = byId(item.action);
      button.disabled = Boolean(target?.disabled);
    }
  }

  function rowFor(item) {
    const row = element('div', 'settings-row');
    const copy = element('div', 'settings-copy');
    const title = element('span', 'settings-row-title', item.title);
    const help = element('p', 'settings-row-help', item.help);
    copy.append(title, help);
    row.append(copy);
    if (item.action) {
      const button = element('button', 'btn small settings-action', item.actionLabel || 'Open');
      button.type = 'button';
      button.setAttribute('aria-label', `${item.actionLabel || 'Open'}: ${item.title}`);
      button.addEventListener('click', () => {
        const target = byId(item.action);
        if (target && !target.disabled) target.click();
      });
      actionButtons.push({button, item});
      row.append(button);
    } else if (item.control === 'theme') {
      row.append(themeControl());
    } else if (item.host) {
      const hosted = byId(item.host);
      if (hosted) {
        row.classList.add('settings-row-hosted');
        hosted.classList.add('settings-hosted');
        hosted.open = true;
        row.append(hosted);
      }
    }
    return {row, title, help};
  }

  function build() {
    view = element('section', 'settings-page');
    view.id = 'settings-page';
    view.setAttribute('role', 'region');
    view.setAttribute('aria-labelledby', 'settings-title');
    view.setAttribute('aria-describedby', 'settings-description');
    view.hidden = true;

    const head = element('div', 'settings-head');
    const heading = element('div', 'settings-heading');
    const title = element('h2', '', 'Settings');
    title.id = 'settings-title';
    const lede = element('p', 'settings-lede', 'Everything Kova keeps on this computer, in one place.');
    lede.id = 'settings-description';
    heading.append(title, lede);
    const closeButton = iconButton('x', 'Close settings');
    closeButton.addEventListener('click', close);
    head.append(heading, closeButton);

    const layout = element('div', 'settings-layout');
    const side = element('aside', 'settings-side');

    const label = element('label', 'settings-search');
    label.append(icon('search'));
    search = document.createElement('input');
    search.type = 'search';
    search.id = 'settings-search';
    search.placeholder = 'Search settings';
    search.setAttribute('aria-label', 'Search settings');
    search.setAttribute('autocomplete', 'off');
    search.setAttribute('spellcheck', 'false');
    search.addEventListener('input', () => filter(search.value));
    label.append(search);

    status = element('p', 'settings-status');
    status.id = 'settings-search-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    const nav = element('nav', 'settings-nav');
    nav.setAttribute('role', 'tablist');
    nav.setAttribute('aria-label', 'Settings sections');
    nav.setAttribute('aria-orientation', 'vertical');

    const content = element('div', 'settings-content');
    noResults = element('p', 'settings-empty', 'No settings match that search. Try a shorter word.');
    noResults.id = 'settings-no-results';
    noResults.hidden = true;
    content.append(noResults);

    for (const data of SECTIONS) {
      const panel = element('section', 'settings-section');
      panel.id = `settings-panel-${data.id}`;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', `settings-tab-${data.id}`);
      panel.tabIndex = 0;
      panel.hidden = true;
      panel.append(element('h3', 'settings-section-title', data.label));

      const tab = element('button', 'settings-tab');
      tab.type = 'button';
      tab.id = `settings-tab-${data.id}`;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', panel.id);
      tab.setAttribute('aria-selected', 'false');
      tab.tabIndex = -1;
      tab.append(icon(data.icon), element('span', 'settings-tab-label', data.label));
      tab.addEventListener('click', () => activate(data.id));
      nav.append(tab);

      const rows = element('div', 'settings-rows');
      const entries = [];
      for (const item of data.items) {
        const {row, title: rowTitle, help} = rowFor(item);
        rows.append(row);
        entries.push({
          row, item,
          available: () => !item.action || !byId(item.action)?.hidden,
          titleNode: rowTitle, helpNode: help,
          search: `${data.label} ${item.title} ${item.help} ${item.keywords || ''}`.toLowerCase(),
        });
      }
      panel.append(rows);

      content.append(panel);
      sections.push({id: data.id, label: data.label, keywords: data.keywords, tab, panel, entries});
    }

    nav.addEventListener('keydown', event => {
      const visible = visibleTabs();
      if (!visible.length) return;
      const at = visible.findIndex(section => section.tab === document.activeElement);
      let next = null;
      if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        next = at < 0 ? visible[0] : visible[(at + 1) % visible.length];
      } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
        next = at < 0 ? visible[visible.length - 1] : visible[(at - 1 + visible.length) % visible.length];
      } else if (event.key === 'Home') {
        next = visible[0];
      } else if (event.key === 'End') {
        next = visible[visible.length - 1];
      } else {
        return;
      }
      event.preventDefault();
      activate(next.id, true);
    });

    side.append(label, status, nav);
    layout.append(side, content);
    view.append(head, layout);
    view.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      if (event.key !== 'Tab') return;
      const stops = tabStops();
      if (!stops.length) return;
      const first = stops[0], last = stops[stops.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    document.body.append(view);

    activate('general');
  }

  build();
  const launch = byId('open-settings');
  if (launch) launch.addEventListener('click', event => { event.preventDefault(); open(); });
  // The command palette opens a section directly ("Startup settings").
  if (typeof window !== 'undefined') window.KovaSettings = {open, close};
})();

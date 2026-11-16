/* One Settings view for the panels that used to sit in the sidebar footer.
   Loaded after app.js: it hosts the existing controls by id, so their logic
   stays where it is and this file only arranges and searches them. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);

  // Each row is one setting, or a link to the dialog that owns it. Keeping the
  // copy here means search can match words people actually use ("appearance",
  // "boot", "plugins") instead of only the section labels.
  const SECTIONS = [
    {
      id: 'general', label: 'General', icon: 'sliders-horizontal',
      keywords: 'appearance theme light dark shortcuts keyboard install notifications phone device',
      items: [
        {title: 'Theme', help: 'Switch between the dark and light theme.',
         keywords: 'appearance colour color light dark', action: 'theme', actionLabel: 'Switch theme'},
        {title: 'Keyboard shortcuts', help: 'Every shortcut the app listens for.',
         keywords: 'keys keyboard', action: 'open-shortcuts', actionLabel: 'Show shortcuts'},
        {title: 'Install and notifications', help: 'Install Kova on this device and choose how it notifies you.',
         keywords: 'pwa install notification phone', action: 'install-settings', actionLabel: 'Open install'},
        {title: 'Sounds', help: 'Choose the cues for finished replies, reminders and approvals.',
         keywords: 'sound audio chime volume alert', action: 'open-sounds', actionLabel: 'Open sounds'},
        {title: 'Connect your phone', help: 'Chat from your phone on the same private network.',
         keywords: 'phone pair device remote', action: 'connect-phone', actionLabel: 'Connect a phone'},
      ],
    },
    {
      id: 'models', label: 'Models and providers', icon: 'square-terminal',
      keywords: 'model provider api key vision local ollama',
      items: [
        {title: 'Connect a provider', help: 'Add a hosted provider or use a model on this computer.',
         keywords: 'add provider api key openai chatgpt custom local ollama',
         action: 'add-provider', actionLabel: 'Connect a provider'},
        {title: 'Model settings', help: 'Turn on vision for the model in the composer.',
         keywords: 'vision image model settings', action: 'open-model-settings', actionLabel: 'Open model settings'},
      ],
    },
    {
      id: 'privacy', label: 'Privacy', icon: 'lock',
      keywords: 'local only outbound report traffic secrets',
      items: [
        {title: 'Local only and traffic report',
         help: 'Keep every request on this computer, and see what left it.',
         keywords: 'local only report outbound requests', action: 'open-privacy', actionLabel: 'Open privacy'},
      ],
    },
    {
      id: 'memory', label: 'Memory', icon: 'brain',
      keywords: 'notes remember recall obsidian',
      items: [
        {title: 'Memory notes', help: 'Browse, search, edit or forget what Kova remembers.',
         keywords: 'notes remember recall obsidian', action: 'open-memory', actionLabel: 'Open memory'},
      ],
    },
    {
      id: 'extensions', label: 'Extensions', icon: 'puzzle',
      keywords: 'plugins skills mcp tools catalog',
      items: [
        {title: 'Installed extensions and skills', help: 'See what each plugin adds, and edit your own skills.',
         keywords: 'plugins skills catalog install', action: 'open-extensions', actionLabel: 'Open extensions'},
        {title: 'MCP servers', help: 'Add tools from Model Context Protocol servers.',
         keywords: 'mcp tools servers', action: 'open-mcp', actionLabel: 'Open MCP servers'},
      ],
    },
    {
      id: 'startup', label: 'Startup', icon: 'arrow-up',
      keywords: 'start login sign in boot autostart service',
      items: [
        {title: 'Start Kova when I sign in',
         help: 'Keep reminders and scheduled jobs running after you sign in.',
         keywords: 'start login sign in boot autostart service startup'},
      ],
      // The autostart widget already exists in the page; host it verbatim so
      // its toggle and status keep talking to the same app.js code.
      host: 'settings',
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
        const hit = !query || sectionHit || entry.search.includes(query);
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

  function rowFor(section, item) {
    const row = element('div', 'settings-row');
    const copy = element('div', 'settings-copy');
    const title = element('span', 'settings-row-title', item.title);
    const help = element('p', 'settings-row-help', item.help);
    copy.append(title, help);
    row.append(copy);
    if (item.action) {
      const button = element('button', 'btn quiet settings-open', item.actionLabel || 'Open');
      button.type = 'button';
      button.setAttribute('aria-label', `${item.actionLabel || 'Open'} ${item.title}`);
      button.addEventListener('click', () => {
        const target = byId(item.action);
        if (target) target.click();
      });
      row.append(button);
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
        const {row, title: rowTitle, help} = rowFor(data, item);
        rows.append(row);
        entries.push({
          row, item,
          titleNode: rowTitle, helpNode: help,
          search: `${data.label} ${item.title} ${item.help} ${item.keywords || ''}`.toLowerCase(),
        });
      }
      panel.append(rows);

      if (data.host) {
        const hosted = byId(data.host);
        if (hosted) {
          hosted.classList.add('settings-autostart');
          hosted.open = true;
          const wrap = element('div', 'settings-host');
          wrap.append(hosted);
          panel.append(wrap);
        }
      }

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
})();

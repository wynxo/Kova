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
      keywords: 'appearance theme light dark system sounds startup sign out keyboard reasoning text size density font typography',
      items: [
        {title: 'Theme', help: 'Follow this device, choose light or dark, or use a theme file you installed.',
         keywords: 'appearance colour color light dark mode system auto device installed custom theme file',
         control: 'theme'},
        {title: 'Text size', help: 'Scale every label, message and heading a step up or down.',
         keywords: 'text size font typography small large bigger read legible zoom scale',
         control: 'text_size'},
        {title: 'Density', help: 'Tighten or open the space around messages, sidebar rows and the message box.',
         keywords: 'density spacing compact comfortable padding layout tight roomy air',
         control: 'density'},
        {title: 'Font', help: 'Keep the bundled Inter, follow this device, or set the conversation in a monospace face.',
         keywords: 'font typeface inter system monospace mono code readable',
         control: 'font'},
        {title: 'Reasoning', help: "Fold the model's thinking into a row, open it, or hide it.",
         keywords: 'thinking thought chain show hide collapsed expanded rows',
         control: 'reasoning'},
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

  // The theme as one radio group: System, Light and Dark, then any theme files
  // installed in Kova's home (#174). theme.js owns the choice, its shared copy in
  // the server preferences and the tokens it applies; this only reflects and sets
  // it. Without theme.js it falls back to the #theme light/dark toggle.
  const themeGroups = [];
  const FIXED_THEMES = [['system', 'System', 'settings'], ['light', 'Light', 'sun'], ['dark', 'Dark', 'moon']];
  function themeOptions() {
    const engine = window.KovaTheme;
    if (!engine) return FIXED_THEMES.filter(([value]) => value !== 'system').map(([id, name, glyph]) => ({id, name, glyph}));
    return engine.choices().map(option => {
      const fixed = FIXED_THEMES.find(([value]) => value === option.id);
      return {id: option.id, name: option.name, glyph: fixed ? fixed[2] : (option.appearance === 'light' ? 'sun' : 'moon')};
    });
  }
  function currentTheme() {
    if (window.KovaTheme) return window.KovaTheme.choice();
    return document.documentElement?.dataset?.theme === 'light' ? 'light' : 'dark';
  }
  function themeControl() {
    const group = element('div', 'settings-segmented settings-theme');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', 'Theme');
    themeGroups.push(group);
    fillTheme(group);
    return group;
  }
  function fillTheme(group) {
    const options = themeOptions();
    const radios = options.map(({id, name, glyph}, index) => {
      const radio = element('button', 'settings-segment');
      radio.type = 'button';
      radio.dataset.value = id;
      radio.setAttribute('role', 'radio');
      radio.append(icon(glyph), element('span', '', name));
      radio.addEventListener('click', () => chooseTheme(id));
      radio.addEventListener('keydown', event => {
        const step = {ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1}[event.key];
        if (!step) return;
        event.preventDefault();
        const next = options[(index + step + options.length) % options.length].id;
        chooseTheme(next);
        [...(group.children || [])].find(item => item.dataset.value === next)?.focus();
      });
      return radio;
    });
    group.dataset.options = options.map(option => option.id).join(' ');
    group.replaceChildren(...radios);
  }
  function chooseTheme(value) {
    if (window.KovaTheme) window.KovaTheme.set(value);
    else if (currentTheme() !== value) byId('theme')?.click();
    syncTheme();
  }
  function syncTheme() {
    const ids = themeOptions().map(option => option.id).join(' ');
    const value = currentTheme();
    for (const group of themeGroups) {
      // A theme file added or removed since the group was drawn redraws it.
      if (group.dataset.options !== ids) fillTheme(group);
      const radios = [...(group.children || [])];
      const any = radios.some(radio => radio.dataset.value === value);
      radios.forEach((radio, index) => {
        const checked = radio.dataset.value === value;
        radio.setAttribute('aria-checked', String(checked));
        radio.tabIndex = checked || (!any && index === 0) ? 0 : -1;
      });
    }
  }
  document.addEventListener?.('kova:theme', () => syncTheme());

  // Rows follow their target: a control another script hides (Connect your
  // phone, on the phone itself) or disables (while offline) does the same here.
  const actionButtons = [];

  // Reasoning is a three-way choice: folded rows (the default), rows that
  // open as they arrive, or no reasoning at all. KovaReasoning owns the value,
  // its local copy and the server-side UI preferences, so this only reflects
  // and changes it.
  const REASONING = [
    ['collapsed', 'Collapsed', 'Fold each thought row until it is opened'],
    ['expanded', 'Expanded', 'Open each thought row as it finishes'],
    ['hidden', 'Hidden', 'Never show the reasoning text'],
  ];
  const reasoningRadios = [];
  function reasoningMode() {
    return window.KovaReasoning ? window.KovaReasoning.mode() : 'collapsed';
  }
  function reasoningControl() {
    const group = element('div', 'settings-segmented');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', 'Reasoning');
    for (const [value, label, hint] of REASONING) {
      const radio = element('button', 'settings-segment');
      radio.type = 'button';
      radio.dataset.value = value;
      radio.title = hint;
      radio.setAttribute('role', 'radio');
      radio.append(element('span', '', label));
      radio.addEventListener('click', () => chooseReasoning(value));
      radio.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
        event.preventDefault();
        const at = REASONING.findIndex(([candidate]) => candidate === value);
        const step = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1;
        const next = REASONING[(at + step + REASONING.length) % REASONING.length][0];
        chooseReasoning(next);
        reasoningRadios.find(item => item.dataset.value === next)?.focus();
      });
      reasoningRadios.push(radio);
      group.append(radio);
    }
    return group;
  }
  function chooseReasoning(value) {
    window.KovaReasoning?.setMode(value);
    syncReasoning();
  }
  function syncReasoning() {
    const value = reasoningMode();
    for (const radio of reasoningRadios) {
      const checked = radio.dataset.value === value;
      radio.setAttribute('aria-checked', String(checked));
      radio.tabIndex = checked ? 0 : -1;
    }
  }

  // Text size, density and font are the same three-way choice (#179).
  // KovaTypography owns each value, the server copy and the three attributes on
  // <html>; this only reflects and changes them. "default" and "inter" are the
  // shipped look, so the fallback matches what the page paints without the module.
  const TYPOGRAPHY = [
    {name: 'text_size', label: 'Text size', read: 'textSize', write: 'setTextSize', fallback: 'default',
     items: [['small', 'Small', 'A step down from the shipped sizes'],
             ['default', 'Default', 'The shipped type scale'],
             ['large', 'Large', 'A step up for reading at a distance']]},
    {name: 'density', label: 'Density', read: 'density', write: 'setDensity', fallback: 'default',
     items: [['compact', 'Compact', 'Tighter gaps, for more on screen'],
             ['default', 'Default', 'The shipped spacing'],
             ['comfortable', 'Comfortable', 'More air between rows and messages']]},
    {name: 'font', label: 'Font', read: 'font', write: 'setFont', fallback: 'inter',
     items: [['inter', 'Inter', 'The bundled typeface'],
             ['system', 'System', "This device's interface font"],
             ['mono', 'Monospace', 'Set the conversation in the bundled mono face']]},
  ];
  const typographyRadios = {};
  function typographyChoice(name) {
    return TYPOGRAPHY.find(choice => choice.name === name);
  }
  function typographyValue(choice) {
    const engine = window.KovaTypography;
    return (engine && typeof engine[choice.read] === 'function' && engine[choice.read]()) || choice.fallback;
  }
  function typographyControl(choice) {
    const group = element('div', 'settings-segmented');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', choice.label);
    const radios = [];
    for (const [value, label, hint] of choice.items) {
      const radio = element('button', 'settings-segment');
      radio.type = 'button';
      radio.dataset.value = value;
      radio.title = hint;
      radio.setAttribute('role', 'radio');
      radio.append(element('span', '', label));
      radio.addEventListener('click', () => chooseTypography(choice, value));
      radio.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
        event.preventDefault();
        const at = choice.items.findIndex(([candidate]) => candidate === value);
        const step = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1;
        const next = choice.items[(at + step + choice.items.length) % choice.items.length][0];
        chooseTypography(choice, next);
        radios.find(item => item.dataset.value === next)?.focus();
      });
      radios.push(radio);
      group.append(radio);
    }
    typographyRadios[choice.name] = radios;
    return group;
  }
  function chooseTypography(choice, value) {
    window.KovaTypography?.[choice.write]?.(value);
    syncTypography();
  }
  function syncTypography() {
    for (const choice of TYPOGRAPHY) {
      const value = typographyValue(choice);
      for (const radio of typographyRadios[choice.name] || []) {
        const checked = radio.dataset.value === value;
        radio.setAttribute('aria-checked', String(checked));
        radio.tabIndex = checked ? 0 : -1;
      }
    }
  }

  function syncControls() {
    syncTheme();
    syncReasoning();
    syncTypography();
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
    } else if (item.control === 'reasoning') {
      row.append(reasoningControl());
    } else if (typographyChoice(item.control)) {
      row.append(typographyControl(typographyChoice(item.control)));
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

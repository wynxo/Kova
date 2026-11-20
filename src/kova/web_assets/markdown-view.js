// Markdown for replies (#189): parse, sanitize, scope and decorate one message.
// app.js delegates here so the renderer can grow without the app shell growing
// with it. Vanilla JS, classic script: window.marked, window.DOMPurify and
// window.CodeHeader come from the files before it.
(function () {
  // No remote images, embedded content, form controls we did not ask for, SVG or
  // style injection. `sup` carries footnote references, `input` a read-only task
  // checkbox, `section` the footnote list, and `details`/`summary` the collapsible
  // blocks a reply may write (#189).
  const ALLOWED_TAGS = ['p', 'br', 'strong', 'em', 'del', 'a', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'table', 'thead', 'tbody',
    'tr', 'th', 'td', 'sup', 'section', 'input', 'details', 'summary', 'span'];
  const ALLOWED_ATTR = ['href', 'title', 'class', 'start', 'id', 'type', 'checked', 'disabled',
    'aria-label', 'aria-hidden', 'open', 'data-kova-file'];

  // GitHub-flavored callouts (#189): a quote whose first line is `[!NOTE]` and so
  // on becomes a labelled aside. The marker is replaced by a visible label, so the
  // kind is never carried by colour alone.
  const CALLOUTS = {
    note: 'Note',
    tip: 'Tip',
    important: 'Important',
    warning: 'Warning',
    caution: 'Caution',
  };
  const CALLOUT = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*/i;

  // Three or more backticks or tildes at the start of a line open or close a fence.
  const FENCE = /^ {0,3}(`{3,}|~{3,})/;
  const DEFINITION = /^\[\^([^\]\s]+)\]:[ \t]*(.*)$/;
  const REFERENCE = /\[\^([^\]\s]+)\]/g;
  let scopes = 0;
  let mathParser, katexScript, diagramFrame, diagramFrameReady, diagramJobs;
  let diagramId = 0;
  const diagramSources = new WeakMap();

  // Is the last code fence still open? A block that is still being written is
  // rendered as plain text and highlighted only once it closes.
  function hasOpenFence(text) {
    let open = null;
    for (const line of String(text || '').split('\n')) {
      const match = FENCE.exec(line);
      if (!match) continue;
      if (!open) open = match[1];
      else if (match[1][0] === open[0] && match[1].length >= open.length) open = null;
    }
    return open !== null;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (character) => (
      {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
  }

  function inline(text) {
    const parser = window.marked?.parseInline;
    return parser ? parser(text, {breaks: true, gfm: true}) : escapeHtml(text);
  }

  // GFM footnote definitions, kept out of the visible body. Definitions inside a
  // code fence are code, never footnotes. A definition continues over indented
  // lines, the way Markdown punctuation works everywhere else.
  function extractFootnotes(text) {
    const lines = text.split('\n');
    const definitions = new Map();
    const kept = [];
    let fence = null;
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const fenceMatch = FENCE.exec(line);
      if (fenceMatch) {
        if (!fence) fence = fenceMatch[1];
        else if (fenceMatch[1][0] === fence[0] && fenceMatch[1].length >= fence.length) fence = null;
        kept.push(line);
        continue;
      }
      if (fence) { kept.push(line); continue; }
      const definition = DEFINITION.exec(line);
      if (!definition) { kept.push(line); continue; }
      const body = [definition[2]];
      while (index + 1 < lines.length) {
        const next = lines[index + 1];
        if (!next.trim()) {
          // A blank line may separate paragraphs of the same note.
          if (/^[ \t]{2,}\S/.test(lines[index + 2] || '')) { body.push(''); index += 1; continue; }
          break;
        }
        if (!/^[ \t]{2,}\S/.test(next)) break;
        body.push(next.trim());
        index += 1;
      }
      definitions.set(definition[1], body.join('\n').trim());
    }
    return {text: kept.join('\n'), definitions};
  }

  // Footnote references become numbered links; their notes are appended once, in
  // the order the reader meets them. Nothing is replaced inside code.
  function replaceReferences(text, definitions) {
    if (!definitions.size) return {text, count: 0};
    const numbers = new Map();
    const seen = new Map();
    const order = [];
    const lines = text.split('\n');
    let fence = null;
    const rendered = lines.map((line) => {
      const fenceMatch = FENCE.exec(line);
      if (fenceMatch) {
        if (!fence) fence = fenceMatch[1];
        else if (fenceMatch[1][0] === fence[0] && fenceMatch[1].length >= fence.length) fence = null;
        return line;
      }
      if (fence) return line;
      // Inline code spans are code too: `[^1]` in backticks stays literal.
      return line.split(/(`+[^`]*`+)/).map((part, index) => {
        if (index % 2) return part;
        return part.replace(REFERENCE, (whole, label) => {
          if (!definitions.has(label)) return whole;
          if (!numbers.has(label)) {
            numbers.set(label, numbers.size + 1);
            order.push(label);
          }
          const number = numbers.get(label);
          const visit = (seen.get(label) || 0) + 1;
          seen.set(label, visit);
          const id = visit === 1 ? `fnref-${number}` : `fnref-${number}-${visit}`;
          return `<sup class="fn-ref" id="${id}"><a href="#fn-${number}" aria-label="Footnote ${number}">${number}</a></sup>`;
        });
      }).join('');
    });
    const notes = order.map((label) => {
      const number = numbers.get(label);
      return `<li id="fn-${number}"><p>${inline(definitions.get(label) || '')} ` +
        `<a href="#fnref-${number}" class="fn-back" aria-label="Back to reference ${number}">↩</a></p></li>`;
    });
    const section = `<section class="footnotes" aria-label="Footnotes">\n<ol>\n${notes.join('\n')}\n</ol>\n</section>`;
    return {text: `${rendered.join('\n')}\n\n${section}\n`, count: order.length};
  }

  // Every id in a message gets the message's own prefix, so two replies that both
  // render "fn-1" cannot steal each other's links. Re-rendering the same block
  // keeps its prefix, so anchors stay put while the text streams.
  function scopeIds(root) {
    if (!root.dataset.mdScope) {
      scopes += 1;
      root.dataset.mdScope = `md${scopes}-`;
    }
    const prefix = root.dataset.mdScope;
    const ids = [...root.querySelectorAll('[id]')];
    if (!ids.length) return;
    const renamed = new Set();
    for (const node of ids) {
      const id = node.getAttribute('id');
      if (!id) continue;
      node.setAttribute('id', prefix + id);
      renamed.add(id);
    }
    root.querySelectorAll('a[href^="#"]').forEach((link) => {
      const target = link.getAttribute('href').slice(1);
      if (renamed.has(target)) link.setAttribute('href', `#${prefix}${target}`);
    });
  }

  // Narrow screens scroll a wide table inside its own frame, never the page; the
  // header row stays put while the rows below it scroll.
  function wrapTables(root) {
    root.querySelectorAll('table').forEach((table) => {
      if (table.parentNode?.classList.contains('table-wrap')) return;
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', 'Table');
      table.replaceWith(wrap);
      wrap.append(table);
    });
  }

  // The first text node a paragraph actually paints, following inline elements
  // (`**[!NOTE]**` still counts). Null when the paragraph has no text at all.
  function leadingText(node) {
    const children = node.childNodes || node.children || [];
    if (!children.length) {
      return node.nodeType === 3 || node.tagName === '#text' ? node : null;
    }
    for (const child of children) {
      const found = leadingText(child);
      if (found) return found;
    }
    return null;
  }

  // Turn `> [!NOTE]` blocks into labelled callouts. The marker is removed from
  // the paragraph and a label paragraph is put in front of it, so a plain quote
  // is left exactly as it was and a callout always says what it is in words.
  function callouts(root) {
    const quotes = [...root.querySelectorAll('blockquote')];
    for (const quote of quotes) {
      const first = quote.querySelector('p');
      if (!first) continue;
      const text = first.textContent;
      const trimmed = text.trimStart();
      const marker = CALLOUT.exec(trimmed);
      if (!marker) continue;
      const kind = marker[1].toLowerCase();
      const remove = text.length - trimmed.length + marker[0].length;
      const node = leadingText(first);
      if (node && node.textContent.length >= remove) node.textContent = node.textContent.slice(remove);
      else first.textContent = trimmed.slice(marker[0].length);
      quote.classList.add('callout', `callout-${kind}`);
      quote.setAttribute('role', 'note');
      const label = document.createElement('p');
      label.className = 'callout-label';
      label.textContent = CALLOUTS[kind];
      quote.insertBefore(label, first);
      // A marker alone in its paragraph leaves nothing behind.
      if (!first.textContent.trim()) first.remove();
    }
  }

  // Markdown task lists are a record of what the reply says, not a control the
  // reader can change: the box is disabled and labelled for screen readers.
  function readOnlyTasks(root) {
    root.querySelectorAll('input[type="checkbox"]').forEach((box) => {
      box.disabled = true;
      box.classList.add('task-box');
      box.setAttribute('aria-label', box.checked ? 'Done' : 'Not done');
      const item = box.parentNode;
      if (!item || item.tagName?.toLowerCase() !== 'li') return;
      item.classList.add('task-item');
      if (box.checked) item.classList.add('task-done');
    });
  }

  function links(root) {
    root.querySelectorAll('a').forEach((link) => {
      const href = link.getAttribute('href') || '';
      // Fragment links stay in the message (footnotes); web links open away from
      // the app; local API URLs and scripts never become navigation.
      if (href.startsWith('#')) return;
      if (!/^https?:\/\//i.test(href)) link.removeAttribute('href');
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    });
  }

  // Fenced code supports language:path and language title="file name".
  function codeRenderer() {
    const renderer = new window.marked.Renderer();
    const code = renderer.code;
    renderer.code = function (token) {
      const info = token.lang || '';
      const match = /^([\w+#.-]+)(?::(\S+)|\s+(?:title|filename)=(?:"([^"]+)"|'([^']+)'|(\S+)))\s*$/.exec(info);
      if (!match) return code.call(this, token);
      const [, lang, ...names] = match;
      const name = names.find(Boolean).slice(0, 200);
      return code.call(this, {...token, lang}).replace(/^<pre><code\b/,
        () => `<pre><code data-kova-file="${escapeHtml(name)}"`);
    };
    return renderer;
  }

  function parser(source, final) {
    if (!final || !/[$\\]/.test(source)) return window.marked;
    if (mathParser) return mathParser;
    function extension(name, level, pattern) {
      return {
        name, level,
        start: text => {
          const start = level === 'block' ? /(^|\n)(?=\$\$|\\\[)/ : /(?=\$(?![\s$])|\\\()/;
          const found = start.exec(text);
          return found ? found.index + found[0].length : undefined;
        },
        tokenizer(text) {
          const match = pattern.exec(text);
          if (!match) return undefined;
          return {type: name, raw: match[0], text: match[1] || match[2]};
        },
        renderer(token) {
          return `<span data-kova-math="${level}">${escapeHtml(token.text)}</span>`;
        },
      };
    }
    mathParser = new window.marked.Marked({extensions: [
      extension('kovaBlockMath', 'block', /^(?:\$\$\s*([^$]+?)\s*\$\$|\\\[([\s\S]+?)\\\])[ \t]*(?:\n|$)/),
      extension('kovaInlineMath', 'inline', /^(?:\$(?![\s$])([^\n$]+?)\$(?![\d$])|\\\((.+?)\\\))/),
    ]});
    return mathParser;
  }

  function renderMath(root) {
    const formulas = [...root.querySelectorAll('[data-kova-math]')];
    if (!formulas.length) return Promise.resolve();
    katexScript ||= new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/app/assets/vendor/katex.js';
      script.onload = resolve;
      script.onerror = reject;
      document.head.append(script);
    }).catch(() => { katexScript = null; });
    return katexScript.then(() => {
      for (const formula of formulas) {
        if (!window.katex || !root.contains(formula)) continue;
        const source = formula.textContent;
        if (source.length > 4000) continue;
        try {
          const html = window.katex.renderToString(source, {
            displayMode: formula.getAttribute('data-kova-math') === 'block',
            output: 'mathml', trust: false, maxExpand: 1000, maxSize: 12,
          });
          formula.innerHTML = window.DOMPurify.sanitize(html, {
            USE_PROFILES: {mathMl: true},
            FORBID_ATTR: ['style', 'href', 'src', 'mathcolor', 'mathbackground'],
          });
          formula.setAttribute('data-math-source', source);
        } catch (_) { /* Keep the literal formula if it cannot render. */ }
      }
    });
  }

  function readyDiagramFrame() {
    if (diagramFrameReady) return diagramFrameReady;
    diagramJobs = new Map();
    diagramFrame = document.createElement('iframe');
    diagramFrame.className = 'diagram-sandbox';
    diagramFrame.setAttribute('sandbox', 'allow-scripts');
    diagramFrame.setAttribute('aria-hidden', 'true');
    diagramFrame.tabIndex = -1;
    const assets = location.origin + '/app/assets';
    const library = assets + '/vendor/mermaid.js';
    const renderer = assets + '/diagram-frame.js';
    diagramFrame.srcdoc = `<!doctype html><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src ${library} ${renderer}; style-src 'unsafe-inline'">
      <script defer src="${library}"></script><script defer src="${renderer}"></script>`;
    diagramFrameReady = new Promise(resolve => {
      const frame = diagramFrame;
      const timeout = setTimeout(() => { frame.remove(); resolve(false); }, 10000);
      window.addEventListener('message', ({source, origin, data}) => {
        if (source !== frame.contentWindow || origin !== 'null') return;
        if (data?.ready) { clearTimeout(timeout); resolve(true); }
        diagramJobs.get(data?.id)?.(data.svg);
      });
    });
    document.body.append(diagramFrame);
    return diagramFrameReady;
  }

  function renderDiagram(code) {
    const source = code.textContent;
    if (!source.trim() || source.length > 12000 || /^\s*---/m.test(source) || source.includes('%%{')) return Promise.resolve();
    const style = getComputedStyle(document.documentElement);
    const token = name => style.getPropertyValue(name).trim();
    const palette = {
      darkMode: document.documentElement.dataset.theme !== 'light',
      background: token('--bg-base'), raised: token('--bg-raised'),
      text: token('--fg-default'), border: token('--border-strong'),
    };
    const key = JSON.stringify(palette);
    if (diagramSources.get(code) === key) return Promise.resolve();
    diagramSources.set(code, key);
    return readyDiagramFrame().then(ready => {
      if (!ready || !code.isConnected || diagramSources.get(code) !== key) return;
      const id = ++diagramId;
      return new Promise(resolve => {
        const timeout = setTimeout(() => { diagramJobs.delete(id); resolve(); }, 10000);
        diagramJobs.set(id, svg => {
          clearTimeout(timeout);
          diagramJobs.delete(id);
          if (typeof svg === 'string' && svg.startsWith('<svg') && svg.length < 500000 &&
              code.isConnected && diagramSources.get(code) === key) {
            const pre = code.parentElement;
            let image = pre.previousElementSibling;
            if (!image?.classList.contains('markdown-diagram')) {
              image = document.createElement('img');
              image.className = 'markdown-diagram';
              pre.before(image);
            }
            const parsed = new DOMParser().parseFromString(svg, 'image/svg+xml');
            image.alt = parsed.querySelector('title')?.textContent || 'Mermaid diagram';
            image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
            image.dataset.diagramTheme = palette.darkMode ? 'dark' : 'light';
          }
          resolve();
        });
        diagramFrame.contentWindow.postMessage({id, source, palette}, '*');
      });
    });
  }

  function outline(...roots) {
    const headings = roots.flatMap(root => [...root.querySelectorAll('h1, h2, h3, h4, h5, h6')])
      .filter(heading => heading.textContent.trim());
    return headings.length >= 3 ? headings : [];
  }

  function plainText(root) {
    function text(node) {
      if (node.nodeType === 3) return node.textContent;
      if (node.matches?.('button, .code-bar, .code-gutter, .fn-back')) return '';
      if (node.hasAttribute?.('data-math-source')) return node.getAttribute('data-math-source');
      const tag = node.tagName?.toLowerCase();
      if (tag === 'pre') return `\n${node.querySelector('code')?.textContent || ''}\n`;
      const contents = Array.from(node.childNodes, text).join('');
      if (tag === 'td' || tag === 'th') return contents + '\t';
      if (/^(br|p|h[1-6]|li|blockquote|tr|summary)$/.test(tag)) return contents + '\n';
      return contents;
    }
    return text(root).trim();
  }

  // Render `text` into `root`. `final` is false while the reply streams: an open
  // code fence then stays plain text and cannot be copied yet, and footnotes wait
  // for the finished reply so the note list does not move as the text grows.
  function render(root, text, options = {}) {
    const final = Boolean(options.final);
    const source = String(text || '');
    const {text: withoutDefinitions, definitions} = final
      ? extractFootnotes(source)
      : {text: source, definitions: new Map()};
    const prepared = definitions.size ? replaceReferences(withoutDefinitions, definitions) : {text: withoutDefinitions};
    const html = parser(prepared.text, final).parse(prepared.text, {breaks: true, gfm: true, renderer: codeRenderer()});
    root.innerHTML = window.DOMPurify.sanitize(html, {
      ALLOWED_TAGS,
      ALLOWED_ATTR,
    });
    scopeIds(root);
    wrapTables(root);
    callouts(root);
    readOnlyTasks(root);
    links(root);
    // The header row and highlighting are rebuilt with the subtree, so clear the
    // module's once-per-root marker before asking it to attach again.
    root.classList.remove('code-header-ready');
    window.CodeHeader?.attach(root, final, !final && hasOpenFence(source));
    const diagrams = final ? [...root.querySelectorAll('pre code.language-mermaid')] : [];
    for (const code of diagrams) diagramSources.set(code, null);
    const ready = final ? Promise.all([renderMath(root), ...diagrams.map(renderDiagram)]) : Promise.resolve();
    return {footnotes: prepared.count || 0, ready};
  }

  document.addEventListener('kova:theme', () => {
    for (const code of document.querySelectorAll('.markdown pre code.language-mermaid')) {
      if (diagramSources.has(code)) renderDiagram(code);
    }
  });
  window.KovaMarkdown = {render, hasOpenFence, outline, plainText};
})();

"use strict";
document.querySelectorAll("[data-theme-value]").forEach(button => {
  button.addEventListener("click", () => {
    document.documentElement.dataset.theme = button.dataset.themeValue;
    document.dispatchEvent(new Event('kova:theme'));
    document.querySelectorAll("[data-theme-value]").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
  });
});
document.querySelectorAll("[data-density-value]").forEach(button => {
  button.addEventListener("click", () => {
    document.documentElement.dataset.density = button.dataset.densityValue;
    document.querySelectorAll("[data-density-value]").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
  });
});
document.querySelectorAll("[data-motion-value]").forEach(button => {
  button.addEventListener("click", () => {
    if (button.dataset.motionValue === "reduced") {
      document.documentElement.dataset.motion = "reduced";
    } else {
      delete document.documentElement.dataset.motion;
    }
    document.querySelectorAll("[data-motion-value]").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
  });
});

const markdownExample = document.querySelector('[data-markdown-example]');
if (markdownExample && window.KovaMarkdown) {
  const source = [
    '# Markdown reference',
    'A paragraph with **strong**, *emphasis*, ~~strikethrough~~, `inline code` and a [link](https://example.com).[^note]',
    '## Lists and headings',
    '- A list item\n- Another item',
    '1. First step\n2. Second step',
    '- [x] Done\n- [ ] Next',
    '### Third level\n#### Fourth level\n##### Fifth level\n###### Sixth level',
    '---',
    '## Quotes and details',
    '> Quoted context stays indented.\n>\n> > A nested quote.',
    ...['NOTE', 'TIP', 'IMPORTANT', 'WARNING', 'CAUTION'].map(kind => `> [!${kind}]\n> A labelled callout.`),
    '<details>\n<summary>Show details</summary>\n\nThe hidden paragraph can contain **Markdown**.\n\n</details>',
    '## Code and tables',
    '```js title="example.js"\nconst total = 3;\ntotal.toFixed(1); // one decimal\n```',
    '| Point | Status |\n| --- | --- |\n| First | Done |\n| Second | Next |',
    '## Equations and diagrams',
    String.raw`Inline math: $a^2 + b^2 = c^2$.`,
    String.raw`$$\sum_{i=1}^{n} i = \frac{n(n+1)}{2}$$`,
    '```mermaid\nflowchart LR\n  accTitle: From draft to ready\n  Draft --> Ready\n```',
    '[^note]: A note under a hairline.',
  ].join('\n\n');
  window.KovaMarkdown.render(markdownExample, source, {final: true}).ready.then(() => {
    markdownExample.dataset.ready = 'true';
  });
}

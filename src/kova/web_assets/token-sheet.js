"use strict";

// Mirrors PAIRS in scripts/build_tokens.py. If you change one, change both.
const PAIRS = [
  ["fg.default", "bg.base", 4.5, "body text on the app background"],
  ["fg.default", "bg.surface", 4.5, "body text on panels"],
  ["fg.default", "bg.raised", 4.5, "body text on raised chips and cards"],
  ["fg.muted", "bg.surface", 4.5, "timestamps and secondary copy"],
  ["fg.inverse", "accent.default", 4.5, "text drawn on a solid accent chip"],
  ["accent.default", "bg.base", 3.0, "the accent edge, icon and activity dot"],
  ["status.danger", "bg.surface", 4.5, "error text"],
  ["status.success", "bg.surface", 4.5, "success text"],
  ["status.warning", "bg.surface", 4.5, "warning text"],
  ["border.strong", "bg.surface", 3.0, "input and focus-ring boundaries"],
  ["border.subtle", "bg.surface", 1.2, "decorative dividers"],
  ["step.think", "bg.base", 4.5, "thinking steps on the trace"],
  ["step.tool", "bg.base", 4.5, "tool-call steps on the trace"],
  ["step.ask", "bg.base", 4.5, "steps that wait for the user"],
  ["step.agent", "bg.base", 4.5, "sub-agent steps on the trace"],
  ["syntax.keyword", "bg.base", 3.0, "code keyword colour"],
  ["syntax.comment", "bg.base", 3.0, "code comment colour"]
];

const TOKENS = [
  ["bg.base", "group-core"], ["bg.surface", "group-core"], ["bg.raised", "group-core"],
  ["bg.overlay", "group-core"], ["fg.default", "group-core"], ["fg.muted", "group-core"],
  ["fg.faint", "group-core"], ["fg.inverse", "group-core"],
  ["border.subtle", "group-core"], ["border.strong", "group-core"],
  ["accent.default", "group-accent"], ["accent.hover", "group-accent"],
  ["accent.pressed", "group-accent"], ["accent.on", "group-accent"], ["accent.soft", "group-accent"],
  ["status.success", "group-status"], ["status.warning", "group-status"],
  ["status.danger", "group-status"], ["status.info", "group-status"],
  ["step.think", "group-status"], ["step.tool", "group-status"],
  ["step.ask", "group-status"], ["step.agent", "group-status"],
  ["syntax.keyword", "group-status"], ["syntax.string", "group-status"],
  ["syntax.number", "group-status"], ["syntax.comment", "group-status"],
  ["syntax.function", "group-status"], ["syntax.type", "group-status"],
  ["syntax.invalid", "group-status"]
];

const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d");

/** Parse a CSS colour to linear-light sRGB, or null when it is translucent/invalid. */
function linearise(value) {
  if (!value) return null;
  const probe = "#010203";
  ctx.fillStyle = probe;
  ctx.fillStyle = value;
  const resolved = ctx.fillStyle;
  if (resolved === probe && value.trim().toLowerCase() !== probe) return null;

  let channels;
  if (resolved.startsWith("#")) {
    let hex = resolved.slice(1);
    if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
    channels = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  } else {
    const parts = resolved.slice(resolved.indexOf("(") + 1, resolved.lastIndexOf(")"))
      .split(/[,\s/]+/).filter(Boolean);
    if (parts.length === 4 && parseFloat(parts[3]) < 1) return null;
    channels = parts.slice(0, 3).map(p => parseFloat(p) / 255);
  }
  return channels.map(c => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)));
}

function luminance(value) {
  const lin = linearise(value);
  if (!lin) return null;
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

function ratio(fg, bg) {
  const a = luminance(fg), b = luminance(bg);
  if (a === null || b === null) return null;
  const hi = Math.max(a, b), lo = Math.min(a, b);
  return (hi + 0.05) / (lo + 0.05);
}

const variable = token => "--" + token.replace(/\./g, "-");
const read = token =>
  getComputedStyle(document.documentElement).getPropertyValue(variable(token)).trim();

function renderTokens() {
  document.querySelectorAll(".grid").forEach(host => { host.textContent = ""; });
  for (const [token, group] of TOKENS) {
    const value = read(token);
    const host = document.getElementById(group);
    if (!host) continue;
    const row = document.createElement("div");
    row.className = "swatch";

    const chip = document.createElement("span");
    chip.className = "chip";
    chip.style.background = value || "transparent";

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = token;

    const shown = document.createElement("span");
    shown.className = "value";
    shown.textContent = value || "\u2014";

    row.append(chip, name, shown);
    row.title = "var(" + variable(token) + ")";
    host.append(row);
  }
}

function renderPairs() {
  const body = document.getElementById("pairs");
  body.textContent = "";
  let failures = 0;

  for (const [fg, bg, minimum, why] of PAIRS) {
    const value = ratio(read(fg), read(bg));
    const tr = document.createElement("tr");

    const label = document.createElement("td");
    label.textContent = why;
    label.title = fg + " on " + bg;

    const sample = document.createElement("td");
    const pair = document.createElement("span");
    pair.className = "pair";
    const fgDot = document.createElement("span");
    fgDot.className = "dot";
    fgDot.style.background = read(fg);
    const bgDot = document.createElement("span");
    bgDot.className = "dot";
    bgDot.style.background = read(bg);
    pair.append(fgDot, bgDot);
    sample.append(pair);

    const ratioCell = document.createElement("td");
    ratioCell.className = "ratio";
    ratioCell.textContent = value === null ? "n/a" : value.toFixed(2) + ":1";

    const needed = document.createElement("td");
    needed.textContent = minimum.toFixed(1) + ":1";

    const verdict = document.createElement("td");
    const badge = document.createElement("span");
    if (value === null) {
      badge.className = "verdict skip";
      badge.textContent = "translucent";
    } else if (value >= minimum) {
      badge.className = "verdict pass";
      badge.textContent = "pass";
    } else {
      badge.className = "verdict fail";
      badge.textContent = "fail";
      failures += 1;
    }
    verdict.append(badge);

    tr.append(label, sample, ratioCell, needed, verdict);
    body.append(tr);
  }

  const summary = document.getElementById("summary");
  summary.textContent = failures === 0
    ? "All " + PAIRS.length + " declared pairs pass in this theme."
    : failures + " of " + PAIRS.length + " declared pairs fail in this theme.";
}

function render() {
  renderTokens();
  renderPairs();
}

document.querySelectorAll(".modes button").forEach(button => {
  button.addEventListener("click", () => {
    document.documentElement.dataset.theme = button.dataset.mode;
    document.querySelectorAll(".modes button").forEach(other => {
      other.setAttribute("aria-pressed", String(other === button));
    });
    render();
  });
});

render();

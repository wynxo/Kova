# The terminal chat: design

How `kova` looks in a terminal, and why. The rules in [DESIGN.md](DESIGN.md) still
hold: the terminal is a renderer of the same semantic tokens, the accent means
activity, and every colour comes from a role in `src/kova/theme.py`.

## What it is for

Kova runs an agent on your own machine. Its promise is that **you see and control
every step while it happens**: what it is thinking, each tool it calls and with
which arguments, what came back and how long it took, what it wants your
permission for, its plan, the work it runs in parallel, and a way to stop or
redirect it at any moment. The terminal design exists to make that promise
visible, and to stay out of the way of the answer itself.

## The signature: the trace

Every turn is drawn as a **trace**: a thin spine down the left edge with one node
per step. A node's shape says what kind of step it is, its colour repeats that in
terminals that have colour, and the right edge holds the step's time.

```
▌ Run the tests and tell me what fails

K Kova
│
∴ thought  The user wants the tests run. First I should see how the…    1.0s
≡ plan     1 of 3 done   Run the test suite
├┬ 2 reads at once                                                       0.1s
│├ list     .                         6 entries                          0.0s
│└ read     pyproject.toml            4 lines                            0.0s
▸ run      ./run-tests -q              exit 1                            0.4s
│          FAILED tests/test_parser.py::test_tabs - assert ['a\tb'] == …
│          2 failed, 46 passed in 0.41s
◆ allowed  edit src/parser.py          once
▸ edit     src/parser.py               +3 −1                             0.0s

  The suite had 46 passed, 2 failed, both in tests/test_parser.py.
  …
  15.2s  6k in  900 out  198 tok/s
```

While a step runs, its node is the only thing on screen in the accent colour, and
it turns: that node *is* the spinner. When the step ends it settles into its kind's
glyph and the time is written down. Nothing else animates.

Everything else stays quiet so the trace is the one memorable thing.

## Step kinds

| Kind | Glyph | ASCII | Role (colour) | Used for |
|---|---|---|---|---|
| thinking | `∴` | `:` | `THINK` (ANSI magenta) | reasoning |
| look | `▹` | `>` | `MEMORY` (`status.info`) | read, list, search, fetch, recall: tools that only read |
| change | `▸` | `>` | `CODE` (`syntax.keyword`) | write, edit, run a command: tools that can change things |
| ask | `◆` | `?` | `WARNING` (`status.warning`) | approvals and questions waiting on you |
| plan | `≡` | `=` | muted text | the checklist |
| group | `├┬` | `+` | muted text | parallel tools and sub-agents, drawn as a tree |
| failed | `✗` | `x` | `ERROR` | any step that failed |
| stopped | `■` | `#` | `WARNING` | a step or turn you stopped |
| live | `◐◓◑◒` | `-\|/` | `ACCENT` | the step running now |

Hollow and solid triangles carry the difference that matters most for control:
a hollow `▹` only looked, a solid `▸` touched your files or ran a command. The
shape carries the meaning, so the trace reads the same at 16 colours, in a
colour-blind palette or with `NO_COLOR`.

## Speakers

Each speaker has a mark and a colour, ready for bots and group chats:

- **You**: a left bar `▌` in `USER`, on a faint tint. The input box uses the same
  bar, so what you type looks like what gets sent.
- **Kova** (or the name you gave it): a one-cell reverse-video tile with its
  initial, `K`, in the accent: the terminal form of the app mark.
- **Bots** (later): the same tile with their own initial, coloured from a fixed
  ring of roles (`identity()` in `tui/render.py`), so a name always gets the same
  colour.

## Palette

No new colours. The roles already exist in every theme and follow its semantic
tokens; one role is added:

| Role | kova-dark | kova-light | 16 colours |
|---|---|---|---|
| text | terminal default | terminal default | default |
| spine, rules | `FAINT` | `FAINT` | bright black |
| `ACCENT` (live only) | `#f6f6f8` | token `accent.default` | bright white / black |
| `THINK` (new) | `#c77dff` (ANSI 5) | light ANSI 5 | magenta |
| `MEMORY` look | `#7aa2ff` | `status.info` | bright blue |
| `CODE` change | `#ffb38a` | `syntax.keyword` | yellow |
| `WARNING` ask | `#ffaf5f` | `status.warning` | yellow |

At 16 colours `CODE` and `WARNING` share yellow; `▸` and `◆` keep them apart.
Text itself is never coloured by kind: only the one-cell node and the step word.

## Layout

Left-aligned, one reading column, time right-aligned at the edge (capped at 104
columns, like prose). At 80 columns the argument column shrinks first, then the
result; the step word and the time never wrap.

The live region at the bottom, from top to bottom:

```
∴ thinking  pyproject.toml lists pytest under the dev group, so…   0.7s   ← live node
≡ 1 of 3  Run the test suite  ▰▰▱                                         ← pinned plan
────────────────────────────────────────────────────────────────────────
▌ Type to queue a message
──────────────────────────────── esc stop   ctrl+o steps   ctrl+t thinking
work  local-model  asks before changes                ctx 4%  ~/projects/demo
```

- **Input**: two rules, no box. Keys for what you can do *right now* sit in the
  bottom rule.
- **Status line**: plain words in columns, no capital-letter chips.
- **Approvals** replace the input: an amber bar down the left, what will run and
  where, a diff when it is an edit, and one key per answer
  (`y` once, `a` this session, `w` always, `n` no). Destructive actions use the
  error colour and say so in one sentence.
- **Pickers and the palette**: a titled rule above, rows, the keys in a rule below.

## Glyphs and fallbacks

Unicode by default. `KOVA_ASCII=1`, a non-UTF-8 output encoding or the Linux
console (`TERM=linux`) switch every glyph above, the rules and the trees to
plain ASCII (`tui/glyphs.py`).

## Review against generic defaults

The first draft was checked against the defaults that make an interface look
templated. What changed and why:

- **Boxes around everything.** The old welcome card, input, plan and dialogs were
  all the same rounded box. Now: rules for things you type into, a left bar for
  things waiting on you, no frame for the transcript.
- **Gradient wordmark.** The half-block logo in a gradient said nothing about the
  product. It is gone; the `K` tile and one line of plain instructions replace it.
- **Capital-letter chips and `a · b · c` meta strings.** `WORK` and the
  middle-dot status line became lowercase words separated by space.
- **Borrowed vocabulary.** Whimsical spinner verbs (*Percolating…*), `✻ Thought for`,
  `⎿` and `⏵⏵` are another product's look. Kova now names what is happening
  (*thinking*, *run*, *waiting for you*) and uses its own trace.
- **A colour per tool.** The first draft gave every tool its own chip colour,
  which is decoration. Colour now encodes four kinds of step, so it carries
  meaning and stays below five hues.
- **A duration bar on every row** (a profiler look) was drawn and cut: the time
  column already says it, and the trace should be the only flourish.

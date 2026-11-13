# Kova Terminal Shortcuts

These keys control the interactive chat (`kova` / `./cli`). Press `?` in the chat
to open this same list. The `?` panel and the `/help` command show them; the table
below lists every key the terminal chat understands.

## Chat keys

| Key | What it does |
| --- | --- |
| `ctrl+p` | Open the command palette (commands, models and recent chats) |
| `!` | Run a shell command |
| `/` | Open the commands menu |
| `@` | Attach a file |
| `#` | Remember a note |
| `esc` | Interrupt Kova / stop the reply; close a menu |
| `esc` `esc` | Clear the input |
| `ctrl+c` | Stop · clear the input · twice to exit |
| `ctrl+d` | Exit (or `ctrl+c` twice) |
| `\⏎` `shift+⏎` `ctrl+j` | New line |
| `shift+tab` | Cycle permission modes |
| `tab` | Complete |
| `↑` `↓` | Earlier messages |
| `ctrl+l` | Clear the screen |

## Reasoning and tools

Toggle live details for the current turn:

| Key | What it does |
| --- | --- |
| `ctrl+t` | Toggle full model reasoning (streamed live) |
| `ctrl+o` | Expand / collapse tool calls and their output |
| `pgup` `pgdn` | Scroll the expanded details |

## Command palette

`ctrl+p` opens a searchable list of slash commands, models and recent chats.
Type to filter (the filter is fuzzy: `/mdl` finds `/model`), move with `↑` `↓`,
press `enter` to run or open the chosen item, and `esc` to close. Commands that
need an argument (such as `/cd`) are put in the input box for you to finish.
Recent chats resume exactly like `/resume`.

## Status line

The line under the input shows the chat mode, model, chat title and permission
mode, then on the right:

- tokens used (the last turn's output, or the live count while Kova is replying)
- `ctx N%` — how full the active model's context window is, estimated from the
  conversation so far
- the turn's elapsed time

On narrow terminals the status line drops the least important parts first
(folder, then stats) and shortens the model name, so it never wraps.

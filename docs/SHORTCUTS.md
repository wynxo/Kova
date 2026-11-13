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

Every key here is remappable under `[tui.keys]` in `config.toml`; see the README.

## While a reply runs

| Action | What it does |
| --- | --- |
| `enter` | Queue the message; it is saved on the server and starts when this reply finishes |
| `/steer <text>` | Join the running reply at its next round instead of waiting behind it |
| `esc` | Stop the reply; messages queued in this session return to the input |

Queued messages are labelled in the live region and come back when you reopen
the chat.

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

## Sessions

`/resume` opens a picker of recent chats: `↑` `↓` and `enter` switch to one,
and `ctrl+s` forks the highlighted chat — a full copy (messages, plan and
compacted context) that continues independently. `/fork` copies the current
chat the same way, and `/fork 2` copies the second chat in the list.

## Status line

The line under the input shows the chat mode, model, chat title and permission
mode, then on the right:

- tokens used (the last turn's output, or the live count while Kova is replying)
- `ctx N%` — how full the active model's context window is, estimated from the
  conversation so far
- the turn's elapsed time

On narrow terminals the status line drops the least important parts first
(folder, then stats) and shortens the model name, so it never wraps.

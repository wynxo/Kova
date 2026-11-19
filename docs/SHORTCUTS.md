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

## Web and desktop app

The browser UI and the desktop app share these keys. Press `Ctrl` `/` (`⌘` `/`
on a Mac) in the app for the same list.

| Key | What it does |
| --- | --- |
| `enter` | Send the message (queues it while a reply runs) |
| `shift+enter` | New line |
| `↑` in an empty box | Bring back your last message |
| `esc` | Stop the reply; close the drawer or a menu |
| `/` | Jump to the message box |
| `ctrl+k` | Command palette: search chats, run commands, open settings |
| `ctrl+shift+k` | New chat |
| `ctrl+shift+n` | New incognito conversation |
| `ctrl+shift+f` | Search chats (the sidebar's search field) |
| `ctrl+b` | Show or hide the sidebar |
| `ctrl+shift+l` | Switch light and dark |
| `ctrl+space` | Focus the message box |
| `ctrl+/` or `?` | Keyboard shortcuts |

`ctrl+k` opens the palette instead of starting a new chat; new conversations
moved to `ctrl+shift+k` so the two do not compete for the same chord.

Every modifier chord in that table comes from one registry; the typing keys
(including the plain `/` and `?`) stay fixed. Settings → General → **Shortcut
keys** lists the registry: click the keys, press the new
combination, and `esc` cancels. A combination another action already uses is
refused, and the row names that action, so two actions can never share a chord.
**Reset** restores one action and **Reset all** restores every default. Changes
are kept in this browser's preferences (`localStorage`) and the shortcuts
dialog and the command palette show the recorded keys at once.

## Command palette (web and desktop)

The palette is one search box over everything the app can do. It opens with
`ctrl+k` (`⌘K` on a Mac). The sidebar has no separate button for it: its one
search field filters chats, and the palette is the way to run anything else,
including every Settings section.

- Type to filter. Matching is fuzzy and word-aware: `nwcn` finds *New
  conversation*, `thm` finds the theme switch.
- Results are grouped: actions, appearance, the current conversation, its
  permissions, reasoning effort, settings, models, MCP servers, extensions,
  recent conversations, and matches found inside messages.
- Typing two or more characters also searches chat content. A hit shows the
  matching line and who said it, so a phrase from an old reply is one Enter
  away from the conversation it came from.
- Highlighting a theme row previews that theme at once; `esc` puts the saved
  one back and `enter` is the only way to keep it.
- Reasoning effort rows are the same levels the composer's model picker offers
  for the current model, including *Automatic*.
- MCP servers and installed extensions have an on/off row, using the same
  endpoints as their settings panels.
- Each row shows its shortcut, so the palette also teaches the keys.
- `↑` `↓`, `pageup` `pgdn`, `home` and `end` move; `enter` runs; `esc` closes.
- The dialog traps focus in its search box, announces the result count, and
  returns focus to where you were when it closes.
- Actions that need a conversation (compact, fork, rename, delete, permissions,
  web search) only appear when one is open. Model rows switch the active model.

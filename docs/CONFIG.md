# Configuration

Kova keeps its settings in one TOML file, `config.toml`, inside the Kova home
folder. The default home is `~/.kova`; set `KOVA_HOME` to move the whole folder
(chats, notes, token and config travel with it).

```sh
kova config path                  # print the exact config.toml path
kova config show                  # every setting, secrets redacted
kova config show --json           # one machine-readable object
kova config set port 7788         # validate, then save one setting
```

The file is written atomically with mode `0600` on Unix because it can hold API
keys and OAuth metadata. `config show` never prints those values: it displays
`<redacted>` (or just a "key set" note in the human table). Base URLs with
embedded `user:password@` userinfo are masked too. Backups and exports strip
credentials too. There is deliberately no "show secrets" switch.

A running server reads `config.toml` at startup. After `kova config set`, restart
it to apply the change:

```sh
kova stop
kova                         # starts a fresh background server
```

`kova doctor` checks that the file parses, its schema is current, provider
references resolve, and its keys are known.

Comments, hand-written keys and formatting are preserved when Kova saves the
file: it edits the document already on disk instead of rebuilding it. Keys this
version does not recognise also survive, and `kova doctor` lists them with the
closest known name so a typo does not silently look like a setting. Warnings
name keys only, never values.

## Set a value

```sh
kova config set compact_threshold 0.9
kova config set memory.budget 12000
kova config set memory.review on
kova config set tui.theme light
kova config set bind lan
```

Keys are the dotted names in the tables below. The value is parsed and checked
against the same schema before anything is written. A bad value exits non-zero
and leaves the file untouched:

```console
$ kova config set compact_threshold 1.5
✗ Invalid value for compact_threshold: "1.5" must be between 0.1 and 0.99.
```

`config set` covers ordinary scalar settings. Model providers have their own
safer commands (`kova providers`, `kova model`) because they involve credentials
and endpoint migration; unknown or structured keys point you there instead of
guessing.

## Editable settings

`Config` field names are shown so the dataclass and the file stay easy to
compare. Defaults are what a fresh install writes.

| TOML key | `Config` field | Default | Allowed | What it changes |
| --- | --- | --- | --- | --- |
| `active` | `active` | `local` | name of a configured model | The model selected for new turns. `kova model use NAME` also updates the server immediately. |
| `port` | `port` | `7700` | 1–65535 | Local server port. The desktop, CLI and `kova-web` use the same value. |
| `compact_threshold` | `compact_threshold` | `0.75` | 0.1–0.99 | Fraction of the model's context that triggers automatic conversation compaction. `/compact` in the terminal does the same thing on demand. |
| `files_path` | `files_path` | `~/Kova Files` | non-empty path | Root for the file tools. Relative paths are resolved when set; every resolved path must stay inside this folder. |
| `bind` | `bind` | `loopback` | `loopback` or `lan` | `loopback` listens only on `127.0.0.1`. `lan` also starts the private-network listener used by paired phones; it never opens the public internet. `kova pair` toggles this too. |
| `[memory] path` | `memory_path` | `~/.kova/memory` | non-empty path | Root folder for Markdown memory notes. An empty value falls back to the default rather than the working directory. `kova memory use PATH` changes it live when the server is up. |
| `[memory] write_subdir` | `memory_write_subdir` | unset | one folder name, or empty to clear | In Obsidian mode, reads cover the whole vault but writes and deletes stay in this subfolder (normally `Kova`). Separators and `..` are refused. |
| `[memory] budget` | `memory_budget` | `8000` | integer ≥ 0 | Character budget for titles, tags and bodies across all notes. `0` disables the cap; over-budget writes are refused rather than truncated. |
| `[memory] index_lines` | `memory_index_lines` | `200` | integer ≥ 0 | How many lines of the always-loaded `index` note may reach the prompt. `0` disables the index text. |
| `[memory] review` | `memory_review` | `false` | `on`/`off` | Opt-in post-turn memory review. `kova memory review on|off` is the friendlier switch. |
| `[memory] review_every` | `memory_review_every` | `12` | integer ≥ 1 | Run review after this many user turns when review is on. |
| `[memory] consolidate` | `memory_consolidate` | `false` | `on`/`off` | Opt-in nightly memory consolidation. `kova memory nightly on|off` is the friendlier switch. |
| `[memory] consolidate_hour` | `memory_consolidate_hour` | `3` | 0–23 | Earliest local hour at which consolidation may start. |
| `[memory] consolidate_min_confidence` | `memory_consolidate_min_confidence` | `0.8` | 0–1 | Per-operation confidence gate for consolidation. |
| `[memory] consolidate_max_loss` | `memory_consolidate_max_loss` | `0.25` | 0–1 | Most of the store's characters one consolidation run may drop. |
| `[tui] theme` | `tui_theme` | `dark` | `dark` or `light` | Terminal palette. `KOVA_THEME` and `KOVA_COLORS` still override at runtime. |
| `[tui.keys] <action>` | `tui_keys` | `{}` | action name and a supported key | Per-action key overrides for the terminal chat; see [TUI keys](#tui-keys). |
| `[persona] name` | `persona_name` | `Kova` | 1–40 printable characters | The assistant's displayed name and the "You are …" identity line. The name is trimmed. |
| `[persona] tone` | `persona_tone` | `warm` | `warm`, `concise`, `professional`, `playful` | One added line in the system prompt and the identity shown in setup. |
| `[persona] configured` | `persona_configured` | `false` | `true`/`false` | Whether onboarding has asked for a name and tone. Usually set by setup, not by hand. |

Booleans accept `on`/`off`, `true`/`false`, `yes`/`no`, `1`/`0`, `enabled` and
`disabled` on the command line. Path values are expanded (`~`) and resolved
before they are saved.

## Structured and internal fields

These fields are part of `Config` and appear in `config show --json`. They are
not set with `config set`, because changing them by hand can break credentials
or model references.

| TOML key | `Config` field | Notes |
| --- | --- | --- |
| `schema_version` | — | Always `2` for the current provider-aware format. `kova doctor` reports older or unknown schemas. |
| `models` | `models` | Selected model aliases. Each entry has `name`, `provider`, `base_url`, `model`, `context` (token budget for history trimming) and `vision` (whether the model accepts images). Manage with `kova model add`, `kova model use` and `kova model list`. |
| `providers` | `providers` | Provider connections. Each has `id`, `name`, `base_url`, `kind`, `adapter`, and optionally redacted-in-output `api_key`, `headers` and `auth`. Manage with `kova providers`, `kova setup` and `kova providers login`. |
| `mcp` | `mcp` | MCP servers whose tools Kova may use, one `[[mcp]]` table each. See [MCP servers](#mcp-servers). Manage with `kova mcp add`, `kova mcp list`, `kova mcp remove` and the **MCP servers** panel in the app. |
| `oauth_host_id` | `oauth_host_id` | A stable generated host identifier used by provider authentication. Leave it in place; changing it makes an owned login look like a new host. |
| — | `home` | The resolved Kova home folder (`KOVA_HOME` or `~/.kova`). It is derived at load time and is not stored in `config.toml`. |
| — | `_extra` | In-memory home for top-level keys this version does not recognise, populated on load. Saves edit the existing document, so unknown keys and comments survive; `kova doctor`'s schema check lists them with suggestions. Unknown keys whose names look like credentials (`private_key`, `access_key`, `cookie`, `headers`, …) have their values redacted by `config show`. |

Older `[[models]]` entries that carry `base_url` and `api_key` still load. On the
next save Kova writes schema version 2 and creates matching `[[providers]]`
entries without changing the active model or the old aliases. Keys, custom
headers and owned OAuth tokens stay in this file and are never copied into a
backup.

## MCP servers

Kova can use the tools of any [Model Context Protocol](https://modelcontextprotocol.io)
server. Each server is one `[[mcp]]` table:

```toml
# A local server Kova starts and talks to over stdin/stdout (stdio).
[[mcp]]
name = "files"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/home/you/projects"]

# Environment variables are passed to the server; use them for tokens.
[[mcp]]
name = "github"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "..." }
disabled_tools = ["delete_repository"]

# A remote server over streamable HTTP.
[[mcp]]
name = "docs"
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ..." }
enabled = false
```

| Key | Default | What it does |
| --- | --- | --- |
| `name` | required | Letters, digits, `-` and `_`, at most 32 characters, no `__`. Tools appear to the model as `<name>__<tool>`. |
| `command` | — | Program to start for a **stdio** server. Give exactly one of `command` or `url`. |
| `args` | `[]` | Arguments for `command`. |
| `env` | `{}` | Extra environment variables for a stdio server, on top of Kova's own. |
| `url` | — | Address of a **streamable HTTP** server (`http://` or `https://`). |
| `headers` | `{}` | HTTP headers sent with every request, such as `Authorization`. |
| `enabled` | `true` | `false` keeps the entry but stops the server and hides its tools. |
| `disabled_tools` | `[]` | Tool names (as the server spells them) the model never sees. The app's per-tool switches edit this list. |
| `sandbox` | `false` | Start a stdio server inside the Linux Landlock sandbox: it may write only to `KOVA_HOME/mcp/<name>` and temporary space. Without Landlock the server is not started. Most `npx`/`uvx` servers need to write to their package cache, so this is opt-in. |

A stdio server runs in its own folder, `KOVA_HOME/mcp/<name>`, so relative paths
in `args` resolve there. Use absolute paths for anything else.

The usual way to add one is the CLI, which writes the table through the running
server and connects at once:

```sh
kova mcp add files -- npx -y @modelcontextprotocol/server-filesystem ~/projects
kova mcp add github --env GITHUB_PERSONAL_ACCESS_TOKEN=... -- npx -y @modelcontextprotocol/server-github
kova mcp add docs --url https://example.com/mcp --header "Authorization=Bearer ..."
kova mcp list              # servers, state and tool counts
kova mcp test github       # reconnect now and list its tools
kova mcp remove docs       # stop it and forget it
```

In the app, **MCP servers** in the sidebar lists every server with its tools,
an on/off switch for each server and tool, **Always allow** per tool, a **Test**
button and **Remove**. Servers that need a token are best added with the CLI, so
the token never passes through the browser.

How the tools behave:

- **Approval.** Kova cannot see what an MCP tool does, so every call shows an
  approval card in Manual and Auto (Full access runs without asking). **Always
  allow** in the app adds the permission rule
  `{"action": "allow", "tool": "github__create_issue"}`; you can also write
  such rules yourself, for example `"tool": "files__*"` (see
  [permission-rules.md](permission-rules.md)). Removing a server removes the
  rules for its tools.
- **Work mode only.** Chat mode offers only web tools, so MCP tools are used in
  work mode.
- **Crashes.** If a stdio server exits, the call it was running fails (it is not
  repeated), and the next call starts the server again, up to three times a
  minute. `kova mcp test NAME` reconnects it on demand.
- **Secrets.** `env` and `headers` values are redacted by `kova config show`,
  never returned to the app, and stripped from backups.

A server whose tool list changes while running shows the new tools after
`kova mcp test NAME` or a restart.

## TUI keys

`tui.keys` maps a terminal-chat action to a key name:

```toml
[tui]
theme = "light"

[tui.keys]
palette = "ctrl-g"
thinking = "ctrl-p"
```

Set one override with the command line:

```sh
kova config set tui.keys.palette ctrl-g
```

Supported actions are `shortcuts`, `palette`, `thinking`, `tools`, `scroll_up`,
`scroll_down`, `cancel`, `interrupt`, `exit`, `permissions`, `clear`, `suspend`,
`up`, `down`, `complete`, `submit`, `newline`, `backspace`, `delete`, `left`,
`right`, `home`, `end`, `word_left`, `word_right`, `kill_end`, `kill_start`,
`kill_word`, `kill_word_right`, `yank` and `next`.

Bindings must be unique. Supported key names are the defaults in
`src/kova/tui/keymap.py` plus `ctrl-g`, `ctrl-r`, `ctrl-v` and `ctrl-x`.
Setting an action back to its default removes the override. Typed command
prefixes (`/`, `!`, `#`, `@`) are not keybindings.

## Complete example

```toml
schema_version = 2
oauth_host_id = "urn:uuid:00000000-0000-0000-0000-000000000000"
active = "local"
port = 7700
compact_threshold = 0.75
files_path = "/home/you/Kova Files"
bind = "loopback"

[memory]
path = "/home/you/.kova/memory"
budget = 8000
index_lines = 200
review = false
review_every = 12
consolidate = false
consolidate_hour = 3
consolidate_min_confidence = 0.8
consolidate_max_loss = 0.25

[tui]
theme = "dark"

[tui.keys]
palette = "ctrl-g"

[persona]
name = "Kova"
tone = "warm"
configured = false

[[providers]]
id = "local"
name = "local"
base_url = "http://127.0.0.1:11434/v1"
kind = "custom"
adapter = "openai"
headers = {}
auth = {}

[[models]]
name = "local"
provider = "local"
base_url = "http://127.0.0.1:11434/v1"
model = "MODEL"
context = 32768
vision = false
```

The example omits `api_key`; when one is present, `config show` still prints
`<redacted>` instead of the value.

## Troubleshooting

- **`config.toml` is not TOML.** `kova config show` and most commands report one
  error line. Fix the syntax, then run `kova doctor`.
- **A setting is ignored.** Check its spelling with `kova config show`; the
  schema check in `kova doctor` names unknown keys and their closest matches.
  Unknown keys are preserved, not deleted, so you can fix the name without
  losing the value.
- **A port change did nothing.** The old server is still running. Run `kova
  stop`, then start Kova again.
- **An MCP server shows "error".** `kova mcp test NAME` prints the reason,
  including the last lines the server wrote to stderr. Check the command runs
  in a terminal, that tokens in `env`/`headers` are set, and that `npx`/`uvx`
  is installed. Fix the entry, then test again.
- **A provider disappeared.** Provider references must resolve. Re-add it with
  `kova providers add` or reconnect the model with `kova setup`.
- **Keys are exposed.** They should not be: `config show` redacts them. If you
  pasted a real key into an issue, chat or screenshot, rotate it with the
  provider and run `kova providers login CONNECTION`.

See also [ARCHITECTURE.md](ARCHITECTURE.md) for how config fits into the server
and [CONTRIBUTING.md](../CONTRIBUTING.md) for the invariants around tokens and
keys.

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
| `[local] idle_unload_minutes` | `local_idle_unload_minutes` | `5` | integer ≥ 0 | Unload the managed llama.cpp engine after this many idle minutes; `0` disables idle unload. Active model streams are protected; the next chat reloads it automatically. Restart the Kova server after changing this setting. |
| `[privacy] local_only` | `privacy_local_only` | `false` | boolean | Refuse non-loopback Kova traffic. Use `kova privacy --local-only on` or the Privacy panel to apply immediately; direct config edits require a restart. |
| `[privacy] secrets` | `privacy_secrets` | `off` | `off`, `redact` or `block` | Opt-in outbound secret guard at the model boundary. `off` keeps previous behaviour; `redact` replaces known credential shapes before a non-loopback request; `block` refuses the turn before any request. |
| `[usage] monthly_budget_usd` | `usage_monthly_budget_usd` | `0` (no budget) | number ≥ 0, in US dollars | Once this month's estimated spend passes 80% of it, the next answer shows a warning, at most once a day. It never blocks a turn. `kova usage` shows the spend; see [Usage and cost](#usage-and-cost). |
| `[pricing] "<model>"` | `pricing` | built-in table | `{ input = N, output = N }`, USD per million tokens | Your own prices, by Kova model name, model id or `kind/model-id`. Edit `config.toml` by hand; `config set` does not write it. Overrides beat the built-in table, so `0` makes a free server free. |
| `[catalog] url` | `catalog_url` | unset (Kova's index on GitHub) | an `https://` URL, a local path, `bundled`, or empty to clear | Where `kova catalog` and the Extensions page read the extensions index. When it cannot be read, Kova uses the index bundled with it. See [EXTENSIONS.md](EXTENSIONS.md). |
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
| `[tui] theme` | `tui_theme` | `dark` | `dark`, `light` or a theme id from `kova themes list` | Terminal palette, from the same theme data as the app. `kova themes use <id>` sets it. `KOVA_THEME` and `KOVA_COLORS` still override at runtime. |
| `[tui.keys] <action>` | `tui_keys` | `{}` | action name and a supported key | Per-action key overrides for the terminal chat; see [TUI keys](#tui-keys). |
| `[persona] name` | `persona_name` | `Kova` | 1–40 printable characters | The assistant's displayed name and the "You are …" identity line. The name is trimmed. |
| `[persona] tone` | `persona_tone` | `warm` | `warm`, `concise`, `professional`, `playful` | One added line in the system prompt and the identity shown in setup. |
| `[persona] configured` | `persona_configured` | `false` | `true`/`false` | Whether onboarding has asked for a name and tone. Usually set by setup, not by hand. |
| `[voice] stt` | `voice_stt` | `local` | `local` or `browser` | Speech-to-text backend. `local` uses the bundled Whisper model on the server; `browser` transcribes from the desktop web UI. |
| `[voice] tts` | `voice_tts` | `espeak-ng` | `espeak-ng` or `off` | Text-to-speech backend for spoken replies. `off` disables spoken output. |
| `[voice] language` | `voice_language` | unset | an ISO language code, or empty to clear | Language for speech-to-text and text-to-speech. Defaults to the model's primary language when unset. |
| `[voice] max_seconds` | `voice_max_seconds` | `60` | integer ≥ 1 | Longest voice clip the server accepts, in seconds. |

The desktop shell reads two keys the server never interprets. `[desktop] quick_ask`
is the Electron global-shortcut accelerator for the Quick ask overlay, default
`CommandOrControl+Shift+Space`. It is an Electron accelerator string (for example
`"CommandOrControl+Shift+Space"` or `"Alt+Q"`); a missing or malformed value falls
back to the default. `[desktop] title_bar` is `native` (the default) or `themed`:
`themed` keeps each OS's own window controls but lets the app paint its title
strip from the active theme. Any other value stays native. The server recognises
the section so `kova config` and the doctor never flag it as an unknown key.

Booleans accept `on`/`off`, `true`/`false`, `yes`/`no`, `1`/`0`, `enabled` and
`disabled` on the command line. Path values are expanded (`~`) and resolved
before they are saved.

## Usage and cost

Every answer records its tokens in and out, the time to the first token, the
total time and the model's speed in tokens per second. The web app shows them
under each answer (`1.2k in · 340 out · ~$0.004 · 38 tok/s`; hover for the
timings), and `kova usage` adds them up by day and by model:

```sh
kova usage                 # the last 30 days
kova usage --days 7 --json # for scripts
kova status                # includes today's spend
```

Token counts come from the model server. OpenAI-compatible servers are asked
for them with `stream_options.include_usage`; one that rejects the option is
asked again without it. When a server sends no counts, Kova estimates about four
characters a token and marks the numbers with `~`.

Cost is an estimate, never a bill. A model on this PC, a `local` provider or a
loopback address costs `$0.00 local`. Known cloud models use a small built-in
table (`kova.usage.PRICING`, with the date it was checked); anything else shows
tokens only and never a made-up price. Cached prompt tokens are counted at
the full input price, so a provider's cache discount makes the real bill
lower than the estimate. Prices change, so set your own:

```toml
[pricing]
"gpt-4o" = { input = 2.5, output = 10 }        # USD per million tokens
"my-lab-model" = { input = 0, output = 0 }     # a Kova model name works too

[usage]
monthly_budget_usd = 20
```

Usage stays in `kova.db` on this machine (see
[ARCHITECTURE.md](ARCHITECTURE.md#usage-and-cost) for how it is measured). Incognito chats record none, and
deleting a chat deletes its usage.

## Outbound secret protection

`privacy.secrets` is an opt-in guard around text Kova sends to a model. It is
off by default so existing installs behave exactly as before.

```sh
kova config set privacy.secrets redact   # replace likely credentials
kova config set privacy.secrets block    # refuse the turn
kova config set privacy.secrets off      # the default
```

The guard runs in `ModelClient`, the shared boundary for OpenAI-compatible,
Anthropic and Responses traffic, on every model round. It scans system, user,
assistant and tool-result text on a deep copy of the request. Persisted chats,
tool-call arguments and authentication headers are never rewritten.

- `redact` replaces known shapes with deterministic placeholders such as
  `[REDACTED:openai_key:3f9a]`. The UI reports how many likely secrets were
  replaced and their kinds, never the values. A follow-up request is scanned
  again, and a placeholder is never turned back into a secret.
- `block` refuses the turn with a clear error before any request is made and
  keeps the user's stored message unchanged.
- There is deliberately no `warn` mode: sending a likely secret without an
  explicit decision would defeat the guard. Hand-edited unknown values are
  treated as `block`; `kova config set` rejects them.

Only a literal loopback provider bypasses the guard: `127.0.0.0/8`, `::1` and
the exact name `localhost` in the parsed hostname. A LAN name, a public hostname
or an address that merely contains `127.0.0.1` is remote, and no DNS lookup is
performed. When a scan hits its size or finding cap, the unscanned remainder is
removed in `redact` mode and `block` mode refuses the request, so an incomplete
scan is never treated as clean.

This is a safety net, not a guarantee. Detection covers the known shapes in
`src/kova/secrets.py` (provider tokens, private-key blocks, JWTs and bearer
tokens, credential URLs and secret-like `.env` assignments); an unusual or
rotated credential shape can still be missed. Do not rely on it as the only
control for a hosted provider, and review what a tool read before sending it.

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

The composer's **Make default** saves the selected model alias as `active`
and its optional default `reasoning_effort` on that model entry. For a
local or custom Chat Completions endpoint, Kova cannot infer supported
values from the model ID. If the endpoint accepts `reasoning_effort`,
declare only the values it supports, for example
`reasoning_efforts = ["low", "medium", "high"]` under its `[[models]]` entry.
Include `"off"` only if the endpoint accepts the wire value `"none"`.

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

In the app, **Settings → Extensions and MCP → MCP servers** lists every server with its tools,
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

[privacy]
secrets = "off"
local_only = false

[tui]
theme = "dark"

[tui.keys]
palette = "ctrl-g"

[persona]
name = "Kova"
tone = "warm"
configured = false

[voice]
stt = "local"
tts = "espeak-ng"
max_seconds = 60

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

## Managed local models

When Ollama is not running, **Use a model on this PC** in setup or desktop
**Settings → Models and providers → Connect** offers a managed llama.cpp engine. Nothing downloads until
you choose a model and confirm. GPU builds use Vulkan on Linux/Windows or
Metal on Apple Silicon; explicit CPU mode offers the small 3B model with a
speed warning. Unsupported platforms and failed GPU loading show a next step
instead of claiming the model is ready. Install a working Vulkan/Metal driver,
choose CPU mode, or install/start Ollama and recheck.

Kova pins [llama.cpp b12387](https://github.com/ggml-org/llama.cpp/releases/tag/b12387)
and the GGUF repository revisions in `engine_downloads.py`. Both engine archives
and GGUFs require an exact length and SHA-256 match before use. Engine archives
and their shared libraries live in `KOVA_HOME/engine/`, GGUFs in
`KOVA_HOME/models/`. Interrupted downloads keep `.part` files and use HTTP Range
on retry; a server ignoring Range replaces rather than appends the partial file.
There is no automatic download, binary update, or cloud fallback during chat.

The engine binds only to a free `127.0.0.1` port and requires a random API key
stored with its local provider. Context is bounded to 4096 tokens (2048 for a
tight fit); only a model that passes the VRAM reserve check is fully offloaded.
CPU mode uses zero GPU layers. `engine.pid` and `engine.log` sit beside
`server.pid`; diagnostics remain local. PID recovery verifies process birth
time, executable and full arguments before signalling anything.

```sh
kova config set local.idle_unload_minutes 5
kova stop                              # stops Kova and its managed engine
kova model remove-local PROVIDER       # deletes GGUF and connection
```

The desktop **Remove** action does the same thing. A GGUF shared by another
managed CPU/GPU variant is retained until its last connection is removed.
The engine bundle stays cached. Restart and shutdown stop an owned old process;
idle unload frees it after the configured interval, and the next chat starts
it again without downloading. Disk fit includes model bytes, engine archive,
extraction headroom and a reserve. Low disk reports the estimated exact
shortfall; unreachable hosts report retry/manual Ollama instructions.

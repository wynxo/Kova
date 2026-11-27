# Extensions

Kova stays a lean private AI; extra abilities come as **plugins** (bundles of
skills, slash commands, MCP servers and permission rules, see
[ARCHITECTURE.md](ARCHITECTURE.md#plugins)), **skills** (one Markdown file
of instructions) and **bots** (a ready-made assistant with its own persona,
tools and notes). The extensions catalog is where you find them.

## Use it

```sh
kova catalog search                 # everything
kova catalog search clipboard       # names, descriptions and tags
kova catalog search --kind skill
kova catalog search --kind bot      # ready-made bots
kova catalog install tutor          # adds @tutor; then: kova chat --bot tutor
kova catalog show morning-briefing  # what it is, what it may do, source, checksum
kova catalog install clipboard      # verify, show what it adds, ask, install
kova catalog install clipboard@0.1.0  # insist on that exact version
kova catalog install clipboard --upgrade
kova plugin install clipboard       # a bare name also goes to the catalog

kova skills list                    # every skill the model sees, and where it came from
kova skills show "Explain an error"
kova skills rm explain-error
```

In the desktop and web app, open **Marketplace** at the bottom of the sidebar
(or *Marketplace* in the command palette). **Settings → Extensions and MCP →
Extensions and skills** opens the same window on Installed. Its tabs:

- **Discover**: everything in the catalog, grouped as Bots, Plugins and
  Skills, each card saying what kind it is and whether it is installed.
- **Installed**: your plugins (switch on/off, see what each adds, remove) and
  every skill with its origin. Skills Kova learned, catalog skills and skills
  you put in the skills folder can be read, edited and deleted. Plugin skills
  belong to their plugin: disable or remove the plugin instead.
- **Plugins**, **Skills** and **Bots**: the catalog. **Install** downloads the
  extension, checks its checksum and shows exactly what it adds; nothing is
  written until you press **Install** again. Installing or switching on a
  plugin is refused from a paired phone, because it can choose programs to
  run on your computer. An installed bot appears in **Kova Bots** and is
  edited or removed there, like any bot you made.

Skill origins:

| Origin | Where it lives | Can you edit or delete it? |
| --- | --- | --- |
| Learned by Kova | `~/.kova/skills/Kova/` | yes |
| From the catalog | `~/.kova/skills/catalog/` | yes |
| Added by you | anywhere else in `~/.kova/skills/` | yes |
| Plugin | inside the plugin's folder | no; disable or remove the plugin |

## Bots from the catalog

A catalog bot is a bot folder (`bot.toml`, `persona.md`, optionally one avatar
image) like the ones in `~/.kova/bots/`: data, never code. Before you confirm,
Kova shows the tools it may use, the tools it may never use, its approval
level, mode, model, memory settings and its whole persona.

Two things are always changed on the way in, and the confirmation says so:

- **It asks first.** A downloaded bot's approval ceiling is capped at
  `manual`, whatever its file says. Raise it in the bot's settings if you
  trust it.
- **No foreign working folder.** `permissions.folder` is a path on the
  author's machine, so it is dropped; the bot uses its own folder in Kova Files.

Only the definition, the persona and the avatar the definition names are kept;
any other file in the download is discarded. An avatar that is missing, over
256 KB or not really a PNG, JPEG or WebP is dropped. Installing never replaces
a bot you already have: a taken id or `@handle` is refused. Updating a bot that
came from the catalog replaces its definition and persona but keeps the
approval level and working folder you chose, and its own notes. Versions are
recorded in `~/.kova/bots/.catalog.json`.

## Trust

- **Pinned:** each catalog entry names one version and the SHA-256 of its
  source. A download whose checksum, plugin name or version differs from the
  index is refused before anything is shown or written.
- **Shown:** the confirmation lists the source, the checksum and everything
  the extension adds (MCP server command lines and URLs, skills, slash
  commands, permission rules; for a skill, its full instructions). MCP `env`
  values may hold secrets, so only their names are shown.
- **Confirmed:** nothing is installed, and nothing runs, until you say yes.
  Plugin tools still go through the normal permission prompts and sandbox.
- **Archives** may contain regular files and folders only: links, devices,
  absolute paths and `..` are refused, and downloads are capped at 20 MB.

The checksum proves the download is the one the index describes. It does not
make an extension safe: read what it adds before you say yes.

## Where the index comes from

`[catalog] url` in `config.toml` (`kova config set catalog.url ...`) picks the
index. Without it, Kova reads the index in this repository on GitHub. If that
cannot be read (offline, a typo, a broken file), Kova uses the copy bundled
with it and says so. `kova config set catalog.url bundled` uses only the
bundled copy and never touches the network.

The first-party extensions live in `src/kova/catalog_data/`, so they install
offline:

| Name | Kind | What it does |
| --- | --- | --- |
| `clipboard` | plugin | `/clip` and a skill to read or write the clipboard through `run_command` (#91) |
| `morning-briefing` | plugin | `/briefing`: reminders, overnight jobs, memory changes and a focus (#95) |
| `file-watcher` | plugin | coming soon: needs a watch job kind in Kova first (#108) |
| `explain-error` | skill | turn an error or log line into cause and next step |
| `weekly-review` | skill | a short weekly review conversation |
| `translator` | bot | translates and keeps the tone; web and its own notes only |
| `tutor` | bot | teaches step by step, checks understanding, can set practice reminders |
| `critic` | bot | finds the weak spots in a plan or draft; reads, never writes |

## Index format

```json
{
  "schema": 1,
  "extensions": [
    {
      "name": "clipboard",
      "kind": "plugin",
      "version": "0.1.0",
      "description": "One or two sentences.",
      "author": "Kova",
      "homepage": "https://example.com/clipboard",
      "source": {
        "url": "https://example.com/clipboard-0.1.0.tar.gz",
        "sha256": "64 lowercase hex characters"
      },
      "permissions": ["Plain-language lines: what it may read, run or reach."],
      "tags": ["clipboard", "productivity"],
      "status": "available"
    }
  ]
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | lowercase slug; for a plugin it must equal the manifest's `name`, for a bot its `id` |
| `kind` | yes | `plugin`, `skill` or `bot` |
| `version` | yes | the one version this index pins; must equal the plugin manifest's `version` (a bot file has no version, so the index's is recorded) |
| `source.url` | if available | `https://` archive (`.tar.gz`, `.tgz`, `.zip`) for a plugin or bot, `https://` Markdown file for a skill, a path relative to the index, or `bundled:<path>` inside `src/kova/catalog_data/` |
| `source.sha256` | if available | SHA-256 of the downloaded bytes; for a bundled plugin or bot folder, the folder checksum below |
| `description`, `author`, `homepage` | no | shown in search and on the card |
| `permissions` | no | a plain-language summary; the real list is read from the plugin itself at install time |
| `tags` | no | words for search |
| `status` | no | `available` (default) or `coming-soon` (listed, not installable) |
| `issue` | no | the Kova issue that tracks a coming-soon entry |

Entries Kova cannot read are skipped, not fatal. A `schema` newer than this
Kova understands is refused with a hint to update.

## Publish an extension

1. Make the plugin folder with its `kova-plugin.toml` (see
   [ARCHITECTURE.md](ARCHITECTURE.md#plugins)), the skill's `.md` file with
   `name` and `description` front matter, or a bot folder (the easiest way is
   to make the bot in Kova and copy `~/.kova/bots/<id>/`).
2. For a plugin or bot, pack it: `tar czf myplugin-0.1.0.tar.gz myplugin/`.
3. Get the checksum: `sha256sum myplugin-0.1.0.tar.gz`, or
   `uv run python -m kova.catalog hash PATH` (which also hashes a folder the way
   `bundled:` sources are checked).
4. Add the entry to an index and point `[catalog] url` at it.

A new version is a new archive with a new checksum. Bundled folders are
checked byte for byte, so `.gitattributes` keeps their line endings as they
are; `tests/test_catalog.py` fails when a bundled checksum is stale.

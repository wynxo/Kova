# Extensions

Kova stays a lean private AI; extra abilities come as **plugins** (bundles of
skills, slash commands, MCP servers and permission rules, see
[ARCHITECTURE.md](ARCHITECTURE.md#plugins)) and **skills** (one Markdown file
of instructions). The extensions catalog is where you find them.

## Use it

```sh
kova catalog search                 # everything
kova catalog search clipboard       # names, descriptions and tags
kova catalog search --kind skill
kova catalog show morning-briefing  # what it is, what it may do, source, checksum
kova catalog install clipboard      # verify, show what it adds, ask, install
kova catalog install clipboard@0.1.0  # insist on that exact version
kova catalog install clipboard --upgrade
kova plugin install clipboard       # a bare name also goes to the catalog

kova skills list                    # every skill the model sees, and where it came from
kova skills show "Explain an error"
kova skills rm explain-error
```

In the desktop and web app, **Settings → Extensions and MCP → Extensions and skills** has three tabs:

- **Installed**: your plugins (switch on/off, see what each adds, remove) and
  every skill with its origin. Skills Kova learned, catalog skills and skills
  you put in the skills folder can be read, edited and deleted. Plugin skills
  belong to their plugin: disable or remove the plugin instead.
- **Plugins** and **Skills**: the catalog. **Install** downloads the
  extension, checks its checksum and shows exactly what it adds; nothing is
  written until you press **Install** again. Installing or switching on a
  plugin is refused from a paired phone, because it can choose programs to
  run on your computer.

Skill origins:

| Origin | Where it lives | Can you edit or delete it? |
| --- | --- | --- |
| Learned by Kova | `~/.kova/skills/Kova/` | yes |
| From the catalog | `~/.kova/skills/catalog/` | yes |
| Added by you | anywhere else in `~/.kova/skills/` | yes |
| Plugin | inside the plugin's folder | no; disable or remove the plugin |

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
| `name` | yes | lowercase slug; for a plugin it must equal the manifest's `name` |
| `kind` | yes | `plugin` or `skill` |
| `version` | yes | the one version this index pins; must equal the plugin manifest's `version` |
| `source.url` | if available | `https://` archive (`.tar.gz`, `.tgz`, `.zip`) for a plugin, `https://` Markdown file for a skill, a path relative to the index, or `bundled:<path>` inside `src/kova/catalog_data/` |
| `source.sha256` | if available | SHA-256 of the downloaded bytes; for a bundled plugin folder, the folder checksum below |
| `description`, `author`, `homepage` | no | shown in search and on the card |
| `permissions` | no | a plain-language summary; the real list is read from the plugin itself at install time |
| `tags` | no | words for search |
| `status` | no | `available` (default) or `coming-soon` (listed, not installable) |
| `issue` | no | the Kova issue that tracks a coming-soon entry |

Entries Kova cannot read are skipped, not fatal. A `schema` newer than this
Kova understands is refused with a hint to update.

## Publish an extension

1. Make the plugin folder with its `kova-plugin.toml` (see
   [ARCHITECTURE.md](ARCHITECTURE.md#plugins)), or the skill's `.md` file with
   `name` and `description` front matter.
2. For a plugin, pack it: `tar czf myplugin-0.1.0.tar.gz myplugin/`.
3. Get the checksum: `sha256sum myplugin-0.1.0.tar.gz`, or
   `uv run python -m kova.catalog hash PATH` (which also hashes a folder the way
   `bundled:` sources are checked).
4. Add the entry to an index and point `[catalog] url` at it.

A new version is a new archive with a new checksum. Bundled folders are
checked byte for byte, so `.gitattributes` keeps their line endings as they
are; `tests/test_catalog.py` fails when a bundled checksum is stale.

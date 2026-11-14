# Instructions for AI agents working on Kova

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first.

## This repository is public: never leak the machine you run on

Everything you write here is public and permanent: code, tests, docs, screenshots,
commit messages, **issue and PR titles, bodies and comments**. Git history and
GitHub's "edited" history keep old versions even after a fix.

Never include, even as "evidence" or "real output":

- home folders or usernames: `/home/<name>`, `/Users/<name>`, `C:\Users\<name>`
- shell prompts or window titles like `<user>@<host>:~`
- hostnames, device names, worktree or agent temp paths (`~/.t3/worktrees/…`, `/tmp/claude-…`)
- home-network addresses (`192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`) or the names/ports of
  the maintainer's own model servers
- hardware/system details of the machine (CPU model, RAM, disk, OS edition, uptime)
- anything personal: names, emails, locations, private projects, notes

Use placeholders instead:

| Real | Write |
|---|---|
| home folder | `~/…` or `/home/you/…` |
| prompt / window title | `you@demo:~/projects/demo$` |
| project folder | `~/projects/demo` |
| LAN address | `192.0.2.10` (documentation range) |
| model server | `homelab:8080`, `local-model` |

When pasting command output, logs or test results, **rewrite these parts first**.
Screenshots: use a throwaway `KOVA_HOME`, a neutral prompt and a stub model, then strip
metadata (`exiftool -all= file.png`).

`python scripts/privacy_check.py` checks the repository (CI runs it, plus the PR title and
body). A made-up private IP used as an example must be listed in `.privacy-allow`.

## Before opening a PR

- `uv run pytest -q` passes.
- `python scripts/privacy_check.py` passes.
- The PR description contains no real paths, prompts, addresses or machine details.

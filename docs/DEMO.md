# Two-minute judge demo

One Python server, shared chats across terminal and web, explicit approvals, and
reviewable file changes. Budget **two minutes after setup** with a responsive,
tool-capable model. Model latency can extend this; rehearse before presenting.
These Bash commands assume a source checkout with uv installed.

## Prepare once, before the clock

Use a fresh home and spare port, not your personal Kova data. If port 7788 is
occupied, choose another before starting. Keep this shell open for the whole demo.

```sh
export KOVA_HOME="$(mktemp -d "${TMPDIR:-/tmp}/kova-judge.XXXXXX")"
export KOVA_DEMO_REPO="$PWD"
uv run python - <<'PY'
from kova.config import load_config, save_config
cfg = load_config()
cfg.port = 7788
cfg.files_path = cfg.home / 'workspace'
cfg.files_path.mkdir(parents=True, exist_ok=True)
for name in ('one.txt', 'two.txt'):
    (cfg.files_path / name).write_text('before\n')
save_config(cfg)
print('Demo home:', cfg.home)
PY
uv run kova setup
```

Connect an already-running local tool-capable model or your own provider. Setup
and model download are not part of the two minutes. Do not display API keys.
In a second terminal, export the **same printed KOVA_HOME** and work from the same
checkout. Queue a reminder just before starting the clock:

```sh
uv run kova reminders add "in 1 min" "Judge demo: stand up"
cd "$KOVA_HOME/workspace"
uv run --project "$KOVA_DEMO_REPO" kova --new --mode work
```

## The two-minute script

1. **0:00–0:25 · Show consent.** In the terminal chat, type:

   ```text
   /permissions manual
   Run a command that prints only "hello judge". Do not modify any files.
   ```

   Inspect the proposed command and working folder before allowing it once.
   Narrate: “The model proposes an action; I approve the actual command.”
   If it answers without a tool, ask it explicitly to use `run_command`.

2. **0:25–0:55 · Show reversible changes.** Type:

   ```text
   Read one.txt and two.txt in the configured files folder. Change both to contain only "after" followed by a newline. Use the file tools.
   /changes
   ```

   Approve the proposed writes. Use `/diff N` for the resulting change-set number,
   then `/restore N` (replace `N` with the number actually shown), review the
   preview and confirm. Do not assume the ID is 1. In the second terminal:

   ```sh
   cat "$KOVA_HOME/workspace/one.txt" "$KOVA_HOME/workspace/two.txt"
   ```

   Both should show `before`. If no change set was recorded, say so; do not claim
   a restore happened.

3. **0:55–1:20 · Show explicit memory.** Ask:

   ```text
   Save a memory note: my judge-demo project is called Paper Lantern. Use the memory tool, then tell me when it is saved.
   ```

   Approve if prompted. In the second terminal, verify with:

   ```sh
   uv run kova memory list
   ```

   Exit the chat with `/exit`, reopen from the workspace with
   `uv run --project "$KOVA_DEMO_REPO" kova --new --mode work`, and ask
   “What is my judge-demo project called?” Show the new chat, not just the earlier
   reply. The expected answer is Paper Lantern only if the save succeeded.

4. **1:20–1:40 · Show the background server.** The queued reminder should be due.
   Explain that exiting the chat did not stop the server. Keep a client open to
   receive its reminder event; do not promise an OS notification with every client
   closed. Verify stored reminders if needed:

   ```sh
   uv run kova reminders list
   ```

5. **1:40–2:00 · Show the same conversation in another client.** From the second
   terminal:

   ```sh
   uv run kova-web
   ```

   Open the conversation just created, then narrow the browser to phone width.
   Show the shared history and sidebar drawer. Narrate: “Different clients, the
   same local server and data.” Electron is optional: `uv run kova-app`.

## Slow model or unavailable provider

Stop waiting after one clear attempt and show the
[screenshot gallery](screenshots/README.md):
[web chat](screenshots/web-chat-1440x900-dark.png),
[phone approval](screenshots/web-approval-360x740-dark.png),
[terminal reply](screenshots/tui-after-reply.png), and
[Electron start screen](screenshots/desktop-starting.png).
These are existing captures, not a recording of today's demo. The gallery notes
that the web screenshots used a local stub model; they demonstrate UI states,
not real-model reliability or measured speed. No prerecorded GIF is supplied.

## Reset for the next judge

Close demo clients, then stop **this demo home** from the original shell:

```sh
cd "$KOVA_DEMO_REPO"
uv run kova stop
printf 'Finished demo data retained at %s\n' "$KOVA_HOME"
unset KOVA_HOME
```

Repeat “Prepare once” to get a new directory, fresh chats and fresh files. This
retains the previous demo for inspection and never deletes your personal data.
If stop cannot verify the process, stop the known demo server manually before
reusing its port. Do not reset a home while its server is still running.

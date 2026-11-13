# Skills and scheduled jobs

## Reusable skills

Skills are procedural memory: how to do a repeatable task, not facts about the
user. In **work mode**, Kova sees the slug and short description of every valid
skill at the start of each turn. Discovery reads only YAML front matter. The
Markdown body is read only when the model calls `skill_read` for that slug.
Chat mode does not expose these work tools.

Ask Kova to save a procedure it has successfully used. It can call:

- `skill_add`: create a new skill without overwriting another one.
- `skill_list`: refresh the list of descriptions without reading bodies.
- `skill_read`: load a procedure when relevant.
- `skill_patch`: update one exact, unique text span after reading the skill;
  optionally revise its description too.

Files live in `~/.kova/skills/` (or `$KOVA_HOME/skills/`). For example,
`release-check.md`:

```markdown
---
description: Use before publishing a release of this project.
---
1. Read the project's release instructions.
2. Run the required checks and inspect their actual results.
3. Review the changes with the user before publishing.
```

Descriptions are at most 240 characters; bodies are at most 12,000 UTF-8 bytes.
Model-created stores allow up to 32 skills. Slugs use lowercase letters/numbers
and hyphens, with a 60-character limit. Invalid front matter and symlinks are
ignored during discovery; load/write errors are returned to the model. Writes
are atomic and patches require a unique match. Files can also be reviewed,
edited, or removed with your editor.

Skills are guidance, **not permission to act**. They do not override your request
or Kova's permissions. Creation and patching require approval in manual mode;
auto/full mode permits them. Do not store credentials in skills.

## Scheduled jobs

Ask Kova to schedule a task in work mode, or use the CLI:

```sh
# A recurring shell command, without any LLM call:
kova jobs add "in 5 min" "date >> timestamps.log" --kind script --every 300 --cwd ~/my-project

# A one-shot agent task, recorded in a new chat:
kova jobs add "tomorrow 9am" "Read the project notes and summarize open tasks" --kind agent --cwd ~/my-project

# Opt-in heartbeat: a proactive turn after 30 minutes of user inactivity:
kova jobs add "in 30 min" "Review project notes and suggest one useful next step" --kind heartbeat --every 1800 --cwd ~/my-project

kova jobs list
kova jobs show 1
kova jobs rm 1
```

`--every` is a fixed elapsed-time interval in seconds; zero means once.
Heartbeats require a positive interval. Times use the same local-time parser as
reminders, including `in 10 min`, `tomorrow 5pm`, and ISO 8601. These are elapsed
intervals, not cron expressions or DST-aware calendar recurrences. `--timeout`
is 1–300 seconds (default 60).

The model has `job_add`, `job_list`, and `job_remove`. Jobs capture the chat's
working folder; the CLI defaults to the current directory. Scheduling/removing
jobs requires approval in manual and auto mode, unless an existing permission
rule allows it. Full mode allows scheduling. An approved script executes as your
user without a new approval on every recurrence. Script jobs use the existing
bounded command runner, including process-tree cleanup on timeout/cancellation.
They do not contact a model.

Agent jobs and heartbeats create visible chats with **manual permissions**, not
full-access sessions. Actions requiring a new approval are denied immediately
with an explanation; the job records that outcome rather than waiting for an
unattended approval. Existing explicit permission rules still apply. You can
inspect the chat, or use its normal Stop action. `jobs show` reports the last
result, bounded script output, errors, and an agent result's chat ID.

Heartbeats are **off by default**. Adding a heartbeat job enables one; remove
that job to disable it. It runs only when no foreground turn is active, after a
full interval of inactivity. New foreground activity cancels an active heartbeat
and resets its idle timer. A server restart starts a fresh idle interval.

### Execution and persistence

- The Kova server must be running (`kova serve`); this is not an OS cron service.
  The CLI starts the server on demand. Closing a chat does not stop it.
- The reminder loop checks jobs approximately every second. Normal reminders
  continue to notify independently of running jobs.
- Jobs extend the existing reminders table in `kova.db`, not a separate queue.
  Older databases migrate automatically and ordinary reminders retain their
  behavior. The authenticated `/jobs` API supports listing, creation, and removal.
- A job does not overlap itself. Up to four jobs execute concurrently; additional
  due jobs wait. Heartbeats also wait for other jobs to finish.
- Missed recurrence slots are coalesced into one run, with the next run on the
  original cadence. There is no burst of catch-up jobs.
- Dispatch advances the schedule **before** execution. This prevents duplicate
  dispatch of a claimed slot across restart, but a crash after claiming may miss
  that run. Failed runs record their result; they are not automatically retried.
- Removing a job stops future dispatch; an active run is cancelled on the next
  scheduler tick. Server shutdown cancels active jobs and cleans up child processes.
- The existing portable `kova backup` archive does not yet include skills or job
  definitions. Preserve the skills folder and `kova.db` separately if migrating
  them; restoring ordinary reminders does not enable executable jobs.

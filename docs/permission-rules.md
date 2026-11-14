# Permission rules and approvals

Rules are checked before manual/auto/full mode: **deny > ask > allow**.
Without a matching rule, the existing mode and destructive classifier apply.
A denial's optional `message` goes back to the model.

On Linux with Landlock, normal commands run inside a kernel sandbox that can
only write to the working folder and temporary space, so Manual and Auto do not
prompt for them. The destructive classifier still prompts (deleting the working
folder is data loss even when it is contained), and Full access runs without the
sandbox. When the sandbox is unavailable, every command asks instead of running
unconfined. Rules are checked first either way.

Open the permission menu in the web composer and choose **Edit permission
rules…**, or `/permissions rules` in the
TUI, to edit the JSON array. `/permissions manual|auto|full` remains available.
Example:

```json
[
  {"action": "deny", "tool": "run_command", "pattern": "rm *", "message": "Use the trash instead"},
  {"action": "ask", "tool": "write_file", "pattern": "*.toml"},
  {"action": "allow", "tool": "run_command", "pattern": "git status*"}
]
```

Tool names and command/path patterns use case-sensitive shell-style globs.
Deny and ask match any command part; pattern allows must match every part.
Separators `;`, `&&`, `||`, pipes, newlines, parentheses and backticks are split
conservatively, including within quotes. Expansions, redirects and shell wrappers
require exact approval rather than a pattern allow. This deliberately over-asks;
it is not a shell parser or sandbox. Do not treat a broad `*` allow as isolation.

Cards offer allow once, allow this exact action for this session, always allow
this exact action, and deny with optional guidance. Exact grants bind every tool
argument (including edit contents), working directory, and, for shell commands,
the actual shell argv plus executable fingerprint. They do not trust other commands
in the chat. FileSession's existing file fingerprints still guard stale edits.
Explicit exact grants can approve destructive actions; they do not disable the
classifier for different actions.

Once grants only resolve the pending request. Session rules are scoped by chat id
and disappear on server restart. Always rules persist in
`$KOVA_HOME/permission-rules.json` (normally `~/.kova/permission-rules.json`),
written atomically with owner-only permissions. Remove rules in either editor to
revoke them. Exact grants show their original command/path in `pattern`; edit or
remove `exact` only when intentionally replacing the grant with a pattern rule.

The screenshots in `docs/screenshots/permissions-*` use a disposable server and
scripted model, at 1440×900 and 360×740 in both themes. Playwright checked all
four approval choices, absence of horizontal page overflow, denial submission,
and saving a rule through the actual browser UI.

# AI tools used to build Kova

GenHacks asks every project to list the AI tools it used. This is the running list.

| Tool | Model | Used for |
|---|---|---|
| Claude Code | Claude Opus 5.5 | Design, planning, coordinating the other agents, code review, integration |
| oh-my-pi (omp) | GLM 5.3 (OpenCode Go) | Building features |
| oh-my-pi (omp) / OpenCode | Cyber Tiel Coder 35B-A3B (local, PolyStrata on our homelab) | Small coding jobs, tests; also Kova's own brain in the demo |
| Codex CLI | GPT-6.1 Sol | Hard parts, second-opinion reviews |
| oh-my-pi (omp) | DeepSeek V4.1 Flash (OpenCode Go) | Install/uninstall scripts and the `./cli` launchers |
| oh-my-pi (omp) | GPT-6.1 Sol, GPT-6.1 Terra (ChatGPT/Codex login) | Friendly CLI errors; live memory switching and chat commands |
| herdr | — | Running and watching the agents side by side |
| T3 Code | — | Running Claude and the helper agents as linked threads, each in its own git worktree |

Kova itself talks to any OpenAI-compatible model server; the demo uses Cyber Tiel running locally.

# Cursor, T3 Code, Herdr research

Research snapshot 2026-11-13. VERIFIED means documented by the linked primary source, not independently exercised. UNSURE marks unverified details. Kova paths below are candidate touchpoints, not a completed architecture audit. No secrets inspected.

## Cursor (IDE, agent, CLI)
- VERIFIED — Agent delegates to isolated-context subagents, which return results to their parent; custom agents define prompts, models and tool access, and subagents work in editor, CLI and cloud. Nested delegation has a depth limit. https://cursor.com/docs/subagents
- VERIFIED — Agents Window creates isolated Git worktrees per task; `.cursor/worktrees.json` customizes setup and is checked in IDE, CLI and Agents Window. https://cursor.com/docs/configuration/worktrees
- VERIFIED — Rules in `.cursor/rules` are version-controlled, scoped and injected into model context. https://cursor.com/docs/rules
- VERIFIED — Project memories can be generated from conversation, with background suggestions requiring approval; the older documentation describes direct agent memory tool calls too. Current implementation may have changed. https://docs.cursor.com/en/context/memories
- VERIFIED — Semantic indexing scans the project and computes a Merkle tree; context selection can include current file and similar code. https://www.cursor.com/en/security https://docs.cursor.com/en/guides/working-with-context
- VERIFIED — Local, non-Git checkpoints preview and restore file state without removing chat messages. https://cursor.com/docs/agent/overview
- VERIFIED — Default security docs distinguish reads, workspace edits, configuration edits, data exposure and MCP approvals; workspace edits may land immediately. https://prod.cursor.com/docs/agent/security
- UNSURE — Precise current IDE sidebar, composer, thinking-stream rendering and notification implementation were not verified. Internal model/tool loop and provider-driver implementation are not established by public docs above.

## T3 Code
- VERIFIED — The intended subject is https://github.com/pingdotgg/t3code ; no relevant T4 Code was established.
- VERIFIED — Orchestration is separated from provider-specific adapters; driver identifies integration, instance identifies configuration/account. Native adapters and shared ACP adapter expose explicit capabilities; unsupported modes should not be faked. https://github.com/pingdotgg/t3code/blob/main/docs/internals/providers.md https://github.com/pingdotgg/t3code/blob/main/docs/internals/adding-a-provider.md
- VERIFIED — App-owned MCP delegation creates child threads with the supplied task and optional role, not copied parent history; exposes waits, cancellation and thread operations. https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/orchestrator-mcp-server.md
- VERIFIED — V2 design describes an execution graph with app-owned IDs, nested checkpoints and explicit context handoffs; its README explicitly calls itself target architecture, so do not assume every invariant is shipped. https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/README.md
- VERIFIED — Thread sidebar supports search by project, branch, title and messages; agent views follow child work, with child approvals surfaced to the parent; tool calls expand to command and output. https://github.com/pingdotgg/t3code/blob/main/docs/user/thread-sidebar.md
- VERIFIED — Diff pane separates turn changes from branch comparisons and offers individual commit review. https://github.com/pingdotgg/t3code/blob/main/docs/user/source-control.md
- VERIFIED — Composer supports inline context chips, provider/model selection, durable editable queue versus steering, and provider-dependent context compaction. https://github.com/pingdotgg/t3code/blob/main/docs/user/composer.md
- VERIFIED — Per-thread composer permission modes include supervised, edit auto-accept, auto and full access, with provider-dependent enforcement and fallbacks. https://github.com/pingdotgg/t3code/blob/main/docs/user/permission-modes.md
- VERIFIED — Recent release notes mention earlier paragraph streaming, avoiding freezes with collapsed reasoning traces, avoiding repeated worktree hashing and retaining more terminal scrollback. https://github.com/pingdotgg/t3code/releases
- UNSURE — Exact current notification layout and whether T3 maintains its own semantic memory/index distinct from provider context were not established.

## Herdr
- VERIFIED — The relevant terminal multiplexer is https://github.com/herdrdev/herdr ; the related desktop frontend is https://github.com/herdrdev/herdr-app . This matches the requested agent-aware terminal workspace, not an unrelated similarly named project.
- VERIFIED — A background server owns real terminal processes. Sessions contain workspaces, tabs and split panes; processes survive client detach. CLI and local socket expose automation. https://github.com/herdrdev/herdr/blob/master/distribution/agent-guide.md
- VERIFIED — It recognizes multiple agent CLIs using screen state or agent-reported integration, rolls state up to workspace/tab, and helps jump to blocked work; integration can restore sessions after server restart. https://github.com/herdrdev/herdr/blob/master/docs/next/website/src/content/docs/agents.mdx
- VERIFIED — Desktop app describes persistent terminals and spaces with attention indicators. https://github.com/herdrdev/herdr-app
- UNSURE — Herdr itself is a terminal process manager, not established here as implementing the underlying agents' inference loops, subagent semantics, provider drivers, memory, checkpoints or approval policies. Specific diff-review, composer, mode-picker, thinking-stream and notification UX beyond state notifications not verified; terminal-hosted agents retain their own UX. Worktree isolation behavior not verified.

## Ranked Kova opportunities

The following are proposals/inferences, not claims that Kova lacks these features. Paths are candidate files verified to exist in ~/Kova; inspect code before implementing.

1. VERIFIED inspiration / PROPOSAL — **Visible blocked/working/done state per conversation**: reduces polling and finds approvals quickly. Touch `src/kova/events.py`, `src/kova/store.py`, `src/kova/web_assets/app.js`, `src/kova/tui/app.py`. Source: Herdr agents documentation above.
2. VERIFIED inspiration / PROPOSAL — **Action-specific approval cards**: show operation, exact scope, allow/deny, provider-supported session choices; trust and clarity. Touch `src/kova/permissions.py`, `src/kova/agent.py`, web and TUI renderers. Source: T3 permission modes and Cursor security above.
3. VERIFIED inspiration / PROPOSAL — **Expandable live tool timeline**: compact status plus full command and output, preserving errors; makes autonomous work legible. Touch `src/kova/events.py`, `src/kova/agent.py`, `src/kova/web_assets/app.js`, `src/kova/tui/render.py`. Source: T3 thread sidebar above.
4. VERIFIED inspiration / PROPOSAL — **Queue versus steer in composer**, clearly labeled and persisted; users can keep working without losing instructions. Touch `src/kova/agent.py`, `src/kova/store.py`, `src/kova/server.py`, `src/kova/web_assets/app.js`, `src/kova/tui/editor.py`. Source: T3 composer above.
5. VERIFIED inspiration / PROPOSAL — **Reviewable scoped project rules and memories** with provenance and opt-in for suggested memories; useful continuity without invisible profile changes. Touch `src/kova/project_instructions.py`, `src/kova/memory.py`, `src/kova/store.py`, web and TUI. Sources: Cursor rules and memories above.
6. VERIFIED inspiration / PROPOSAL — **Provider capability contract** for steering, approvals, tools and rollback; prevents UI offering unsupported behavior. Touch `src/kova/model_adapters.py`, `src/kova/providers.py`, `src/kova/provider_routes.py`, `src/kova/modes.py`. Source: T3 adding-a-provider above.
7. VERIFIED inspiration / PROPOSAL — **Per-turn change review with branch comparison**, so agent edits are inspectable. Touch `src/kova/agent.py`, `src/kova/events.py`, `src/kova/web_assets/app.js`, `src/kova/web_assets/app.css`. Source: T3 source-control above.
8. VERIFIED inspiration / PROPOSAL — **Local checkpoints with explicit preview and restore**, separate from Git; make exploration reversible. Touch `src/kova/agent.py`, `src/kova/store.py`, `src/kova/tools/`, web and TUI. Source: Cursor checkpoints above.
9. VERIFIED inspiration / PROPOSAL — **Isolated child tasks and optional worktrees**, with explicit task handoff and parent-visible results; enables parallel research while limiting context and conflicts. Touch `src/kova/agent.py`, `src/kova/events.py`, `src/kova/store.py`, `src/kova/tools/`, web and TUI. Sources: Cursor subagents/worktrees and T3 orchestrator above.
10. VERIFIED inspiration / PROPOSAL — **Long-session polish**: incremental rendering, collapsed large traces, retained scrollback and lazy diff loading; responsiveness protects usability. Touch `src/kova/web_assets/app.js`, `src/kova/web_assets/app.css`, `src/kova/tui/render.py`, `desktop/core.js`. Sources: T3 releases and keybindings https://github.com/pingdotgg/t3code/blob/main/docs/user/keybindings.md

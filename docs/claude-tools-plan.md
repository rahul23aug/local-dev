# Self-contained Claude-style tools

Implement the existing 40-name standalone coding-tool catalogue inside this
repository, with real implementations replacing advertised placeholders.
This is a compatible local interface, not the official Claude Code product.

## Architecture

- `native_tools.py` + `runtime/worker.cjs` + `runtime/core.cjs`: adapted standalone
  Node file/search/notebook implementation and bounded sandboxed commands.
- `tool_state.py`: SQLite task/dependency/team/message/config/mode/cron state.
- `protocol_tools.py`: actual stdio MCP and Content-Length LSP connections.
- `catalog.py`: one catalogue, schemas, discovery and truthful availability.
- `tools.py`: routing, compatibility aliases and existing acceptance safeguards.
- `engine.py`: dynamic tool prompt, real bounded synchronous delegation,
  needs-user checkpoint, scheduler and final-verification authority.
- `network_tools.py`: bounded URL fetching and web-result extraction.

No changes to Agent Pi, its browser services or Colab model settings. No exposed
HTTP listener, boot service or automatic GitHub push. Preserve old tool aliases
for existing fixtures/checkpoints. All persisted tool state lives outside the
model-editable workspace.

## Steps

1. Write failing tests for canonical Read/Edit/Write/Glob/Grep, argument safety,
   shell timeout/flood/namespace isolation, protected files, notebook semantics.
2. Adapt the useful Node implementation, remove browser dependencies and fix
   shell-interpolation risks. Route Bash/REPL through bubblewrap with hidden
   host home, private PID/network namespaces and read-only acceptance files.
3. Write and implement persistent task/DAG/team/message/cron services; due jobs
   execute only during a running harness and uncertain jobs are not replayed.
4. Write and implement real LSP/MCP protocol clients with initialization,
   framing, bounded waits/output, per-call lifecycle cleanup and owner-only
   backend configuration. Dependency absence must never return fake success.
5. Integrate catalogue, discovery, actual child agent execution, user-question
   pause, scoped worktrees/skills, network tools and CLI tool inventory.
6. Run automated tests, exercise a real installed Python language server and
   a real subprocess MCP test server. Verify full catalogue coverage and
   scripted canonical-tool repair through the engine. Review and document
   dependency-gated tools honestly; do not call every feature operational just
   because a name exists in the catalogue.

## Boundaries

PowerShell requires a compatible installed executable. LSP servers and MCP
servers are external dependencies configured by the owner, never by arbitrary
model shell text. RemoteTrigger requires an explicit owner-defined handler.
Agent delegation uses the same inference backend, bounded steps/depth, copied
workspace and verifier; it does not start a fake in-memory agent record.
Model-visible Config cannot relax execution or network policy. Real scheduled
commands are bounded and sandboxed; no always-running scheduler is installed.
Network access remains explicit. Exact public URLs and additional server
commands must be owner-configured; credentials never enter the catalogue.

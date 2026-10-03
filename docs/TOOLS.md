# Compatible local tools

This is a self-contained local implementation of the 40-name interface found
in the existing standalone headless-chatgpt runtime, not official Claude Code.
`local_coder/catalog.py` is the definitive JSON-schema catalogue. Ask
`ToolSearch {"query":"LSP"}` to retrieve a contract and its current availability.
The default prompt lists core tools only to avoid filling an 8K model context.

## Implemented groups

| Group | Tools | Behavior |
| --- | --- | --- |
| Files | Read, Write, Edit, Glob, Grep, NotebookEdit | Bounded text, exact edits, real notebook cell edits; no shell interpolation |
| Execution | Bash, PowerShell, REPL | Real synchronous processes inside mandatory bubblewrap; REPL is disposable JavaScript |
| Delegation | Agent, Task | Real child Engine loop, separate copy, same backend, depth one, at most eight steps; no automatic adoption |
| Tasks | TaskCreate/Get/List/Update/Output/Stop | SQLite records with validated acyclic dependencies; TaskStop stops a logical record, not a running synchronous child |
| Collaboration | TeamCreate/Delete, SendMessage | Persisted logical teams/mailboxes; not parallel process orchestration |
| Progress | TodoWrite, Brief, SendUserMessage | Durable to-dos and user messages returned as run feedback |
| Interaction | AskUserQuestion, EnterPlanMode, ExitPlanMode | Real pause/owner answer; source/execution mutations blocked in plan mode |
| Configuration | Config | Presentation preferences only, never owner security/backend settings |
| Scheduling | CronCreate/Delete/List, Sleep | Actual due sandbox commands at active loop boundaries; UTC cron or seconds interval; no idle daemon; Sleep max five seconds |
| Git | EnterWorktree, ExitWorktree | Private Git repo/worktree in copied run; protected checks and fast-forward adoption; retained recovery artifacts |
| Protocols | LSP, ListMcpResourcesTool | Real initialized stdio LSP/MCP connections, bounded replies and process-group cleanup |
| Network | WebFetch, WebSearch | Bounded HTTP(S), DNS pinning/public-address enforcement and redirect checks; owner must enable |
| Extensions | RemoteTrigger, Skill, ToolSearch | Actual owner handler with JSON stdin, bundled workflow instructions, discoverable schemas |

Additional tools: `Verify`, `Finish`, `ReadEvidence`, `list_mcp_tools`,
`read_mcp_resource`, `call_mcp_tool`.

`Finish` is intercepted by Engine; it is not permission to self-declare success.
`Verify` always uses the fixed owner command. Exit an active worktree before
authoritative verification/completion. Legacy names `list/search/read/replace/
write/verify/finish` remain compatible; `shell` is deliberately not an alias.

## Backends and policy

Optional task-level `tools` is **owner-authored** configuration, persisted with
the run. Do not accept it from an untrusted model or repository. For example:

```json
{
  "tools": {
    "network": true,
    "shell_network": false,
    "lsp": {
      "python": {
        "command": ["/absolute/path/to/pylsp"],
        "extensions": [".py"],
        "language_id": "python"
      }
    },
    "mcp": {
      "docs": {
        "command": ["/absolute/path/to/mcp-server"],
        "allowed_tools": ["search_docs"]
      }
    },
    "remote_triggers": {
      "notify": {"command": ["python3", "notify.py"]}
    }
  }
}
```

Python defaults to the project-private `.venv/bin/pylsp` when installed;
explicit `lsp: {}` disables it. Other languages need their own configured
servers. MCP is stdio-only in this increment; no backend is assumed. Every
MCP call requires an explicit `allowed_tools` entry (default denied).
RemoteTrigger requires a named configured handler; it is not a fake queued
request or an automatically provisioned webhook. Its stdin is the JSON payload,
not interpolated shell text. Environment/commands remain outside model schemas.
The handler runs inside the execution sandbox: scripts must be in the copied
workspace (as `notify.py` above), or use exposed installed system binaries.
Arbitrary executables elsewhere in the Pi home are deliberately not exposed.

Web access is disabled by default. `network: true` enables public HTTP(S);
`allow_private_network: true` is an explicit owner override for private endpoints.
`shell_network` is separately disabled by default. Never enable either for
untrusted repository code. Search uses public HTML results and may be blocked
or rate-limited upstream; no bypass or guaranteed availability is claimed.

PowerShell is found in project `.tools/pwsh/pwsh`, system PATH, or a workspace
fallback. The checked Linux ARM64 archive is version 7.6.6, verified against
the official release SHA-256 before extraction. It is not committed to Git.
`requirements-tools.txt` pins Python LSP 1.15.0. Install using
`python3 tools/install_dependencies.py` (add `--powershell` on Linux ARM64).
This does not modify system packages, boot services or Colab model settings.

## Evidence and recovery

Raw bounded command/web output is outside the editable workspace in
`<run-dir>/evidence/`. Use `ReadEvidence` with the returned filename and byte
offset to retrieve a page. Agent returns a bounded child diff and an evidence
filename for the full bounded diff, so the parent can inspect and explicitly
apply changes. The child cannot mark the parent complete.

Tasks/messages/questions/modes/cron receipts persist in `<run-dir>/tools.sqlite3`.
Child questions are relayed to the parent and shown by CLI status with IDs;
answer them using `local-coder answer`, then resume. The harness keeps private
continuation records in `agent.db`; editing task metadata cannot authorize a
child run or increase its budget. Resuming children is also blocked in plan
mode. An uncertain in-flight delegation becomes INTERRUPTED, without replay.
Brief/SendUserMessage are logged and delivered through the CLI's MESSAGE output.
Worktree routing persists in private run metadata. Interrupted cron receipts
are never blindly replayed. Scheduled commands run only while the harness is
active, before decisions; they are not realtime alarms or boot jobs.

The textual JSON tool protocol and synchronous delegation are intentional
limits. No native provider function-call adapter, background parallel workers,
GitNexus, Context Mode integration or hidden acceptance oracle is claimed here.

## Primary dependency references

- [Python LSP server](https://github.com/python-lsp/python-lsp-server)
- [PowerShell official release](https://github.com/PowerShell/PowerShell/releases/tag/v7.6.6)
- [MCP 2025-06-18 specification](https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/docs/specification/2025-06-18)
- [Runtime provenance](../local_coder/runtime/PROVENANCE.md)

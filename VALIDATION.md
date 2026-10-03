# Validation — 2026-10-03

## Current increment: compatible coding tools

- `python3 -m unittest discover -s tests -q`: **116 tests passed** in
  183.488 seconds on this Pi, after all implementation/recovery fixes.
- Both vendored Node files pass `node --check`; Python modules/install helper
  pass `compileall`; `git diff --check` passes.
- `./local-coder tools` discovers **46 contracts**: the 40-name compatible
  catalogue plus Verify, Finish, ReadEvidence and three MCP helpers.
- During an active Engine run, 39 contracts are ready under default local
  configuration. Seven are deliberately gated: four MCP operations require
  owner-configured servers/allowlists, WebFetch/WebSearch require network
  permission, and RemoteTrigger requires an owner handler. Standalone Agent/
  Task additionally require an Engine run. These are not fake-success stubs.
- Real project-private PowerShell 7.6.6, Linux ARM64, downloaded from its
  official release and checked against SHA-256
  `924829e54c983648f6f1419a2dc7f9433c861b2fb5bd57736ff096c24f133729`:
  sandboxed `Write-Output (20 + 22)` passed, exit 0, output `42`.
- Real project-private Python LSP 1.15.0: initialized `pylsp`, opened a Python
  file and resolved its call-site definition through both ProtocolTools and
  the main Tools coordinator.
- MCP: real fixture subprocess initializes and lists/reads resources, lists/
  calls tools, paginates, handles protocol errors and cleans up process groups.
  An owner-allowlisted `echo` call through Tools returned `{"answer":42}`.
  This validates the protocol/client, not an external production MCP server.
- `./local-coder demo` completed copied fixture run
  `d3f05418e4574e8d8bf7cf54a529bfdd` in five scripted decisions, with two
  verifier runs and one rejected early completion. Only `calc.py` changed;
  original fixture remains untouched. Canonical tool flow is separately tested.

Regression coverage includes namespace/secret isolation, protected files and
ancestor/hardlink tampering, timeout/flood/descendant cleanup, real notebook
cells, argv-only searches, persisted DAGs/tasks/mail/cron, planning enforcement,
real private Git worktrees and fast-forward adoption, public-address HTTP/DNS/
redirect enforcement, and protocol framing/receipt failures. Parent/child
questions pause correctly, answers survive bounded context, Unicode child
reports stay within budget, editable task metadata cannot authorize continuation,
and uncertain delegation is not replayed.

### Limits and publication

- Compatible local implementation, **not official Claude Code** or a guarantee
  of every official tool option. JSON actions remain textual, not native API
  function calls. See [tool contracts and configuration](docs/TOOLS.md).
- New tool use is integration-tested with scripted workers; no new full live
  Qwen benchmark was run for this increment. Previous tiny live-model result
  below remains a smoke test, not broad capability evidence.
- Synchronous child workers only; teams/mailboxes are logical durable records,
  not background parallel processes. Cron runs only at active loop boundaries.
- Model execution/native tools are bubblewrap-isolated. The fixed owner
  verifier and configured LSP/MCP servers still run as the owner: trusted
  repositories/commands only; no complete hostile-code/cgroup sandbox claimed.
- Python LSP is configured locally; other languages need servers. Web search
  may be blocked/rate-limited upstream. MCP and RemoteTrigger need owner setup.
- Source is on branch `feat/claude-style-tools`; merging into `main` is separate.
  Private dependencies, run databases, logs and copied
  workspaces remain Git-ignored. Agent Pi, browser services and Colab settings
  were not modified; no daemon or boot service was installed.

## Historical first-increment validation

## Verified locally

- `python3 -m unittest discover -s tests -v`: 22 tests passed.
- `./local-coder demo`: completed a copied fixture in five decisions.
- Demo recorded one rejected false-completion request and two verifier runs.
- Only `calc.py` changed in the successful demo workspace; original fixture
  remained buggy and untouched.
- CLI help works through the installed Colab CLI Python environment.
- Live Colab adapter smoke test passed after setup released its lock. Actual
  Qwen returned `{"tool":"list","args":{}}` as valid JSON through the receipted
  transport: 163 prompt tokens, 10 completion tokens, finish reason `stop`.
  Overall test elapsed 190.28 seconds including waiting for setup; this is not
  an isolated inference-latency measurement.

Coverage includes persistence/reopening, checkpoint budget resume, uncertain
action handling, failed verification, protected test tampering, workspace path
escape/symlinks, bounded context, malformed decisions, infrastructure failure
classification, verifier timeout, output flooding, retained raw evidence, and
Colab adapter receipts/usage/concurrent-inference locking. Independent review
identified and regression tests now cover explicit verification metrics,
missing verifier infrastructure, lock storage errors and malformed receipts.

## Not verified / not claimed

- One full live Qwen coding smoke task subsequently completed autonomously;
  see [the measured result](benchmarks/results/2026-10-03-addition.md).
  This tiny task is not a repository-level capability benchmark.
- The smoke backend is scripted; it says nothing about model competence.
- No independent hidden oracle, OS sandbox, dependency DAG, GitNexus adapter,
  Context Mode adapter, supervisor/worker roles, or ablation suite yet.
- Existing production repositories, Colab model configuration and services
  were not changed. No boot service was created. Source publication as the
  public `local-dev` repository is a separate explicit user request; runtime
  state, raw logs and copied workspaces are excluded from Git.
- Passing owner-defined tests is not proof of all requirements or regression
  safety. Test code runs with the caller's permissions; trusted fixtures only.

## Locations

- Source: `/home/rahul/local-coder`
- Run database: `/home/rahul/local-coder/.state/agent.db`
- Workspaces: `.state/runs/<run-id>/workspace/`
- Raw verifier logs: `.state/runs/<run-id>/evidence/<uuid>.log`
- Representative successful smoke run: `908df00114834b1194633d6cc85c8b6d`

Next useful validation is a harder unknown-location task and an independent,
isolated benchmark runner before claiming autonomous success rates.

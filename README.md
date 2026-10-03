# Local Coder

GitHub repository: `local-dev`; project and local CLI: **Local Coder**.

An experimental coding harness: the Raspberry Pi owns workspaces, execution,
evidence, checkpoints and completion. Colab supplies inference through the
existing `colab-agent` transport. No existing project is modified by setup.

## Run

```bash
cd /home/rahul/local-coder
./local-coder demo
```

This scripted smoke test intentionally requests completion too early, receives
failing tests, edits the copied fixture and completes only after tests pass.
It tests the harness, **not Qwen's coding ability**.

For a real model run, first start your existing Colab inference environment:

```bash
cd /home/rahul/colab-agent
./colab-agent setup
cd /home/rahul/local-coder
./local-coder run examples/addition-task.json --trusted-code --max-steps 20
./local-coder status RUN_ID
./local-coder resume RUN_ID --trusted-code --max-steps 40
```

Do not run setup if inference is already ready. No GPU is requested or runtime
automatically recreated by Local Coder. Model/context/hardware configuration
remains owned by `colab-agent/environment.json` (currently Qwen3.5 **9B**, CPU,
8K context). The adapter asks for up to 1,024 reply tokens; the existing remote
tokenizer enforces the prompt/context limit. Replies are non-streaming.

## Owner-authored tasks

Use JSON with an absolute `repo`, a concise `objective`, a verification command
as an argv array, and optional `protected` relative paths. Source files are
copied into `.state/runs/RUN_ID/workspace`; changes are not applied back or
committed automatically. Verify the resulting diff yourself before adopting it.

The repository includes a standalone **40-name Claude-style tool interface**,
plus governed `Verify`/`Finish` and MCP/evidence helpers. It is not the official
Claude Code runtime. Tools are implemented here, without a headless-browser
service or Agent Pi dependency. See [tool guide](docs/TOOLS.md).

```bash
./local-coder tools
python3 tools/install_dependencies.py --powershell  # optional, Linux ARM64
```

The model returns one JSON action per turn; the Pi validates and executes it,
then feeds back results. This is not native API function-calling. Core tool
contracts stay in the short prompt; `ToolSearch` discovers advanced contracts
and reports missing dependencies/policies honestly. Existing lowercase fixture
names remain supported. Existing tests are protected automatically; protect
other acceptance/configuration files explicitly. Added tests are permitted.

Completion requires a successful fresh owner verifier, intact acceptance files
and a nonempty file change. This is deliberately limited evidence: it does not
prove every requirement, hidden test, or regression is satisfied. A no-change
task cannot complete in this repair-focused MVP.

## Optional large-model supervisor

A separate large model can now own planning/review while Colab remains the coding
worker. The supervisor never receives repository tool authority and cannot mark
the run complete. It creates a controller-owned dependency DAG, reviews bounded
worker diffs, can revise/replan after failures, and performs a final model review
before the existing owner verifier/audit remains authoritative.

Configure an owner-authored JSON task with a supervisor command:

```json
{
  "repo": "/absolute/repo",
  "objective": "Implement the requested change",
  "verify": ["python3", "-m", "pytest", "-q"],
  "supervisor": {
    "command": ["/absolute/path/to/large-model-wrapper"],
    "timeout": 180
  }
}
```

The wrapper receives one JSON object on stdin with operation `plan`, `review`,
`final_review` or `recover`, and returns exactly one JSON object on stdout.
It can call Claude, Gemini, OpenAI or another larger model. Supervisor commands
are owner configuration and execute as the owner, not in the worker sandbox.
See [supervisor protocol](docs/SUPERVISOR.md).

## Persistence and recovery

SQLite `.state/agent.db` stores runs, decisions, raw observations, usage and
checkpoints. Context contains the task plus a bounded tail of evidence with
event IDs, not the whole history. Infra failures become `INFRA_BLOCKED`, not
coding failures. Increase the total step budget to resume exhausted runs.
Each run also has `tools.sqlite3` for durable tasks/dependencies, messages,
to-dos, preferences, planning mode, questions and scheduled-command receipts.
`AskUserQuestion` pauses with `NEEDS_INPUT`; answer using:

```bash
./local-coder answer RUN_ID QUESTION_ID 'your answer'
./local-coder resume RUN_ID --trusted-code --max-steps 40
```

An uncertain in-flight generation/action/verification becomes `INTERRUPTED`
on restart and is **not** automatically replayed. Inspect it and start a fresh
run; this avoids repeating potentially applied changes. Completed runs are
immutable through the run loop. One controller runs at a time. Colab inference
also takes the existing colab-agent lock to avoid setup/chat overlap.

## Security and limits

**Trusted repositories, owner-configured servers and test commands only.**
Model shell/REPL/PowerShell and native file tools use fail-closed bubblewrap:
private PID/network/home, a writable copied workspace, hidden-dotfile masking,
read-only system binaries/runtime and read-only acceptance files. This is not
a complete hostile-code isolation system: there is no cgroup resource budget,
and the fixed owner verifier and configured LSP/MCP servers run as your user.
Malicious verifier/server code can access host files, network or secrets.
Do not feed untrusted repositories or servers to this experimental harness.
No genuinely hidden oracle exists yet.

Copies exclude Git internals, virtual environments, node_modules, bytecode,
tool/runtime state and `.env*`; other secret files may still be copied. Repositories with symlinks are
rejected. Maximum copy: 5,000 files/50 MiB; file tool: 256 KiB; verification:
120 seconds and 1 MiB of output; model loop defaults to 30 actions. Node,
ripgrep, bubblewrap and Git must be installed. Optional backends are private
project installs; missing ones produce explicit unavailable status.
Full verifier output up to that limit is retained in each run's `evidence/`
directory; only a tail enters model context. Children of timed-out tests are stopped.
Prompts and source snippets go to Colab. SQLite and Colab CLI logs/receipts can
contain source and prompts; files created by the CLI use a private umask.

## Verification

```bash
python3 -m unittest discover -s tests -v
```

See [VALIDATION.md](VALIDATION.md) and [implementation plan](docs/implementation-plan.md).
Future increments: GitNexus, Context Mode, independent hidden oracle,
benchmark datasets and ablations. These are not claimed as implemented. The CLI creates no daemon or boot service and does
not push commits automatically.

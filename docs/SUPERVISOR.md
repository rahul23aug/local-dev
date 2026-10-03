# Large-model supervisor protocol

Local Coder can optionally use a large model as a planning/review brain while
the existing Colab model remains the coding worker.

The separation is deliberate:

- **Supervisor:** decomposes the user objective, reviews worker evidence and
  replans/reopens work when needed.
- **Worker:** uses the Claude-style local tools to inspect, edit, run and test
  code one bounded work packet at a time.
- **Harness:** owns the workspace, controller-only DAG, checkpoints, protected
  acceptance files, final verifier and the only transition to `COMPLETE`.

The worker cannot edit the supervisor DAG. `TaskCreate`/`TaskUpdate` remain
useful worker coordination tools, but they are not continuation authority for
the supervisor.

## Configure

An owner-authored task may include:

```json
{
  "repo": "/absolute/path/to/repo",
  "objective": "Implement retry handling without changing the public API",
  "verify": ["python3", "-m", "pytest", "-q"],
  "supervisor": {
    "command": ["/absolute/path/to/supervisor-wrapper"],
    "timeout": 180
  }
}
```

The command is persisted so `resume` reconstructs the same supervisor. It runs
as the owner (outside the worker bubblewrap sandbox) and may therefore use the
owner's authenticated Claude/Gemini/OpenAI CLI or API wrapper. Do not take this
configuration from repository content or from the worker model.

## Stdio contract

For each supervisor call, Local Coder writes exactly one JSON request to stdin:

```json
{"operation": "plan", "payload": {}}
```

The wrapper must write exactly one JSON object to stdout and exit zero. The
request is capped at 256 KiB, output at 1 MiB, and timeout at 600 seconds.

### `plan`

Input includes the original objective and a bounded repository file manifest.

Expected output:

```json
{
  "nodes": [
    {
      "id": "inspect",
      "title": "Map current retry path",
      "objective": "Find where transient database failures are classified.",
      "blocked_by": [],
      "success_criteria": ["Relevant call path is understood"]
    },
    {
      "id": "implement",
      "title": "Implement retry",
      "objective": "Implement bounded exponential backoff for transient errors.",
      "blocked_by": ["inspect"],
      "success_criteria": ["Integrity errors are never retried"]
    }
  ]
}
```

Plans are bounded to 32 nodes and must be acyclic. Node IDs and dependencies are
validated before they are persisted.

### `review`

Called when the worker uses `Finish` for the current node. Input contains the
node plus bounded changed-file/diff and recent execution evidence.

Return one of:

```json
{"decision": "accept", "guidance": "The node is satisfied."}
```

```json
{"decision": "revise", "guidance": "The fallback path is still missing."}
```

or `replan` with a complete replacement `nodes` array. A replan cannot delete
already completed nodes or make completed work depend on unfinished work.

### `final_review`

After every node is accepted, the supervisor reviews the aggregate evidence:

```json
{"decision": "accept", "guidance": "Requirements are covered."}
```

or:

```json
{"decision": "revise", "reopen_node": "implement", "guidance": "Handle timeout errors too."}
```

Reopening a node also reopens completed downstream dependants.

### `recover`

If the supervisor accepted the work but the deterministic owner verifier fails,
the failure evidence is sent back with operation `recover`. It must return
`revise` with `reopen_node`, or `replan`.

The large model never overrides a failing owner verifier.

## Direct model adapter

`ModelSupervisorBackend` can wrap any future backend that implements the same
`generate(messages)` interface as the current Colab backend. That keeps the
supervisor provider-independent; a Claude/Gemini/OpenAI adapter can be added
without changing the worker Engine.

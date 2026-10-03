# First-increment validation — 2026-10-03

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

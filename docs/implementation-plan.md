# Local Coder — first increment

Approved direction: Raspberry Pi owns the agent; Colab owns inference only.
Implement inline without further architecture review.

## Scope

Python standard-library CLI with SQLite checkpoints, bounded atomic decisions,
copied task workspaces, restricted file tools, owner-defined verification,
completion governor, Colab adapter and a deterministic smoke benchmark.
Do not change colab-agent or any production repository. No auto-start service.

## Sequence

1. Write behavioral tests for completion rejection, repair, persistence,
   workspace escape, test tampering, malformed decisions and budget exhaustion.
   Run `python3 -m unittest discover -s tests -v` and observe missing implementation.
2. Implement `local_coder/store.py` (runs/events), `tools.py` (bounded file tools
   and verification), `backend.py` (scripted and existing Colab transport),
   `engine.py` (governed loop), and `cli.py` (demo/run/resume/status).
3. Run the same tests; repair defects; run the demo through actual file edits
   and subprocess tests. Record validation and limitations.

## Boundaries

This is a trusted-code MVP, not an OS sandbox. File tools reject symlinks and
path escape, but repository tests execute with the invoking user's permissions.
Use only trusted fixtures until container/user isolation is implemented.
No arbitrary model shell tool. Tests and criteria are provided by the owner,
not the model. Test hashes are checked before completion. A passing command
is evidence, not proof of complete requirement coverage.

## Next increments (not implemented in this slice)

Persistent dependency DAG, independent hidden oracle in an OS-isolated runner,
GitNexus adapter, Context Mode adapter, supervisor/worker split, ablation runner
and a preregistered multi-task benchmark. No claims of small-model capability
from a scripted smoke demo.

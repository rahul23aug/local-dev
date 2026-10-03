# Testing and acceptance

Read the affected implementation and the existing tests before changing code.
Add a narrowly scoped regression test when allowed. Existing protected acceptance
files are read-only: do not weaken them, remove assertions or replace the verifier.

Use Verify for authoritative verification. Bash can run targeted checks, but those
checks alone do not mark the run complete. Inspect failures and repair their cause.
Before Finish, inspect the diff and check public APIs and unrelated behavior.
Only the harness decides COMPLETE after independent verification and integrity checks.

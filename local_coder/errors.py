class InfrastructureError(RuntimeError):
    """Host or inference transport failed; do not ask the worker to fix it."""

"""Optional large-model supervisor for decomposing and reviewing worker tasks.

The supervisor never executes repository tools and never owns completion. It
produces bounded plans/reviews while the existing Engine remains the worker and
the deterministic harness retains final verification authority.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess

from .errors import InfrastructureError

MAX_NODES = 32
MAX_PAYLOAD = 256 * 1024
MAX_REPLY = 1024 * 1024
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

_OPERATION_SCHEMA = {
    "plan": '{"nodes":[{"id":"N1","title":"...","objective":"...","blocked_by":[],"success_criteria":["..."]}]}',
    "review": '{"decision":"accept|revise|replan","guidance":"...","nodes":[...]?}',
    "final_review": '{"decision":"accept|revise","guidance":"...","reopen_node":"N1"?}',
    "recover": '{"decision":"revise|replan","guidance":"...","reopen_node":"N1"?,"nodes":[...]?}',
}


class ScriptedSupervisorBackend:
    """Deterministic supervisor used by controller tests."""
    def __init__(self, responses):
        self.responses = iter(responses)

    def call(self, operation, payload):
        try:
            response = next(self.responses)
        except StopIteration as exc:
            raise InfrastructureError("Scripted supervisor exhausted") from exc
        if not isinstance(response, dict):
            raise InfrastructureError("Supervisor response must be an object")
        return response


class ModelSupervisorBackend:
    """Adapt any existing chat backend with generate(messages) into a supervisor."""
    def __init__(self, backend):
        self.backend = backend

    def call(self, operation, payload):
        if operation not in _OPERATION_SCHEMA:
            raise InfrastructureError("Unknown supervisor operation")
        system = (
            "You are the planning/review brain for a coding agent. The small worker executes tools. "
            "You do not execute tools and you cannot declare the overall run complete. Return exactly "
            "one JSON object and no markdown. Keep nodes small, independently reviewable, and ordered "
            "by explicit dependencies. For this operation return: " + _OPERATION_SCHEMA[operation]
        )
        response = self.backend.generate([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"operation": operation, "payload": payload}, ensure_ascii=True)},
        ])
        try:
            record = json.loads(response["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InfrastructureError("Supervisor model returned invalid JSON") from exc
        if not isinstance(record, dict):
            raise InfrastructureError("Supervisor model returned a non-object")
        return record


class CommandSupervisorBackend:
    """Owner-configured supervisor command using one JSON request/response on stdio.

    The command receives {"operation":...,"payload":...} on stdin and must emit
    exactly one JSON object on stdout. It runs as the owner, not in the worker
    sandbox, so only owner-authored commands belong here.
    """
    def __init__(self, command, timeout=180):
        if (not isinstance(command, list) or not command or
                any(not isinstance(item, str) or not item or "\x00" in item for item in command)):
            raise ValueError("Supervisor command must be a nonempty argv list")
        if type(timeout) not in (int, float) or not 1 <= float(timeout) <= 600:
            raise ValueError("Supervisor timeout must be 1-600 seconds")
        self.command = list(command)
        self.timeout = float(timeout)

    def configuration(self):
        return {"command": self.command, "timeout": self.timeout}

    def call(self, operation, payload):
        try:
            request = json.dumps({"operation": operation, "payload": payload}, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError) as exc:
            raise InfrastructureError("Supervisor request is not bounded JSON") from exc
        if len(request) > MAX_PAYLOAD:
            raise InfrastructureError("Supervisor request exceeds 256 KiB")
        try:
            process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise InfrastructureError("Supervisor command could not be started") from exc
        try:
            try:
                stdout, stderr = process.communicate(request, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise InfrastructureError("Supervisor command timed out") from None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if process.returncode != 0:
            raise InfrastructureError(f"Supervisor command failed with exit code {process.returncode}")
        if len(stdout) > MAX_REPLY or len(stderr) > MAX_REPLY:
            raise InfrastructureError("Supervisor command output exceeds 1 MiB")
        try:
            record = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise InfrastructureError("Supervisor command returned invalid JSON") from exc
        if not isinstance(record, dict):
            raise InfrastructureError("Supervisor command returned a non-object")
        return record


class Supervisor:
    """Persistent controller-owned task graph and large-model review boundary."""
    def __init__(self, store, backend, max_nodes=MAX_NODES):
        if type(max_nodes) is not int or not 1 <= max_nodes <= MAX_NODES:
            raise ValueError("max_nodes must be 1-32")
        self.store = store
        self.backend = backend
        self.max_nodes = max_nodes

    def configuration(self):
        method = getattr(self.backend, "configuration", None)
        return method() if method else {}

    @staticmethod
    def _text(value, limit, name):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise InfrastructureError(f"Supervisor {name} must be 1-{limit} characters")
        return value.strip()

    def _normalize_plan(self, raw_nodes, existing=()):
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= self.max_nodes:
            raise InfrastructureError(f"Supervisor plan must contain 1-{self.max_nodes} nodes")
        old = {node["id"]: node for node in existing}
        nodes = []
        ids = set()
        for position, raw in enumerate(raw_nodes):
            if not isinstance(raw, dict):
                raise InfrastructureError("Supervisor plan node must be an object")
            ident = raw.get("id")
            if not isinstance(ident, str) or not _NODE.fullmatch(ident) or ident in ids:
                raise InfrastructureError("Supervisor node id is invalid or duplicated")
            ids.add(ident)
            blocked = raw.get("blocked_by", raw.get("blockedBy", []))
            criteria = raw.get("success_criteria", [])
            if (not isinstance(blocked, list) or len(blocked) > 16 or
                    any(not isinstance(item, str) for item in blocked)):
                raise InfrastructureError("Supervisor blocked_by must be a bounded id list")
            if (not isinstance(criteria, list) or len(criteria) > 12 or
                    any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in criteria)):
                raise InfrastructureError("Supervisor success criteria are invalid")
            previous = old.get(ident, {})
            nodes.append({
                "id": ident,
                "position": position,
                "title": self._text(raw.get("title", ident), 200, "node title"),
                "objective": self._text(raw.get("objective"), 3000, "node objective"),
                "blocked_by": list(dict.fromkeys(blocked)),
                "success_criteria": [item.strip() for item in criteria],
                "status": "completed" if previous.get("status") == "completed" else "pending",
                "attempts": int(previous.get("attempts", 0)),
                "last_review": previous.get("last_review", ""),
            })
        table = {node["id"]: node for node in nodes}
        if any(dep not in table or dep == node["id"] for node in nodes for dep in node["blocked_by"]):
            raise InfrastructureError("Supervisor plan contains an unknown/self dependency")
        visiting, visited = set(), set()
        def visit(ident):
            if ident in visiting:
                raise InfrastructureError("Supervisor plan contains a dependency cycle")
            if ident in visited:
                return
            visiting.add(ident)
            for dependency in table[ident]["blocked_by"]:
                visit(dependency)
            visiting.remove(ident)
            visited.add(ident)
        for ident in table:
            visit(ident)
        completed = {node["id"] for node in nodes if node["status"] == "completed"}
        if any(node["status"] == "completed" and any(dep not in completed for dep in node["blocked_by"])
               for node in nodes):
            raise InfrastructureError("Supervisor replan cannot block a completed node on unfinished work")
        return nodes

    @staticmethod
    def _manifest(workspace):
        workspace = workspace.resolve()
        result = []
        for path in sorted(workspace.rglob("*")):
            if len(result) >= 300:
                break
            try:
                relative = path.relative_to(workspace)
            except ValueError:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_file() and not path.is_symlink():
                result.append(relative.as_posix())
        return result

    def ensure_plan(self, run, row, workspace):
        if self.store.supervisor_nodes(run):
            return
        response = self.backend.call("plan", {
            "objective": row["objective"],
            "repository_files": self._manifest(workspace),
            "rules": [
                "The worker handles concrete tool use and code edits.",
                "Keep each node small enough for a weak local coding model.",
                "Do not encode shell commands, credentials, or tool policy in the plan.",
            ],
        })
        nodes = self._normalize_plan(response.get("nodes"))
        self.store.supervisor_replace(run, nodes)
        self.store.event(run, "SUPERVISOR_PLAN", {"nodes": [{"id": n["id"], "title": n["title"]} for n in nodes]})

    def nodes(self, run):
        return self.store.supervisor_nodes(run)

    def ensure_current(self, run):
        nodes = self.nodes(run)
        active = next((node for node in nodes if node["status"] == "in_progress"), None)
        if active:
            return active
        completed = {node["id"] for node in nodes if node["status"] == "completed"}
        ready = next((node for node in nodes if node["status"] == "pending" and
                      all(dep in completed for dep in node["blocked_by"])), None)
        if ready:
            self.store.supervisor_update(run, ready["id"], status="in_progress")
            return self.store.supervisor_node(run, ready["id"])
        return None

    def context(self, run):
        nodes = self.nodes(run)
        active = next((node for node in nodes if node["status"] == "in_progress"), None)
        return {
            "enabled": True,
            "phase": "work" if active else ("final_review" if nodes and all(n["status"] == "completed" for n in nodes) else "waiting"),
            "current": active,
            "progress": [{"id": node["id"], "title": node["title"], "status": node["status"],
                          "blocked_by": node["blocked_by"]} for node in nodes],
        }

    @staticmethod
    def _guidance(response):
        value = response.get("guidance", "")
        if not isinstance(value, str) or len(value) > 4000:
            raise InfrastructureError("Supervisor guidance is invalid")
        return value.strip()

    def _replan(self, run, response):
        existing = self.nodes(run)
        nodes = self._normalize_plan(response.get("nodes"), existing=existing)
        completed = {node["id"] for node in existing if node["status"] == "completed"}
        if not completed.issubset({node["id"] for node in nodes}):
            raise InfrastructureError("Supervisor replan cannot delete completed nodes")
        self.store.supervisor_replace(run, nodes)
        guidance = self._guidance(response)
        current = self.ensure_current(run)
        if guidance and current is not None:
            self.store.supervisor_update(run, current["id"], last_review=guidance)
        return {"decision": "replan", "guidance": guidance}

    def review_current(self, run, evidence):
        node = self.ensure_current(run)
        if node is None:
            raise InfrastructureError("Supervisor review requested without an active node")
        response = self.backend.call("review", {"node": node, "evidence": evidence})
        decision = response.get("decision")
        guidance = self._guidance(response)
        attempts = node["attempts"] + 1
        if decision == "accept":
            self.store.supervisor_update(run, node["id"], status="completed", attempts=attempts,
                                         last_review=guidance)
            return {"decision": "accept", "node": node["id"], "guidance": guidance}
        if decision == "revise":
            self.store.supervisor_update(run, node["id"], status="in_progress", attempts=attempts,
                                         last_review=guidance)
            return {"decision": "revise", "node": node["id"], "guidance": guidance}
        if decision == "replan":
            return self._replan(run, response)
        raise InfrastructureError("Supervisor review decision is invalid")

    def all_complete(self, run):
        nodes = self.nodes(run)
        return bool(nodes) and all(node["status"] == "completed" for node in nodes)

    def _reopen(self, run, ident, guidance):
        nodes = self.nodes(run)
        table = {node["id"]: node for node in nodes}
        if ident not in table:
            raise InfrastructureError("Supervisor requested an unknown node to reopen")
        affected = {ident}
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if node["id"] not in affected and any(dep in affected for dep in node["blocked_by"]):
                    affected.add(node["id"])
                    changed = True
        for node in nodes:
            if node["id"] == ident:
                self.store.supervisor_update(run, ident, status="in_progress", last_review=guidance)
            elif node["id"] in affected:
                self.store.supervisor_update(run, node["id"], status="pending")
        return ident

    def final_review(self, run, evidence):
        response = self.backend.call("final_review", {"nodes": self.nodes(run), "evidence": evidence})
        decision = response.get("decision")
        guidance = self._guidance(response)
        if decision == "accept":
            return {"decision": "accept", "guidance": guidance}
        if decision == "revise":
            ident = response.get("reopen_node")
            if not isinstance(ident, str):
                raise InfrastructureError("Supervisor final revision must identify reopen_node")
            self._reopen(run, ident, guidance)
            return {"decision": "revise", "reopen_node": ident, "guidance": guidance}
        raise InfrastructureError("Supervisor final review decision is invalid")

    def recover(self, run, evidence):
        response = self.backend.call("recover", {"nodes": self.nodes(run), "evidence": evidence})
        decision = response.get("decision")
        guidance = self._guidance(response)
        if decision == "revise":
            ident = response.get("reopen_node")
            if not isinstance(ident, str):
                raise InfrastructureError("Supervisor recovery must identify reopen_node")
            self._reopen(run, ident, guidance)
            return {"decision": "revise", "reopen_node": ident, "guidance": guidance}
        if decision == "replan":
            return self._replan(run, response)
        raise InfrastructureError("Supervisor recovery decision is invalid")

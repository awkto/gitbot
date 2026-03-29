"""Activity tracking for the admin panel.

In-memory ring buffer of recent events, current work, and stats.
The brain writes here, the admin API reads.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

MAX_EVENTS = 200
MAX_WORKFLOWS = 50


@dataclass
class ActivityEvent:
    timestamp: float
    level: str  # "info", "warn", "error"
    message: str
    workflow_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "message": self.message,
            "workflow_id": self.workflow_id,
            "extra": self.extra,
        }


@dataclass
class StepInfo:
    """Details about a single execution step within a workflow."""
    number: int
    description: str
    tier: str = ""
    model: str = ""
    tools_count: int = 0
    started: float = 0
    finished: float = 0
    status: str = "running"  # running, completed, failed
    escalated: bool = False
    actions: list[dict] = field(default_factory=list)  # [{tool, result_preview, error}]

    def to_dict(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started if self.started else 0
        return {
            "number": self.number,
            "description": self.description,
            "tier": self.tier,
            "model": self.model,
            "tools_count": self.tools_count,
            "elapsed_seconds": round(elapsed, 1),
            "status": self.status,
            "escalated": self.escalated,
            "actions": self.actions,
        }


@dataclass
class Workflow:
    id: str
    started: float
    status: str = "running"  # running, completed, failed
    trigger: str = ""        # "assigned", "mentioned", etc.
    target: str = ""         # "Issue #42", "MR !5"
    project: str = ""
    plan_steps: int = 0
    completed_steps: int = 0
    tool_calls: int = 0
    escalations: int = 0
    models_used: list[str] = field(default_factory=list)
    finished: float = 0
    error: str = ""
    phases: list[str] = field(default_factory=list)  # ["gather", "plan", "execute"]
    steps: list[StepInfo] = field(default_factory=list)
    gather_summary: str = ""

    def to_dict(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started
        return {
            "id": self.id,
            "started": self.started,
            "status": self.status,
            "trigger": self.trigger,
            "target": self.target,
            "project": self.project,
            "plan_steps": self.plan_steps,
            "completed_steps": self.completed_steps,
            "tool_calls": self.tool_calls,
            "escalations": self.escalations,
            "models_used": list(set(self.models_used)),
            "elapsed_seconds": round(elapsed, 1),
            "error": self.error,
            "phases": self.phases,
            "steps": [s.to_dict() for s in self.steps],
            "gather_summary": self.gather_summary[:200] if self.gather_summary else "",
        }


MAX_DEBUG_LOGS = 30


class ActivityTracker:
    def __init__(self):
        self._events: deque[ActivityEvent] = deque(maxlen=MAX_EVENTS)
        self._workflows: deque[Workflow] = deque(maxlen=MAX_WORKFLOWS)
        self._current: dict[str, Workflow] = {}  # workflow_id -> Workflow
        self._debug_logs: dict[str, str] = {}  # workflow_id -> debug text
        self._debug_order: deque[str] = deque(maxlen=MAX_DEBUG_LOGS)
        self._lock = Lock()
        self._stats = {
            "webhooks_received": 0,
            "webhooks_skipped": 0,
            "workflows_completed": 0,
            "workflows_failed": 0,
            "total_tool_calls": 0,
            "total_escalations": 0,
            "started_at": time.time(),
        }

    def log(self, level: str, message: str, workflow_id: str = "", **extra):
        with self._lock:
            self._events.append(ActivityEvent(
                timestamp=time.time(), level=level, message=message,
                workflow_id=workflow_id, extra=extra,
            ))

    def webhook_received(self):
        with self._lock:
            self._stats["webhooks_received"] += 1

    def webhook_skipped(self):
        with self._lock:
            self._stats["webhooks_skipped"] += 1

    def start_workflow(self, workflow_id: str, trigger: str, target: str, project: str) -> Workflow:
        with self._lock:
            wf = Workflow(
                id=workflow_id, started=time.time(),
                trigger=trigger, target=target, project=project,
            )
            self._current[workflow_id] = wf
            self._workflows.append(wf)
            return wf

    def set_plan(self, workflow_id: str, steps: int):
        with self._lock:
            if workflow_id in self._current:
                self._current[workflow_id].plan_steps = steps

    def step_completed(self, workflow_id: str, model: str = ""):
        with self._lock:
            if workflow_id in self._current:
                wf = self._current[workflow_id]
                wf.completed_steps += 1
                if model and model not in wf.models_used:
                    wf.models_used.append(model)

    def tool_called(self, workflow_id: str):
        with self._lock:
            self._stats["total_tool_calls"] += 1
            if workflow_id in self._current:
                self._current[workflow_id].tool_calls += 1

    def add_phase(self, workflow_id: str, phase: str):
        with self._lock:
            if workflow_id in self._current:
                self._current[workflow_id].phases.append(phase)

    def set_gather_summary(self, workflow_id: str, summary: str):
        with self._lock:
            if workflow_id in self._current:
                self._current[workflow_id].gather_summary = summary

    def start_step(self, workflow_id: str, number: int, description: str,
                   tier: str = "", model: str = "", tools_count: int = 0):
        with self._lock:
            if workflow_id in self._current:
                step = StepInfo(
                    number=number, description=description,
                    tier=tier, model=model, tools_count=tools_count,
                    started=time.time(),
                )
                self._current[workflow_id].steps.append(step)

    def finish_step(self, workflow_id: str, step_number: int,
                    status: str = "completed", actions: list[dict] | None = None):
        with self._lock:
            if workflow_id in self._current:
                for step in self._current[workflow_id].steps:
                    if step.number == step_number:
                        step.finished = time.time()
                        step.status = status
                        if actions:
                            step.actions = [
                                {
                                    "tool": a.get("tool", ""),
                                    "result_preview": (a.get("result", "") or "")[:150],
                                    "error": a.get("error", False),
                                }
                                for a in actions
                                if a.get("tool") not in ("_text_response", "_empty_response")
                            ]
                        break

    def mark_step_escalated(self, workflow_id: str, step_number: int):
        with self._lock:
            if workflow_id in self._current:
                for step in self._current[workflow_id].steps:
                    if step.number == step_number:
                        step.escalated = True
                        break

    def escalation(self, workflow_id: str):
        with self._lock:
            self._stats["total_escalations"] += 1
            if workflow_id in self._current:
                self._current[workflow_id].escalations += 1

    def finish_workflow(self, workflow_id: str, status: str = "completed", error: str = ""):
        with self._lock:
            if workflow_id in self._current:
                wf = self._current[workflow_id]
                wf.status = status
                wf.finished = time.time()
                wf.error = error
                del self._current[workflow_id]
                if status == "completed":
                    self._stats["workflows_completed"] += 1
                else:
                    self._stats["workflows_failed"] += 1

    def get_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            events = list(self._events)[-limit:]
            return [e.to_dict() for e in reversed(events)]

    def get_workflows(self, limit: int = 20) -> list[dict]:
        with self._lock:
            wfs = list(self._workflows)[-limit:]
            return [w.to_dict() for w in reversed(wfs)]

    def get_current(self) -> list[dict]:
        with self._lock:
            return [w.to_dict() for w in self._current.values()]

    def store_debug_log(self, workflow_id: str, debug_text: str):
        """Store debug output for a failed workflow."""
        with self._lock:
            # Evict oldest if at capacity
            if len(self._debug_order) >= MAX_DEBUG_LOGS:
                oldest = self._debug_order[0]
                self._debug_logs.pop(oldest, None)
            self._debug_logs[workflow_id] = debug_text
            self._debug_order.append(workflow_id)

    def get_debug_log(self, workflow_id: str) -> str | None:
        with self._lock:
            return self._debug_logs.get(workflow_id)

    def get_stats(self) -> dict:
        with self._lock:
            return {
                **self._stats,
                "uptime_seconds": round(time.time() - self._stats["started_at"]),
                "active_workflows": len(self._current),
            }


# Global singleton
tracker = ActivityTracker()

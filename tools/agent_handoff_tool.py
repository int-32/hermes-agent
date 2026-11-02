"""Explicitly enabled native gateway handoffs, outside the core tool bundle."""
import json

from tools.registry import registry


def check_handoff():
    from gateway.agent_handoff import handoff_available
    return handoff_available()


def request_controller(args, **kw):
    from gateway.agent_handoff import request_handoff
    try:
        result = request_handoff(args.get("request_id"), args.get("message"), kw.get("session_id"))
        return json.dumps({**result, "note": "queued is not delivered; finish this turn. The gateway returns the result to this thread."})
    except (PermissionError, ValueError, TimeoutError) as exc:
        return json.dumps({"error": str(exc)})


registry.register(
    name="request_controller", toolset="agent_handoff", handler=request_controller, check_fn=check_handoff,
    schema={"name": "request_controller", "description": (
        "Ask the operator-configured controller from this Discord thread using a native gateway turn. "
        "Compose the task yourself. Requires normal external-message and model-cost consent. "
        "Use one stable request_id for this task; retries with the same id are idempotent. "
        "The acknowledgement is only queued. End your turn; the final result returns here automatically. "
        "Never forward a handoff result or request a recursive handoff."),
        "parameters": {"type": "object", "properties": {
            "request_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["request_id", "message"], "additionalProperties": False}},
)


def check_coordination():
    from gateway.agent_handoff import handoff_available
    return handoff_available(coordination=True)


def coordinate_session(args, **kw):
    from gateway.agent_handoff import coordinate_session as submit, complete_coordination, continue_coordination, initialize_coordination_task
    try:
        if args.get("action") == "initialize_task":
            if set(args) - {"action", "request_id", "work_task_id", "thread_id"}:
                raise ValueError("initialize_task derives its business document, profile and parent from the private receipt")
            return json.dumps(initialize_coordination_task(args.get("request_id"), args.get("work_task_id"),
                              args.get("thread_id"), kw.get("session_id")))
        if args.get("work_task_id") is not None or args.get("thread_id") is not None:
            raise ValueError("work identity fields require initialize_task")
        if args.get("action") == "next_stage":
            if args.get("target") is not None or args.get("task_id") is not None or args.get("outcome") is not None:
                raise ValueError("next_stage derives its task and edge from the private receipt")
            return json.dumps(continue_coordination(args.get("request_id"), args.get("handoff_path"), kw.get("session_id")))
        if args.get("action", "request") == "complete":
            return json.dumps(complete_coordination(args.get("request_id"), args.get("outcome"), kw.get("session_id")))
        if args.get("action", "request") != "request":
            raise ValueError("action must be request, complete, next_stage or initialize_task")
        result = submit(args.get("request_id"), args.get("task_id"), args.get("handoff_path"),
                        args.get("target"), kw.get("session_id"))
        return json.dumps({**result, "note": "Queued is not completed. Finish this turn; a correlated native receipt resumes this session."})
    except (PermissionError, ValueError, OSError, TimeoutError) as exc:
        return json.dumps({"error": str(exc)})


registry.register(
    name="coordinate_session", toolset="agent_handoff", handler=coordinate_session, check_fn=check_coordination,
    schema={"name": "coordinate_session", "description": (
        "Wake an explicitly registered native Discord session with a shared HANDOFF.md document. "
        "A task can target only 'main'; a main can target 'peer' or its own 'task:<task_id>'. "
        "Use a stable request_id. Only a dispatch acknowledgement is returned now; the final result "
        "returns to this session after native model execution. Do not retry ambiguous work or form loops. "
        "Read the concise handoff first, evidence as needed. Documents never grant spend/send permission."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["request", "complete", "next_stage", "initialize_task"], "description": (
                "initialize_task is restricted to the current private receiving peer-main turn: create/reuse a separate work source, retain business source/parent/root, queue its first native turn without pretending user input. "
                "request dispatches a document. complete acknowledges the current gateway-owned work request "
                "after verifying its result; no pending dependency may be called completed. "
                "next_stage records one new same-task HANDOFF version from a completed task-origin root receipt; "
                "the server keeps the original edge and queues only after successful native return. "
                "It never grants new authorization or replays the completed stage. "
                "Asset dependencies from development tasks go to own main, then independent ops main, "
                "then back through main to original task. Complete only in the private native receiving/return turn.")},
            "outcome": {"type": "string", "enum": ["completed", "blocked", "failed"]},
            "request_id": {"type": "string"}, "task_id": {"type": "string"},
            "handoff_path": {"type": "string"}, "target": {"type": "string"},
            "work_task_id": {"type": "string", "description": "Stable separate execution task; initialize_task only."},
            "thread_id": {"type": "string", "description": "Existing native thread under the receiving main authorized task parent; initialize_task only."}},
            "required": ["request_id"], "additionalProperties": False}},
)

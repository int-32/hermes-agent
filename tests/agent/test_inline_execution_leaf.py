"""Framework lookup/assembly is not a tool attempt; an entered business refusal is."""

import json
from types import SimpleNamespace

import pytest

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools.registry import collect_tool_execution


def test_no_session_database_does_not_claim_search_entry():
    agent = SimpleNamespace(_get_session_db_for_recall=lambda: None)
    with collect_tool_execution("session_search", "search-call") as observation:
        result = INLINE_TOOL_EXECUTORS["session_search"](
            agent, {}, InlineToolContext("task", "search-call")
        )
    assert json.loads(result)["success"] is False
    assert not observation.entered
    assert observation.call_id == "search-call"


@pytest.mark.parametrize("stage", ["import", "lookup", "fixed", "mapping", "binding"])
def test_inline_preflight_failure_never_claims_entry(monkeypatch, stage):
    import agent.inline_tool_executors as inline

    entered = []

    def handler(questions, callback):
        entered.append(questions)

    module = SimpleNamespace(clarify_tool=handler)

    def load(name):
        if stage == "import":
            raise ImportError("fixture module unavailable")
        return module

    monkeypatch.setattr(inline, "import_module", load)
    agent = SimpleNamespace(clarify_callback=None)

    def requires_extra(questions, callback, required):
        entered.append(questions)

    prepare = {
        "import": lambda: None,
        "lookup": lambda: delattr(module, "clarify_tool"),
        "fixed": lambda: delattr(agent, "clarify_callback"),
        "mapping": lambda: None,
        "binding": lambda: setattr(module, "clarify_tool", requires_extra),
    }
    prepare[stage]()
    # The real arg mapping must finish before invoking the business callable.
    executor = (
        inline._coerced(
            "clarify", inline._tool("fixture", "clarify_tool", ("questions",))
        )
        if stage == "mapping"
        else INLINE_TOOL_EXECUTORS["clarify"]
    )
    with collect_tool_execution("clarify", "preflight-call") as observation:
        with pytest.raises((ImportError, AttributeError, TypeError)):
            executor(
                agent, {"questions": []}, InlineToolContext("task", "preflight-call")
            )
    assert entered == []
    assert not observation.entered


@pytest.mark.parametrize("raises", [False, True])
def test_actual_inline_business_error_remains_an_attempt(monkeypatch, raises, tmp_path):
    import tools.clarify_tool as module

    target = tmp_path / "entered"

    def handler(questions, callback):
        target.write_text(json.dumps(questions))
        if raises:
            raise RuntimeError("business exception")
        return '{"error":"business refusal"}'

    monkeypatch.setattr(module, "clarify_tool", handler)
    with collect_tool_execution("clarify", "business-call") as observation:
        if raises:
            with pytest.raises(RuntimeError, match="business exception"):
                INLINE_TOOL_EXECUTORS["clarify"](
                    SimpleNamespace(clarify_callback=None),
                    {"questions": ["question"]},
                    InlineToolContext("task", "business-call"),
                )
        else:
            result = INLINE_TOOL_EXECUTORS["clarify"](
                SimpleNamespace(clarify_callback=None),
                {"questions": ["question"]},
                InlineToolContext("task", "business-call"),
            )
            assert json.loads(result)["error"] == "business refusal"
    assert target.read_text() == '["question"]'
    assert observation.entered and observation.call_id == "business-call"


@pytest.mark.parametrize(
    "name, args",
    [
        ("desktop_preview", {"action": "invalid"}),
        ("desktop_preview", {"action": "read"}),
        ("manage_connections", {"action": "invalid"}),
        ("manage_catalog", {"action": "invalid"}),
        ("setup_beat", {"beat": "invalid"}),
        ("setup_mcp", {"action": "invalid", "server": "isolated"}),
    ],
)
def test_custom_actual_business_refusals_keep_canonical_identity(name, args):
    agent = SimpleNamespace()
    ctx = InlineToolContext("task", "alias-call")
    with collect_tool_execution(name, "alias-call") as observation:
        result = INLINE_TOOL_EXECUTORS[name](agent, args, ctx)
    assert json.loads(result).get("error"), result
    assert (
        observation.entered
        and observation.name == name
        and observation.call_id == "alias-call"
    )
    # Routing preserves the caller's original task, call and message context.
    assert (ctx.effective_task_id, ctx.tool_call_id, ctx.messages) == (
        "task", "alias-call", None,
    )


def test_builtin_memory_attempt_survives_refused_provider_mirror(tmp_path, monkeypatch):
    from agent.memory_manager import MemoryManager
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.memory_tool_store import MemoryStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    manager = MemoryManager()

    class Sink:
        name = "unbound-mirror"
        writes = []

        def get_tool_schemas(self):
            return []

        def on_memory_write(self, *args, **kwargs):
            self.writes.append(args)

    sink = Sink()
    manager.add_provider(sink)
    store = MemoryStore(memory_char_limit=500, user_char_limit=300)
    agent = SimpleNamespace(
        _memory_store=store,
        _memory_manager=manager,
        _build_memory_write_metadata=lambda **kwargs: kwargs,
    )
    mode = set_multiplex_context(True)
    home = set_hermes_home_override(str(tmp_path))
    try:
        with collect_tool_execution("memory", "builtin-call") as observation:
            result = INLINE_TOOL_EXECUTORS["memory"](
                agent,
                {"action": "add", "target": "user", "content": "Leaf fixture."},
                InlineToolContext("task", "builtin-call"),
            )
        assert json.loads(result)["success"]
        assert "Leaf fixture." in (tmp_path / "memories/USER.md").read_text()
        assert not sink.writes and observation.entered
    finally:
        reset_hermes_home_override(home)
        reset_multiplex_context(mode)


@pytest.mark.parametrize("route", ["inline", "sequential"])
@pytest.mark.parametrize("mode", ["invalid-action", "paused", "depth"])
def test_real_delegate_business_refusal_is_still_an_attempt(route, mode):
    from run_agent import AIAgent
    from tools.delegate_tool_registry import is_spawn_paused, set_spawn_paused
    from agent.tool_executor import _resolve_sequential_dispatch, _ToolCallRef

    agent = AIAgent.__new__(AIAgent)
    agent._should_emit_quiet_tool_messages = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    agent._delegate_depth = 100000 if mode == "depth" else 0
    previous = is_spawn_paused()
    set_spawn_paused(mode == "paused")
    args = (
        {"action": "invalid"}
        if mode == "invalid-action"
        else {"goal": "never run a child"}
    )
    try:
        with collect_tool_execution("delegate_task", "delegate-call") as observation:
            if route == "inline":
                result = INLINE_TOOL_EXECUTORS["delegate_task"](
                    agent, args, InlineToolContext("task", "delegate-call")
                )
            else:
                ref = _ToolCallRef(
                    name="delegate_task",
                    args=args,
                    task_id="task",
                    call_id="delegate-call",
                    trace=[],
                )
                result = _resolve_sequential_dispatch(agent, ref, []).execute(args)
        error = json.loads(result)["error"]
        assert {
            "invalid-action": "Unknown action",
            "paused": "paused",
            "depth": "depth limit",
        }[mode] in error
        assert observation.entered and observation.call_id == "delegate-call"
    finally:
        set_spawn_paused(previous)


@pytest.mark.parametrize("route", ["inline", "sequential"])
def test_delegate_hidden_field_assembly_failure_cannot_mint_entry(route):
    from run_agent import AIAgent
    from agent.tool_executor import _resolve_sequential_dispatch, _ToolCallRef

    class BrokenTask(dict):
        def items(self):
            raise ValueError("cannot assemble hidden fields")

    agent = AIAgent.__new__(AIAgent)
    agent._should_emit_quiet_tool_messages = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    args = {"tasks": [BrokenTask(goal="unreachable", acp_args=["hidden"])]}
    with collect_tool_execution("delegate_task", "assembly-call") as observation:
        with pytest.raises(ValueError, match="cannot assemble"):
            if route == "inline":
                INLINE_TOOL_EXECUTORS["delegate_task"](
                    agent, args, InlineToolContext("task", "assembly-call")
                )
            else:
                ref = _ToolCallRef(
                    name="delegate_task",
                    args=args,
                    task_id="task",
                    call_id="assembly-call",
                    trace=[],
                )
                _resolve_sequential_dispatch(agent, ref, []).execute(args)
    assert not observation.entered


def test_uninspectable_inline_business_callable_does_not_claim_proof(
    monkeypatch, tmp_path
):
    import tools.clarify_tool as module

    class Uninspectable:
        @property
        def __signature__(self):
            raise ValueError("opaque callable")

        def __call__(self, questions, callback):
            (tmp_path / "opaque").write_text("actual attempt")
            return '{"error":"business refusal"}'

    monkeypatch.setattr(module, "clarify_tool", Uninspectable())
    with collect_tool_execution("clarify", "opaque-call") as observation:
        result = INLINE_TOOL_EXECUTORS["clarify"](
            SimpleNamespace(clarify_callback=None),
            {"questions": []},
            InlineToolContext("task", "opaque-call"),
        )
    assert json.loads(result)["error"] == "business refusal"
    assert (tmp_path / "opaque").read_text() == "actual attempt"
    assert not observation.entered

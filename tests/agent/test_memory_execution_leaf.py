"""Real provider entry determines execution proof and persisted first-tool selection."""

import json
import sys
import threading
import pytest
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.secret_scope import (
    set_multiplex_context,
    reset_multiplex_context,
    set_secret_scope,
    reset_secret_scope,
)
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tests.fakes.fake_llm_provider import (
    FakeLLMServer,
    ToolCall,
    Text,
    write_hermes_home,
)
from gateway.internal_receipt import first_tool_result

TOOL = "boundary_remember"


class LocalProvider(MemoryProvider):
    def __init__(self, store, barrier=None):
        self.store = store
        self.calls = []
        self.initialized = []
        self.barrier = barrier

    @property
    def name(self):
        return "boundary-local"

    def is_available(self):
        return True

    def get_tool_schemas(self):
        return [
            {
                "name": TOOL,
                "description": "Write isolated memory",
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            }
        ]

    def initialize(self, session_id, **kwargs):
        self.initialized.append({"session_id": session_id, **kwargs})

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append(
            {
                "name": tool_name,
                "args": args,
                "kwargs": kwargs,
                "thread_id": threading.get_ident(),
            }
        )
        with self.store.open("a") as f:
            f.write(args["content"] + "\n")
        if self.barrier:
            self.barrier.wait(timeout=15)
        return json.dumps({"ok": True, "written": args["content"]})


def _run_memory_turn(tmp_path, layout, mode, barrier=None):
    from run_agent import AIAgent
    from hermes_state import SessionDB

    alpha, beta = tmp_path / "profiles/alpha", tmp_path / "profiles/beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    store = tmp_path / "provider-store"
    note = tmp_path / "note.txt"
    note.write_text("actual isolated read")
    provider = LocalProvider(store, barrier if mode == "legal" else None)
    events = []
    previous = sys.getprofile()
    previous_thread = threading.getprofile()

    def observe(frame, event, arg):
        if event == "call":
            name = frame.f_code.co_name
            filename = frame.f_code.co_filename
            if name in {
                "_resolve_sequential_dispatch",
                "resolve_invoke_tool_executor",
                "invoke_tool",
                "execute_tool_calls_concurrent",
                "execute_tool_calls_sequential",
                "caller_scope",
                "handle_tool_call",
                "read_file",
                "dispatch",
            }:
                events.append(
                    {
                        "function": name,
                        "file": filename,
                        "thread": threading.get_ident(),
                        "tool": frame.f_locals.get(
                            "function_name", frame.f_locals.get("tool_name")
                        ),
                    }
                )

    multi = set_multiplex_context(True)
    home = set_hermes_home_override(str(alpha))
    secret = set_secret_scope({}, profile_home=str(alpha))
    try:
        memory = MemoryManager()
        memory.add_provider(provider)
        call = ToolCall(TOOL, {"content": "isolated memory attempt"})
        read = ToolCall("read_file", {"path": str(note)})
        script = (
            [call, read, Text("done")]
            if layout == "sequential"
            else [
                ToolCall(
                    TOOL,
                    {"content": "isolated memory attempt"},
                    parallel=[("read_file", {"path": str(note)})],
                ),
                Text("done"),
            ]
        )
        with FakeLLMServer(script) as peer:
            write_hermes_home(alpha, peer.base_url)
            write_hermes_home(beta, peer.base_url)
            db = SessionDB(tmp_path / "state.db")
            # Initialize after AIAgent has generated its real session ID, before any turn.
            caller = beta if mode == "mismatch" else alpha
            token = set_hermes_home_override(str(caller))
            scope = set_secret_scope({}, profile_home=str(caller))
            agent = None
            try:
                agent = AIAgent(
                    model="fake-model",
                    provider="custom",
                    base_url=peer.base_url,
                    api_key="sk-fake-e2e",
                    enabled_toolsets=["file", "memory"],
                    quiet_mode=True,
                    skip_memory=False,
                    memory_manager=memory,
                    skip_context_files=True,
                    max_iterations=6,
                    session_db=db,
                )
                assert memory.has_tool(TOOL) and TOOL in agent.valid_tool_names
                assert agent._memory_manager is memory
                if mode != "unbound":
                    owner = set_hermes_home_override(str(alpha))
                    owner_secret = set_secret_scope({}, profile_home=str(alpha))
                    try:
                        memory.initialize_all(
                            session_id=agent.session_id,
                            platform="api_server",
                            hermes_home=str(alpha),
                        )
                    finally:
                        reset_secret_scope(owner_secret)
                        reset_hermes_home_override(owner)
                sys.setprofile(observe)
                if barrier is None:
                    threading.setprofile(observe)
                try:
                    result = agent.run_conversation(
                        "Run isolated memory attempt then read the note."
                    )
                finally:
                    sys.setprofile(previous)
                    if barrier is None:
                        threading.setprofile(previous_thread)
                tools = [m for m in result["messages"] if m.get("role") == "tool"]
                memory_result = next(m for m in tools if m.get("tool_name") == TOOL)
                read_result = next(
                    m for m in tools if m.get("tool_name") == "read_file"
                )
                proof = (memory_result.get("display_metadata") or {}).get(
                    "tool_execution"
                )
                first = first_tool_result(result)
                record = {
                    "layout": layout,
                    "mode": mode,
                    "calls": provider.calls,
                    "initialized": provider.initialized,
                    "store_exists": store.exists(),
                    "store_content": store.read_text() if store.exists() else None,
                    "memory_result": memory_result,
                    "read_result": read_result,
                    "proof": proof,
                    "first_tool": first,
                    "result": result,
                    "events": events,
                    "session_id": agent.session_id,
                    "restored_first": first_tool_result(
                        {
                            "messages": db.get_messages_as_conversation(
                                agent.session_id
                            ),
                            "current_turn_user_idx": result["current_turn_user_idx"],
                        }
                    ),
                    "main_requests": peer.main_requests(),
                }
                out = tmp_path / f"evidence-{mode}-{layout}.json"
                out.write_text(
                    json.dumps(record, default=str, ensure_ascii=False, indent=2)
                )
                print(
                    "MEMORY_BOUNDARY "
                    + json.dumps(record, default=str, ensure_ascii=False)
                )
                assert "actual isolated read" in read_result["content"]
                assert (read_result.get("display_metadata") or {}).get("tool_execution")
                if mode == "legal":
                    assert (
                        len(provider.calls) == 1
                        and store.read_text() == "isolated memory attempt\n"
                    )
                    assert proof and first["name"] == TOOL
                    assert record["restored_first"]["name"] == TOOL
                else:
                    assert provider.calls == [] and not store.exists()
                    assert "refused" in memory_result["content"]
                    assert not proof, "REFUSAL MUST NOT CLAIM PROVIDER ENTRY"
                    assert first and first["name"] == "read_file"
                    assert record["restored_first"]["name"] == "read_file"
                return record
            finally:
                if agent:
                    agent.close()
                db.close()
                reset_secret_scope(scope)
                reset_hermes_home_override(token)
    finally:
        reset_secret_scope(secret)
        reset_hermes_home_override(home)
        reset_multiplex_context(multi)


@pytest.mark.parametrize("layout", ["sequential", "batch"])
@pytest.mark.parametrize("mode", ["unbound", "mismatch", "legal"])
def test_real_memory_leaf_boundary(tmp_path, layout, mode):
    _run_memory_turn(tmp_path, layout, mode)


@pytest.mark.parametrize("refusal", ["unbound", "mismatch"])
def test_concurrent_real_producers_keep_first_tool_and_persistence(tmp_path, refusal):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def run(mode):
        root = tmp_path / mode
        root.mkdir()
        return _run_memory_turn(root, "sequential", mode, barrier)

    read_threads = []
    previous = threading.getprofile()

    def observe_workers(frame, event, arg):
        if (
            event == "call"
            and frame.f_code.co_name == "read_file_tool"
            and frame.f_code.co_filename.endswith("/tools/file_tools.py")
            and frame.f_locals.get("path") == str(tmp_path / refusal / "note.txt")
        ):
            read_threads.append(threading.get_ident())
            barrier.wait(timeout=15)

    threading.setprofile(observe_workers)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(run, [refusal, "legal"]))
    finally:
        threading.setprofile(previous)
    denied, allowed = records
    assert denied["calls"] == [] and not denied["store_exists"]
    assert (
        denied["first_tool"]["name"] == denied["restored_first"]["name"] == "read_file"
    )
    assert allowed["first_tool"]["name"] == allowed["restored_first"]["name"] == TOOL
    denied_read = set(read_threads)
    assert denied_read and allowed["calls"][0]["thread_id"] not in denied_read
    assert not barrier.broken and barrier.n_waiting == 0
    print(
        "CONCURRENT_MEMORY "
        + json.dumps(
            {
                "refusal": refusal,
                "denied_read_threads": list(denied_read),
                "allowed_provider_thread": allowed["calls"][0]["thread_id"],
                "barrier_parties": barrier.parties,
            }
        )
    )


@pytest.mark.parametrize("mode", ["unbound", "mismatch", "legal"])
def test_actual_inline_dispatch_observation(tmp_path, mode):
    # The exact existing caller-profile test seam: real manager + actual inline executor.
    from types import SimpleNamespace
    from agent.inline_tool_executors import resolve_invoke_tool_executor
    from tools.registry import collect_tool_execution

    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    multi = set_multiplex_context(True)
    home = set_hermes_home_override(str(alpha))
    secret = set_secret_scope({}, profile_home=str(alpha))
    try:
        provider = LocalProvider(tmp_path / "inline-store")
        manager = MemoryManager()
        manager.add_provider(provider)
        # No session identity is asserted by this dispatch-only fixture. initialize_all needs a
        # provider session string but the provider does not use it; do not mint a native receipt.
        if mode != "unbound":
            manager.initialize_all(
                session_id="", platform="api_server", hermes_home=str(alpha)
            )
        caller = beta if mode == "mismatch" else alpha
        token = set_hermes_home_override(str(caller))
        scope = set_secret_scope({}, profile_home=str(caller))
        try:
            agent = SimpleNamespace(_memory_manager=manager, _gateway_session_key="")
            executor = resolve_invoke_tool_executor(agent, TOOL)
            assert executor is not None
            with collect_tool_execution(TOOL, "") as observation:
                result = executor(
                    agent, {"content": "actual inline attempt"}, SimpleNamespace()
                )
            record = {
                "mode": mode,
                "caller": "resolve_invoke_tool_executor",
                "result": result,
                "entered": observation.entered,
                "calls": provider.calls,
                "store_exists": provider.store.exists(),
                "store_content": provider.store.read_text()
                if provider.store.exists()
                else None,
                "executor_file": executor.__code__.co_filename,
                "manager_file": sys.modules["agent.memory_manager"].__file__,
                "precision": "dispatch-only ContextVar fixture, no native identity or receipt acceptance",
            }
            (tmp_path / f"evidence-inline-{mode}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2)
            )
            print("INLINE_BOUNDARY " + json.dumps(record, ensure_ascii=False))
            if mode == "legal":
                assert (
                    len(provider.calls) == 1
                    and observation.entered
                    and provider.store.read_text() == "actual inline attempt\n"
                )
            else:
                assert (
                    provider.calls == []
                    and not provider.store.exists()
                    and "refused" in result
                )
                assert not observation.entered, "REFUSAL MUST NOT CLAIM PROVIDER ENTRY"
        finally:
            reset_secret_scope(scope)
            reset_hermes_home_override(token)
    finally:
        reset_secret_scope(secret)
        reset_hermes_home_override(home)
        reset_multiplex_context(multi)


@pytest.mark.parametrize("raises", [False, True])
def test_real_provider_error_remains_a_business_attempt(tmp_path, raises):
    from tools.registry import collect_tool_execution

    class RefusingProvider(LocalProvider):
        def handle_tool_call(self, tool_name, args):
            self.store.write_text(args["content"])
            if raises:
                raise RuntimeError("provider business exception")
            return '{"error":"provider business refusal"}'

    home = set_hermes_home_override(str(tmp_path))
    secret = set_secret_scope({}, profile_home=str(tmp_path))
    multi = set_multiplex_context(True)
    try:
        provider = RefusingProvider(tmp_path / "provider-error")
        manager = MemoryManager()
        manager.add_provider(provider)
        manager.initialize_all(
            session_id="", platform="api_server", hermes_home=str(tmp_path)
        )
        with collect_tool_execution(TOOL, "provider-call") as observation:
            result = manager.handle_tool_call(
                TOOL, {"content": "real effect"}, caller_gsk=""
            )
        assert "provider business" in json.loads(result)["error"]
        assert provider.store.read_text() == "real effect"
        assert observation.entered and observation.call_id == "provider-call"
    finally:
        reset_multiplex_context(multi)
        reset_secret_scope(secret)
        reset_hermes_home_override(home)

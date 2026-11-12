"""Compatibility contract for explicit hard-stop producers."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from agent.interrupt_compat import request_hard_interrupt


class _ModernAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []

    def hard_interrupt(
        self,
        message: str | None = None,
        *,
        tool_reason: str | None = None,
    ) -> None:
        self.calls.append(("hard", message, tool_reason))

    def interrupt(self, message: str | None = None) -> None:
        self.calls.append(("soft", message, None))


class _LegacyAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def interrupt(self, message: str | None = None) -> None:
        self.calls.append(("legacy", message))


def test_explicit_producer_prefers_feature_detected_hard_interrupt() -> None:
    agent = _ModernAgent()

    assert request_hard_interrupt(agent, "stop now") is True

    assert agent.calls == [("hard", "stop now", None)]


def test_safe_tool_reason_only_reaches_supporting_modern_agent() -> None:
    modern = _ModernAgent()
    legacy = _LegacyAgent()

    assert request_hard_interrupt(
        modern, "private diagnostic", tool_reason="fixed category"
    )
    assert request_hard_interrupt(
        legacy, "private diagnostic", tool_reason="fixed category"
    )

    assert modern.calls == [("hard", "private diagnostic", "fixed category")]
    assert legacy.calls == [("legacy", "private diagnostic")]


def test_explicit_producer_falls_back_to_old_interrupt_signature() -> None:
    agent = _LegacyAgent()

    assert request_hard_interrupt(agent, "stop now") is True

    assert agent.calls == [("legacy", "stop now")]


def test_explicit_producer_reports_unsupported_agent() -> None:
    assert request_hard_interrupt(object(), "stop now") is False


def test_dynamic_proxy_does_not_fabricate_hard_interrupt_support() -> None:
    agent = MagicMock()

    assert request_hard_interrupt(agent, "stop now") is True

    agent.interrupt.assert_called_once_with("stop now")
    agent.hard_interrupt.assert_not_called()


def test_inherited_hard_interrupt_bypasses_legacy_subclass_override() -> None:
    from run_agent import AIAgent

    class LegacySubclass(AIAgent):
        def __init__(self) -> None:
            self.legacy_calls: list[str | None] = []
            self._hard_interrupt_requested = threading.Event()
            self._pending_redirect_lock = threading.RLock()
            self._pending_redirect = None
            self._execution_thread_id = None
            self._interrupt_thread_signal_pending = False
            self._tool_worker_threads: set[int] = set()
            self._tool_worker_threads_lock = threading.Lock()
            self._active_children: list[object] = []
            self._active_children_lock = threading.Lock()
            self.quiet_mode = True
            self.api_mode = "test"

        def interrupt(self, message: str | None = None) -> None:  # type: ignore[override]
            self.legacy_calls.append(message)

    agent = LegacySubclass()

    assert request_hard_interrupt(agent, "stop now") is True

    assert agent.legacy_calls == []
    assert agent._hard_interrupt_requested.is_set()
    assert agent._interrupt_requested is True
    assert agent._interrupt_message == "stop now"


def test_tui_subagent_interrupt_is_an_explicit_hard_stop() -> None:
    from tools import delegate_tool

    agent = _ModernAgent()
    subagent_id = "sa-hard-stop-test"
    with delegate_tool._active_subagents_lock:
        delegate_tool._active_subagents[subagent_id] = {"agent": agent}
    try:
        assert delegate_tool.interrupt_subagent(subagent_id) is True
    finally:
        with delegate_tool._active_subagents_lock:
            delegate_tool._active_subagents.pop(subagent_id, None)

    assert len(agent.calls) == 1
    assert agent.calls[0][0] == "hard"
    assert subagent_id in agent.calls[0][1]


class _ZeroArgAgent:
    """The ``ManagedWorker.interrupt(self)`` shape: a stop that carries no message."""

    def __init__(self) -> None:
        self.calls = 0

    def interrupt(self) -> None:
        self.calls += 1


def test_zero_arg_interrupt_is_stopped_without_the_message() -> None:
    agent = _ZeroArgAgent()

    assert request_hard_interrupt(agent, "Profile stopping", tool_reason="gateway shutdown") is True

    assert agent.calls == 1


def test_message_still_reaches_an_interrupt_that_takes_it() -> None:
    class _VarArgs:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def interrupt(self, *args) -> None:
            self.calls.append(args)

    legacy, var_args = _LegacyAgent(), _VarArgs()

    assert request_hard_interrupt(legacy, "stop now", tool_reason="fixed category") is True
    assert request_hard_interrupt(var_args, "stop now") is True

    assert legacy.calls == [("legacy", "stop now")]
    assert var_args.calls == [("stop now",)]


def test_accepts_keyword_contract() -> None:
    from agent.interrupt_compat import _accepts_keyword

    def named(message=None, *, tool_reason=None): ...
    def var_kw(message=None, **kwargs): ...
    def positional_only(tool_reason, /): ...
    def absent(message=None): ...

    assert _accepts_keyword(named, "tool_reason") is True
    assert _accepts_keyword(var_kw, "tool_reason") is True
    assert _accepts_keyword(positional_only, "tool_reason") is False
    assert _accepts_keyword(absent, "tool_reason") is False
    assert _accepts_keyword(object(), "tool_reason") is False

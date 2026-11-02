"""Native endpoints and guarded failed-run publication of existing compression.

The model's worker SID remains strict. A live agent is evidence of execution, not
permission to switch conversations: only the DB's compression publication can advance it.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sqlite3


@dataclass
class NativeTurn:
    runner: object
    key: str
    sid: str
    home: Path
    generation: int | None
    agent: object = None
    job: object = None


NATIVE_TURN = ContextVar("gateway_native_endpoint_turn", default=None)
logger = logging.getLogger(__name__)


def _generation(runner, key):
    read = getattr(runner, "_current_session_run_generation", None)
    return read(key) if read else None


def bind_native_agent(runner, ctx, agent):
    turn = NATIVE_TURN.get()
    if turn is not None:
        if (turn.runner is not runner or turn.key != ctx.session_key or turn.sid != ctx.session_id
                or turn.generation != ctx.run_generation):
            raise PermissionError("resolved agent disagrees with the native endpoint turn")
        turn.agent = agent


def _published_endpoint(home, key, source, anchor, accepted_sid=None):
    """One read transaction: official compression edges, live tip and inherited route identity."""
    from gateway.agent_handoff import _route_anchor
    from hermes_state_compression import _CHAIN_STEP_SQL, _walk_compression_chain
    from scripts.kanban_registry_records import checked_path
    path = checked_path(Path(home) / "state.db")
    if not path.is_file():
        raise PermissionError("native compression store missing")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        chain = _walk_compression_chain(anchor, lambda sid: db.execute(_CHAIN_STEP_SQL, (sid,)).fetchone())
        if accepted_sid is not None:
            accepted = _walk_compression_chain(accepted_sid, lambda sid: db.execute(_CHAIN_STEP_SQL, (sid,)).fetchone())
            # An accepted predecessor already reaching entry needs no endpoint adoption.
            # Preserve that approved-entry contract, including pre-publication legacy stores.
            if anchor in accepted:
                return anchor
            if accepted[-1] != chain[-1]:
                raise PermissionError("accepted request left the native compression publication")
        if len(chain) == 1:
            return anchor
        rows = [dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()) for sid in chain]
        tip = rows[-1]
        has_handoff = db.execute("SELECT 1 FROM messages WHERE session_id=? LIMIT 1", (tip["id"],)).fetchone()
        if tip["ended_at"] is not None or tip["end_reason"] is not None or tip["message_count"] < 1 or not has_handoff:
            raise PermissionError("native compression tip is not a live published handoff")
        if any(row["ended_at"] is None or row["end_reason"] != "compression" for row in rows[:-1]):
            raise PermissionError("native compression parent was not durably closed")
        for row in rows:
            origin = json.loads(row["origin_json"] or "null")
            if (row["session_key"] != key or row["profile_name"] != (source.profile or "default")
                    or row["user_id"] != source.user_id or not isinstance(origin, dict)
                    or _route_anchor(origin) != _route_anchor(source.to_dict())):
                raise PermissionError("native compression publication changed key/owner/profile/route")
        return tip["id"]


def _entry_identity(runner, entry):
    from gateway.agent_handoff import _route_anchor
    source = runner._restored_source(entry)
    return entry.session_id, _route_anchor(source.to_dict()), Path(runner._resolve_profile_home_for_source(source)).resolve()


def _verify_execution(turn, runner, key, home, sid):
    if turn is None or turn.key != key:
        return
    if (turn.runner is not runner or Path(turn.home).resolve() != home
            or (sid != turn.sid and turn.generation is None)
            or (turn.generation is not None and _generation(runner, key) != turn.generation)):
        raise PermissionError("native endpoint key/home/generation changed")
    if turn.agent is not None:
        agent = turn.agent
        path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        if (agent.session_id != sid or (path is None and sid != turn.sid)
                or (path is not None and Path(path).resolve() != home / "state.db")):
            raise PermissionError("native endpoint disagrees with the executing agent/publication")


async def effective_endpoint(runner, key, *, missing="registered native session not present", accepted_sid=None):
    """Return a checked source/SID; memory entry and durable route remain approved anchors.

    Recheck identity, generation and execution after every off-loop lookup. A concurrent
    publication/reroute fails closed for this query; callers never repair it by changing entry.
    """
    from gateway.session_identity import replace_source
    # Accepted journal associations are routing facts, not the identity of a current
    # requester. Only background consumers supply this anchor; tool authorization never does.
    turn = NATIVE_TURN.get() if accepted_sid is None else None
    # Scope entry is not yet execution proof. Only the common agent-resolution boundary
    # publishes an executing agent; direct/off-turn endpoint reads still use the approved entry.
    if turn is not None and turn.agent is None:
        turn = None
    generation = _generation(runner, key)
    entry = await runner.async_session_store.lookup_by_session_key(key)
    if not entry or not entry.origin:
        raise PermissionError(missing)
    identity = _entry_identity(runner, entry)
    anchor, _, home = identity
    source = replace_source(runner._restored_source(entry))
    # Ordinary off-turn queries retain the entry endpoint. An accepted journal anchor
    # can also prove the same forward publication, without impersonating a model turn.
    active = turn is not None and turn.key == key
    resolve_publication = active or accepted_sid is not None
    sid = await asyncio.to_thread(_published_endpoint, home, key, source, anchor, accepted_sid) if resolve_publication else anchor
    _verify_execution(turn, runner, key, home, sid)
    current = await runner.async_session_store.lookup_by_session_key(key)
    if (not current or _entry_identity(runner, current) != identity
            or _generation(runner, key) != generation):
        raise PermissionError("native endpoint changed during resolution")
    # The tool's bound turn can start at an earlier segment even when post-run sync has
    # already advanced entry. Require that earlier anchor to publish this very same tip.
    if turn is not None and turn.key == key and turn.sid != anchor:
        if await asyncio.to_thread(_published_endpoint, home, key, source, turn.sid) != sid:
            raise PermissionError("native endpoint left its bound compression lineage")
    published = await asyncio.to_thread(_published_endpoint, home, key, source, anchor, accepted_sid) if resolve_publication else anchor
    if published != sid or _entry_identity(runner, current) != identity or _generation(runner, key) != generation:
        raise PermissionError("native endpoint publication changed during resolution")
    live = getattr(getattr(runner, "session_store", None), "_entries", None)
    if live is not None and (key not in live or _entry_identity(runner, live[key]) != identity):
        raise PermissionError("native endpoint routing entry changed during resolution")
    _verify_execution(turn, runner, key, home, sid)
    return source, sid


def _publish_failed_compression(runner, turn, source, sid):
    """Compare-and-publish this failed run's existing DB child, without a transcript write."""
    from gateway.agent_handoff import _route_anchor
    home = Path(turn.home).resolve()
    expected = _route_anchor(source.to_dict())
    store = runner.session_store
    with store._lock:
        entry = store._entries.get(turn.key)
        if (entry is None or entry.session_id not in {turn.sid, sid}
                or _entry_identity(runner, entry) != (entry.session_id, expected, home)
                or _generation(runner, turn.key) != turn.generation):
            raise PermissionError("failed native turn lost its original routing entry/generation")
        if _published_endpoint(home, turn.key, source, turn.sid) != sid:
            raise PermissionError("failed native turn publication changed before recovery")
        _verify_execution(turn, runner, turn.key, home, sid)
        if entry.session_id == sid:
            return
        entry.session_id = sid
    store._save_entries()
    store._record_gateway_session_peer(sid, turn.key, source)


async def recover_failed_compression(runner):
    """Before native scope exits, retain a proven compression even if the worker raised.

    Recovery failure preserves the original exception and routing state. A completed
    worker's agent + native run are proof; neither journal input nor cached SIDs grant it.
    """
    from gateway.agent_handoff import _route_anchor, _same_home
    from gateway.session_context import _SESSION_NATIVE_SOURCE
    turn = NATIVE_TURN.get()
    if turn is None or turn.runner is not runner or turn.agent is None or turn.generation is None:
        return
    try:
        current = _SESSION_NATIVE_SOURCE.get()
        source, sid = await effective_endpoint(runner, turn.key)
        if (current is None or _route_anchor(current.to_dict()) != _route_anchor(source.to_dict())
                or runner._session_key_for_source(current) != turn.key
                or not await asyncio.to_thread(_same_home, runner._resolve_profile_home_for_source(current), turn.home)):
            raise PermissionError("failed native turn source/key/home changed")
        if sid != turn.sid:
            _publish_failed_compression(runner, turn, source, sid)
    except Exception:
        logger.warning("Refused failed native compression recovery for %s", turn.key, exc_info=True)


async def run_native_model(runner, message, context_prompt, history, source, sid, turn_kwargs):
    """Own each actual native model scope until exception recovery has also settled."""
    from gateway.session_context import native_model_turn_scope
    job = turn_kwargs.pop("native_job", None)
    with native_model_turn_scope(runner, source, turn_kwargs.get("session_key") or "", sid,
                                 turn_kwargs.get("run_generation"), job=job):
        try:
            return await runner._run_agent_inner(message, context_prompt, history, source, sid, **turn_kwargs)
        except Exception:
            await recover_failed_compression(runner)
            raise


async def verify_current_source(service, key, sid, home, current):
    from gateway.agent_handoff import _continues, _route_anchor, _same_home
    if current is None:
        raise PermissionError("requires the current native gateway source, not session ids or environment")
    stored, effective_sid = await effective_endpoint(service.runner, key)
    if _route_anchor(current.to_dict()) != _route_anchor(stored.to_dict()):
        raise PermissionError("current native requester or route differs from the card session origin")
    identity = service.runner._canonicalize(current)
    if (identity is None or identity.runtime_profile != (current.profile or "default")
            or service.runner._session_key_for_source(current) != key):
        raise PermissionError("current native source disagrees with canonical route")
    actual_home = service.runner._resolve_profile_home_for_source(current)
    if (not await asyncio.to_thread(_same_home, actual_home, home)
            or not await asyncio.to_thread(_continues, actual_home, sid, effective_sid)):
        raise PermissionError("caller home or bound native turn changed")


async def native_request(service, key, sid, home, current, coordination, completion, args):
    await verify_current_source(service, key, sid, home, current)
    if completion == "initialize_task":
        from gateway.coordination_task_bootstrap import initialize_task
        return await initialize_task(service, key, sid, home, *args)
    if completion == "next_stage":
        from gateway.coordination_stages import next_stage
        return await next_stage(service, key, sid, home, *args)
    if completion:
        from gateway.coordination_receipts import complete_turn
        return await complete_turn(service, key, sid, home, *args)
    if coordination:
        from gateway.agent_coordination import submit_document
        return await submit_document(service, key, sid, home, *args)
    return await service.submit(key, sid, home, *args)

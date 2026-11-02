"""Opt-in, origin-bound gateway handoffs. No CLI agent, Bot Chat, or helper gateway.

The model supplies only a request id and untrusted task text. Routing comes from the
live session binding and operator policy. Claimed work is never retried after a crash.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import logging
import re
import sqlite3
import time
import weakref
from pathlib import Path

from agent.estop import is_engaged
from gateway.config import Platform
from gateway.internal_receipt import InternalTurnReceipt
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_identity import clear_identity
from gateway.runtime_ownership import canonical_home, process_ownership
from gateway.wake import WakeNotAccepted, admit_internal_event

logger = logging.getLogger(__name__)
_service = None
# Profile retirement and close already serialize on the actual ownership mutex.
_HANDOFF_OWNER_LOCK = process_ownership._mutex


def completion_owner(service):
    from gateway import status
    if _service is not service or not status.owns_gateway_runtime_lock() or not service.runner._running:
        raise PermissionError('private completion belongs to an obsolete native owner')
    return process_ownership._handles[canonical_home(status._get_process_hermes_home())]


def _same_home(left, right):
    return Path(left).resolve() == Path(right).resolve()


def _route_anchor(source):
    source = source or {}
    return tuple(source.get(k) or ("default" if k == "profile" else "") for k in (
        "platform", "chat_id", "thread_id", "parent_chat_id", "scope_id", "user_id", "profile", "chat_type"))


def _continues(home, old, current):
    from gateway.native_session_identity import session_continues
    return session_continues(home, old, current)


def validate_request(request_id, message):
    if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id) is None:
        raise ValueError("request_id must be 8-80 ASCII letters, digits, underscores or hyphens")
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise ValueError("message must contain 1-8000 characters")


class HandoffJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            for request_id, raw in db.execute("SELECT id, record FROM requests").fetchall():
                record = json.loads(raw)
                if record["status"] in {"running", "returning"}:
                    record.update(status="ambiguous", error="gateway restarted during execution or send; inspect before recovery")
                    db.execute("UPDATE requests SET record=? WHERE id=?", (json.dumps(record), request_id))
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT record FROM requests WHERE id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def enqueue(self, record):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._enqueue(db, record)

    def _enqueue(self, db, record):
        row = db.execute("SELECT record FROM requests WHERE id=?", (record["request_id"],)).fetchone()
        if row:
            existing = json.loads(row[0])
            same_task = all(existing.get(k) == record.get(k) for k in (
                "request_id", "message", "source_key", "caller_home", "target_key",
                "kind", "source_endpoint", "target_endpoint", "source_profile", "target_profile", "lineage"))
            same_task = same_task and all(existing.get(k) == record.get(k) for k in (
                "protocol", "parent_request_id", "root_request_id", "producer_admission_id", "stage_after_request_id"))
            if not same_task or _route_anchor(existing.get("origin")) != _route_anchor(record.get("origin")):
                raise ValueError("request_id reused with a different origin or message")
            return existing
        producer = record.get('producer_admission_id')
        if producer and db.execute("SELECT 1 FROM requests WHERE json_extract(record, '$.producer_admission_id')=?",
                                   (producer,)).fetchone():
            raise PermissionError('watch admission already has its private work request')
        parent_id = record.get("parent_request_id")
        if parent_id:
            parent_row = db.execute("SELECT record FROM requests WHERE id=?", (parent_id,)).fetchone()
            parent = json.loads(parent_row[0]) if parent_row else {}
            if (parent.get("status") not in {"running", "waiting_children"}
                    or parent.get("target_key") != record["source_key"]
                    or parent.get("root_request_id") != record["root_request_id"]
                    or any(parent.get("document", {}).get(k) != record["document"].get(k)
                           for k in ("task_id", "project"))):
                raise PermissionError("dependency parent is not an active native request")
            siblings = [json.loads(raw[0]) for raw in db.execute("SELECT record FROM requests")]
            if sum(r.get("parent_request_id") == parent_id for r in siblings) >= 4:
                raise PermissionError("bounded dependency dispatch limit reached")
        record = {**record, "status": "queued", "created_at": time.time(), "updated_at": time.time()}
        db.execute("INSERT INTO requests VALUES (?, ?)", (record["request_id"], json.dumps(record)))
        return record

    def update(self, request_id, **changes):
        return self.update_many({request_id: changes})[request_id]

    def update_many(self, changes_by_id, *, guard=None, successor=None, prepare=None):
        updated = {}
        # Decide stage association under the same ownership/SQLite boundary as COMMIT.
        # In particular, a bridge deadline does not stop a previously submitted writer.
        with _HANDOFF_OWNER_LOCK, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if guard:
                guard(db)
            if prepare:
                changes_by_id, successor = prepare(db)
            for request_id, changes in changes_by_id.items():
                row = db.execute("SELECT record FROM requests WHERE id=?", (request_id,)).fetchone()
                record = {**json.loads(row[0]), **changes, "updated_at": time.time()}
                db.execute("UPDATE requests SET record=? WHERE id=?", (json.dumps(record), request_id))
                updated[request_id] = record
            if successor is not None:
                self._enqueue(db, successor)
            if guard:
                guard(db)
        return updated

    def pending(self):
        from gateway.kanban_task_events import needs_observation
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT record FROM requests ORDER BY rowid")]
        return [r for r in records if r["status"] in {"queued", "generated", "waiting_children"}
                or needs_observation(r)]

    def children(self, request_id):
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT record FROM requests ORDER BY rowid")]
        return [r for r in records if r.get("parent_request_id") == request_id]

    def for_admission(self, admission_id):
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT record FROM requests ORDER BY rowid")]
        return [r for r in records if r.get('producer_admission_id') == admission_id]


class HandoffService:
    def __init__(self, runner, home: Path, policy: dict):
        self.runner, self.home, self.policy = runner, Path(home), dict(policy)
        self.coordination_lock = asyncio.Lock()
        self.journal = HandoffJournal(self.home / "runtime" / "agent_handoff.db")

    async def routes(self, source_key, source_sid, caller_home, *, accepted=None):
        """Validate both durable routes each time; source identity is never model text."""
        if self.policy.get("enabled") is not True:
            raise PermissionError("agent handoff disabled")
        from gateway.native_endpoint import effective_endpoint
        source, actual_sid = await effective_endpoint(self.runner, source_key,
            accepted_sid=accepted['source_sid'] if accepted else None)
        target_key = self.policy.get('target_session_key')
        destination, target_sid = await effective_endpoint(self.runner, target_key,
            accepted_sid=accepted['target_sid'] if accepted else None)
        if not await asyncio.to_thread(_continues, self.runner._resolve_profile_home_for_source(source), source_sid, actual_sid):
            raise PermissionError('source session changed outside native compression')
        home_matches = await asyncio.to_thread(
            _same_home, self.runner._resolve_profile_home_for_source(source), caller_home)
        if (source.platform != Platform.DISCORD or source.chat_type != "thread"
                or not source.thread_id or str(source.thread_id) != str(source.chat_id)
                or source.parent_chat_id != self.policy.get("source_parent_channel")
                or (source.profile or "default") != self.policy.get("source_profile")
                or not home_matches
                or destination.platform != Platform.DISCORD
                or (destination.profile or "default") != self.policy.get("target_profile")
                or source_key == target_key
                or self.runner._session_key_for_source(source) != source_key
                or self.runner._session_key_for_source(destination) != target_key):
            raise PermissionError("source profile/channel or controller route is not authorized")
        if self.policy.get("source_profile") == self.policy.get("target_profile"):
            raise PermissionError("handoff loop refused")
        for route, expected_profile, key in ((source, self.policy["source_profile"], source_key),
                                            (destination, self.policy["target_profile"], target_key)):
            if not self.runner._is_user_authorized_for_source(route, allow_adapter_delegation=False):
                raise PermissionError("current gateway authorization refused the route")
            adapter = self.runner._delivery_adapter_for(route)
            if adapter is None:
                raise PermissionError("no native delivery adapter for route")
            probe = SessionSource.from_dict(route.to_dict())
            clear_identity(probe)
            probe._transport_adapter_ref = weakref.ref(adapter)
            identity = self.runner._canonicalize(probe)
            if identity is None or identity.runtime_profile != expected_profile or self.runner._session_key_for_source(probe) != key:
                raise PermissionError("current native routing no longer matches pinned profiles")
            from gateway.native_session_identity import validate_native_session
            sid = actual_sid if route is source else target_sid
            await asyncio.to_thread(validate_native_session, self.runner._resolve_profile_home_for_source(route),
                                    route, key, source_sid if route is source else sid, sid)
        return source, destination, target_sid

    async def submit(self, source_key, source_sid, caller_home, request_id, message):
        validate_request(request_id, message)
        from gateway.agent_coordination import check_legacy_submission
        await check_legacy_submission(self, source_key, request_id)
        source, target, target_sid = await self.routes(source_key, source_sid, caller_home)
        caller_home = await asyncio.to_thread(Path(caller_home).resolve)
        record = await asyncio.to_thread(self.journal.enqueue, {
            "request_id": request_id, "message": message, "source_key": source_key,
            "source_sid": source_sid, "caller_home": str(caller_home),
            "origin": source.to_dict(), "target": target.to_dict(), "target_sid": target_sid,
            "target_key": self.policy["target_session_key"],
        })
        return self.public(record)

    @staticmethod
    def public(record):
        result = {k: record[k] for k in ("request_id", "status", "error", "message_id") if k in record}
        if record.get("kind") == "coordination":
            result.update(document=record["document"], target_endpoint=record["target_endpoint"])
            result.update({k: record[k] for k in ("outcome", "parent_request_id", "root_request_id",
                                                 "continuation_confirmed", "producer_admission_id",
                                                 "request_first_tool", "origin_first_tool", "next_stage", "stage_after_request_id") if k in record})
        return result

    def busy(self, source, key):
        adapter = self.runner._delivery_adapter_for(source)
        return (self.runner._is_session_running(key) or key in adapter._active_sessions
                or self.runner._queue_depth(key, adapter=adapter) > 0)

    async def pinned_routes(self, record):
        from gateway.native_endpoint import _generation
        generations = {k: _generation(self.runner, k) for k in (record['source_key'], record['target_key'])}
        if record.get('kind') == 'coordination':
            from gateway.agent_coordination import pinned_document_routes
            result = await pinned_document_routes(self, record)
        else:
            result = await self._pinned_legacy_routes(record)
        if any(_generation(self.runner, k) != value for k, value in generations.items()):
            raise PermissionError('accepted request generation changed during routing')
        return result

    async def _pinned_legacy_routes(self, record):
        source, target, target_sid = await self.routes(record["source_key"], record["source_sid"], record["caller_home"], accepted=record)
        target_continues = await asyncio.to_thread(
            _continues, self.runner._resolve_profile_home_for_source(target), record["target_sid"], target_sid)
        if (_route_anchor(source.to_dict()) != _route_anchor(record["origin"])
                or _route_anchor(target.to_dict()) != _route_anchor(record["target"])
                or not target_continues or record["target_key"] != self.policy["target_session_key"]):
            raise PermissionError("pinned origin or controller conversation changed; explicit recovery required")
        if target_sid != record["target_sid"]:
            record = await asyncio.to_thread(self.journal.update, record["request_id"], target_sid=target_sid)
        return source, target, record

    async def tick(self):
        if (self.policy.get("execution_enabled") is not True or is_engaged()
                or not self.runner._running or self.runner._draining
                or getattr(self.runner, "_external_drain_active", False)):
            return
        for record in await asyncio.to_thread(self.journal.pending):
            try:
                await self.observe_task_phase(record['request_id'])
                if record['status'] not in {'queued', 'generated', 'waiting_children'}:
                    continue  # Projection retry is never permission to re-execute a terminal request.
                if record["status"] == "queued" and time.time() - record["created_at"] > 900:
                    await asyncio.to_thread(self.journal.update, record["request_id"], status="failed", error="queued_expired")
                    continue
                source, target, record = await self.pinned_routes(record)
                if record.get("protocol") == 2:
                    from gateway.coordination_receipts import tick_document
                    await tick_document(self, record, source, target)
                    continue
                if record["status"] == "queued":
                    if self.busy(target, record["target_key"]):
                        continue
                    record = await self.execute(record, target)
                if record["status"] == "generated":
                    source, _, record = await self.pinned_routes(record)
                    await self.return_result(record, source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Agent handoff failed: request_id=%s", record["request_id"])
                current = await asyncio.to_thread(self.journal.get, record["request_id"])
                status = "ambiguous" if current["status"] in {"running", "returning"} else "failed"
                await asyncio.to_thread(self.journal.update, record["request_id"], status=status, error=type(exc).__name__)
            finally:
                await self.observe_task_phase(record['request_id'])

    async def observe_task_phase(self, request_id):
        from gateway.kanban_task_events import publish_phase
        try:
            await asyncio.to_thread(publish_phase, self, request_id)
        except (PermissionError, OSError, ValueError, sqlite3.Error):
            logger.warning('Native Kanban phase observation deferred: request_id=%s', request_id, exc_info=True)

    async def execute(self, record, target, *, receiving_turn=None):
        request_id = record["request_id"]
        await asyncio.to_thread(self.journal.update, request_id, status="running")
        from gateway.agent_coordination import handoff_prompt
        prompt = handoff_prompt(self, record)
        outcome = await self.native_turn(record, target, prompt, record["target_key"], record["target_sid"],
                                         receiving_turn=receiving_turn)
        if outcome.get("error"):
            return await asyncio.to_thread(self.journal.update, request_id, **outcome)
        if not outcome["generated"] or not outcome["delivered"]:
            return await asyncio.to_thread(self.journal.update, request_id, status="failed", error="no successful native model/delivery receipt")
        generated = await asyncio.to_thread(self.journal.update, request_id, status="generated", response=outcome["response"],
                                            target_message_ids=outcome.get("message_ids", []))
        await self.observe_task_phase(request_id)
        return generated

    async def native_turn(self, record, target, prompt, key, sid, *, phase="request", receiving_turn=None):
        """One admitted native model turn; callers own the durable phase transition."""
        request_id = record["request_id"]
        event = MessageEvent(
            text=prompt,
            source=target, internal=True, allow_gateway_control=False,
            metadata={"gateway_session_key": key, "gateway_session_id": sid,
                      "gateway_session_strict": True},
        )
        receipt = InternalTurnReceipt(asyncio.get_running_loop().create_future(),
                                      {"id": "bot:" + record.get("source_profile", self.policy.get("source_profile", "")),
                                       "name": record.get("source_profile", self.policy.get("source_profile", "")), "is_bot": True})
        if receiving_turn is not None:
            from gateway.native_job import PrivateJob
            receipt.job = PrivateJob(receiving_turn, tuple(record["lineage"]))
        event._internal_turn_receipt = receipt
        adapter = self.runner._delivery_adapter_for(target)
        from gateway.agent_coordination import native_lineage
        async with native_lineage(record), self.runner._plugin_injection_scope(target):
            try:
                await admit_internal_event(adapter, event)
            except WakeNotAccepted:
                return {"status": "failed", "error": "native wake not admitted"}
            await asyncio.to_thread(self.journal.update, request_id, admitted_at=time.time(),
                                    **{phase + "_admitted_at": time.time()})
            try:
                outcome = await asyncio.wait_for(asyncio.shield(receipt.future), timeout=900)
            except TimeoutError:
                return {"status": "ambiguous", "error": "native turn outcome unknown; do not resend"}
        # A queued successor does not invalidate this event's completed private receipt.
        if receipt.interrupted:
            outcome["generated"] = False
        if record.get('producer_admission_id') and outcome.get('first_tool'):
            await asyncio.to_thread(self.journal.update, request_id, **{phase + '_first_tool': outcome['first_tool']})
        return outcome

    async def return_result(self, record, source):
        request_id = record["request_id"]
        location = (f"origin_chat={source.chat_id} | origin_thread={source.thread_id}"
                    if record.get("kind") == "coordination" else f"origin_thread={source.thread_id}")
        text = (f"[Agent handoff result | request_id={request_id} | source_profile={record.get('source_profile', self.policy.get('source_profile'))} "
                f"| target_profile={record.get('target_profile', self.policy.get('target_profile'))} | {location}]\n"
                + ("[Document version] " + json.dumps(record["document"], ensure_ascii=False) + "\n" if record.get("kind") == "coordination" else "")
                + record["response"])
        await asyncio.to_thread(self.journal.update, request_id, status="returning")
        try:
            async with self.runner._plugin_injection_scope(source):
                result = await self.runner._delivery_adapter_for(source).send(
                    source.chat_id, text, metadata={"thread_id": source.thread_id})
        except Exception:
            logger.exception("Agent handoff return outcome unknown: request_id=%s", request_id)
            await asyncio.to_thread(self.journal.update, request_id, status="ambiguous", error="platform send outcome unknown; inspect origin before recovery")
            return
        message_id = str(getattr(result, "message_id", "") or "")
        status = "delivered" if result.success is True and message_id else "ambiguous"
        if result.success is False:
            status = "failed"
        await asyncio.to_thread(self.journal.update, request_id, status=status, message_id=message_id)

    async def watch(self):
        while self.runner._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("Agent handoff watcher stopped; refusing further requests")
                self.policy["enabled"] = False
                return
            await asyncio.sleep(1)


def install_handoff_service(runner):
    global _service
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import get_routing_process_hermes_home
    policy = load_user_config_effective(fail_closed=True).get("agent_handoff") or {}
    if policy.get("enabled") is not True:
        return
    with _HANDOFF_OWNER_LOCK:
        service = HandoffService(runner, get_routing_process_hermes_home(), policy)
        _service = service
    task = asyncio.create_task(service.watch())
    runner._background_tasks.add(task)
    task.add_done_callback(runner._background_tasks.discard)


def clear_handoff_service(runner):
    global _service
    with _HANDOFF_OWNER_LOCK:
        if _service is not None and _service.runner is runner:
            _service = None


def handoff_available(*, coordination=False):
    from hermes_constants import get_hermes_home
    if _service is None or not _service.runner._running or _service.policy.get("enabled") is not True:
        return False
    profile = _service.runner._served_profile_for_home(Path(get_hermes_home()))
    if coordination:
        from gateway.agent_coordination import coordination_policy
        try:
            return profile in coordination_policy(_service)["mains"]
        except PermissionError:
            return False
    return bool(_service.policy.get("source_profile")) and profile == _service.policy.get("source_profile")


def _bound_request(session_id, *, coordination=False, completion=False, args=()):
    """Called only by the gateway's in-process tool thread, never an env-driven CLI."""
    from gateway.session_context import _SESSION_ID, _SESSION_KEY, _UNSET, _SESSION_NATIVE_SOURCE, _SESSION_NATIVE_HOME
    from hermes_constants import get_hermes_home
    service = _service
    if service is None or not handoff_available(coordination=coordination) or _SESSION_KEY.get() is _UNSET or _SESSION_ID.get() != session_id:
        raise PermissionError('requires a bound native gateway model turn')
    home = _SESSION_NATIVE_HOME.get()
    if home is None or not _same_home(home, get_hermes_home()):
        raise PermissionError('requires a bound native gateway model turn in its own profile home')
    loop = service.runner._gateway_loop
    from gateway.native_endpoint import native_request
    operation = native_request(service, _SESSION_KEY.get(), session_id, home, _SESSION_NATIVE_SOURCE.get(),
                               coordination, completion, args)
    future = asyncio.run_coroutine_threadsafe(operation, loop)
    return future.result(timeout=10)


def request_handoff(request_id, message, session_id):
    return _bound_request(session_id, args=(request_id, message))


def coordinate_session(request_id, task_id, handoff_path, target, session_id):
    return _bound_request(session_id, coordination=True, args=(request_id, task_id, handoff_path, target))


def complete_coordination(request_id, outcome, session_id):
    return _bound_request(session_id, coordination=True, completion=True, args=(request_id, outcome))


def continue_coordination(request_id, handoff_path, session_id):
    return _bound_request(session_id, coordination=True, completion="next_stage", args=(request_id, handoff_path))

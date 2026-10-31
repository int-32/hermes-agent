"""Process-local current work, carried by its actual claim or private receiving receipt.

These references never come from event metadata or a model argument. Logical claim
identity and physical model SID/run generation remain separate contracts.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaimedJob:
    authority: object
    ref: object
    live: object
    admission_id: str
    owner_epoch: int
    generation: int

    def validate(self, turn):
        authority = self.authority
        expected = dict(authority_epoch=self.owner_epoch, execution_generation=self.generation,
                        admission_id=self.admission_id)
        if (authority.runner is not turn.runner or authority.epoch != self.owner_epoch
                or self.ref.profile_id != authority.profile_id
                or Path(authority.db.db_path).resolve() != turn.home.resolve() / 'state.db'
                or authority.sessions.get(self.ref.session_id) is not self.live
                or self.live.route != turn.key
                or authority.executing.get(self.ref.session_id) != self.generation
                or self.live.event_stream.execution != expected):
            raise PermissionError("current job no longer owns its stamped admission")


@dataclass(frozen=True)
class PrivateJob:
    document_turn: object
    lineage: tuple

    def validate(self, turn):
        if self.document_turn.key != turn.key or not self.lineage or self.lineage[-1] != turn.key:
            raise PermissionError("private receiving work disagrees with the native turn")


def claimed_job(authority, ref, row):
    if (row['status'] != 'started' or row['owner_epoch'] != authority.epoch
            or row['target_session_id'] != ref.session_id):
        raise PermissionError("current work requires the committed admission claim")
    return ClaimedJob(authority, ref, authority.sessions[ref.session_id], row['admission_id'],
                      row['owner_epoch'], row['generation'])


def current_job():
    from gateway.native_endpoint import NATIVE_TURN
    turn = NATIVE_TURN.get()
    job = turn.job if turn is not None else None
    if job is not None:
        if not isinstance(job, (ClaimedJob, PrivateJob)):
            raise PermissionError("current work requires a process-local receiving reference")
        job.validate(turn)
    return job


def current_document_turn():
    from gateway.coordination_receipts import DOCUMENT_TURN
    job = current_job()
    if isinstance(job, ClaimedJob):
        return None
    return job.document_turn if isinstance(job, PrivateJob) else DOCUMENT_TURN.get()


def current_lineage():
    from gateway.agent_coordination import HANDOFF_LINEAGE
    job = current_job()
    if isinstance(job, ClaimedJob):
        return ()
    return job.lineage if isinstance(job, PrivateJob) else HANDOFF_LINEAGE.get()

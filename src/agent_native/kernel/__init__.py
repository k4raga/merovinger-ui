"""Single main-thread C1/C2 publication boundary; command admission lives separately."""
from .coordinator import (
    AcceptedDecision, BusyPolicy, Coordinator, CoordinatorBusy, CoordinatorUnavailable, CutIdentity,
    DBOutcome, DurableOutcome, DurableSubmission, DurableSubmitPort, MailboxDisposition,
    Phase, PreparedChange, PreparedPreview, PreviewCandidate, PreviewResult, PreviewStatus, PublishedCut, TerminalWitness, RejectedDecision,
    StagedAcceptance, StagedInput, RecoveryHandoff, Status, SubmissionResult, SubmitDisposition,
)

__all__ = [
    "AcceptedDecision", "BusyPolicy", "Coordinator", "CoordinatorBusy", "CoordinatorUnavailable", "CutIdentity",
    "DBOutcome", "DurableOutcome", "DurableSubmission", "DurableSubmitPort", "MailboxDisposition",
    "Phase", "PreparedChange", "PreparedPreview", "PreviewCandidate", "PreviewResult", "PreviewStatus",
    "PublishedCut", "TerminalWitness", "RejectedDecision", "StagedAcceptance", "StagedInput", "RecoveryHandoff", "Status", "SubmissionResult", "SubmitDisposition",
]

from .permissions import CommandPermissionRequest, CommandPermissionLimit, ResolvedCommandPermissions
from ._grant_authority import ActivityOccurrenceCheck, ActivityTerminalPatch, ActivityBacklinkCheck, ActivityCreationEffectCheck, EffectOriginCheck

__all__ += ["CommandPermissionRequest", "CommandPermissionLimit", "ResolvedCommandPermissions",
            "ActivityOccurrenceCheck", "ActivityTerminalPatch", "ActivityBacklinkCheck", "ActivityCreationEffectCheck", "EffectOriginCheck"]

from .host_turn import HostTurn, TurnRegistration
__all__ += ["HostTurn", "TurnRegistration"]

_AGENT_EXPORTS = ("AgentRequestSpec", "AgentRequestReservation", "AgentRequestPreparation",
                  "SealedAgentRequest", "PreparedAgentSend")


def __getattr__(name):
    if name in _AGENT_EXPORTS:
        from . import _agent_requests
        value = getattr(_agent_requests, name)
        globals()[name] = value
        return value
    raise AttributeError(name)


__all__ += list(_AGENT_EXPORTS)

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .redaction import sanitize_diagnostic


class FailureCategory(str, Enum):
    INVALID_INPUT = "invalid_input"
    BROWSER_OFFLINE = "browser_offline"
    AUTH_REQUIRED = "auth_required"
    FRONTEND_NOT_READY = "frontend_not_ready"
    NETWORK = "network"
    BACKEND = "backend"
    SCHEMA_DRIFT = "schema_drift"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"
    IDENTITY_MISSING = "identity_missing"
    IDENTITY_CONFLICT = "identity_conflict"
    GRAPH_AMBIGUOUS = "graph_ambiguous"
    GRAPH_CONVERGENCE = "graph_convergence"
    INVARIANT = "invariant"
    CORRUPT_STATE = "corrupt_state"
    OWNERSHIP = "ownership"
    OWNERSHIP_TIMEOUT = "ownership_timeout"
    CANCELLATION_UNPROVEN = "cancellation_unproven"


_INVARIANT_FAILURE_CATEGORIES = frozenset(
    {
        FailureCategory.SCHEMA_DRIFT,
        FailureCategory.IDENTITY_MISSING,
        FailureCategory.IDENTITY_CONFLICT,
        FailureCategory.GRAPH_AMBIGUOUS,
        FailureCategory.GRAPH_CONVERGENCE,
        FailureCategory.INVARIANT,
        FailureCategory.CORRUPT_STATE,
    }
)


@dataclass(frozen=True, slots=True)
class Failure:
    category: FailureCategory
    message: str
    retryable: bool = False
    external: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.category, FailureCategory):
            raise TypeError("failure category must be a FailureCategory")
        if type(self.message) is not str:
            raise ValueError("failure message must be exactly a string")
        if type(self.retryable) is not bool or type(self.external) is not bool:
            raise ValueError("failure flags must be exactly boolean")
        object.__setattr__(self, "message", sanitize_diagnostic(self.message))

    @property
    def disposition(self) -> str:
        if self.category == FailureCategory.INVALID_INPUT:
            return "invalid_input"
        if self.category == FailureCategory.OWNERSHIP_TIMEOUT:
            return "ownership_timeout"
        if self.category == FailureCategory.CANCELLATION_UNPROVEN:
            return "cancellation_unproven"
        if self.category in _INVARIANT_FAILURE_CATEGORIES or not self.external:
            return "invariant_failure"
        return "external_failure"

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "message": sanitize_diagnostic(self.message),
            "retryable": self.retryable,
            "external": self.external,
        }


class CoreError(RuntimeError):
    category = FailureCategory.INVARIANT
    retryable = False
    external = False

    def as_failure(self) -> Failure:
        return Failure(
            self.category,
            sanitize_diagnostic(str(self)),
            self.retryable,
            self.external,
        )


class InvalidInputError(CoreError):
    category = FailureCategory.INVALID_INPUT


class BrowserOfflineError(CoreError):
    category = FailureCategory.BROWSER_OFFLINE
    retryable = True
    external = True


class AuthenticationRequiredError(CoreError):
    category = FailureCategory.AUTH_REQUIRED
    retryable = True
    external = True


class FrontendNotReadyError(CoreError):
    category = FailureCategory.FRONTEND_NOT_READY
    retryable = True
    external = True


class NetworkError(CoreError):
    category = FailureCategory.NETWORK
    retryable = True
    external = True


class BackendError(CoreError):
    category = FailureCategory.BACKEND
    external = True


class BackendUnavailableError(BackendError):
    retryable = True


class SchemaDriftError(CoreError):
    category = FailureCategory.SCHEMA_DRIFT


class UnsupportedOperationError(CoreError):
    category = FailureCategory.UNSUPPORTED
    external = True


class OperationTimeoutError(CoreError):
    category = FailureCategory.TIMEOUT
    retryable = True
    external = True


class AmbiguousOutcomeError(CoreError):
    category = FailureCategory.AMBIGUOUS_OUTCOME
    retryable = True


class IdentityMissingError(CoreError):
    category = FailureCategory.IDENTITY_MISSING
    retryable = True


class ConflictingIdentityError(CoreError):
    category = FailureCategory.IDENTITY_CONFLICT


class AmbiguousIdentityError(CoreError):
    category = FailureCategory.GRAPH_AMBIGUOUS
    retryable = True


class GraphConvergenceTimeout(CoreError):
    category = FailureCategory.GRAPH_CONVERGENCE
    retryable = True


class CorruptStateError(CoreError):
    category = FailureCategory.CORRUPT_STATE


class ConcurrentStateError(CoreError):
    category = FailureCategory.INVARIANT


class OwnershipConflictError(CoreError):
    category = FailureCategory.OWNERSHIP
    retryable = True


class OwnerWaitTimeoutError(CoreError):
    category = FailureCategory.OWNERSHIP_TIMEOUT
    retryable = True


class CancellationUnprovenError(CoreError):
    category = FailureCategory.CANCELLATION_UNPROVEN
    retryable = True

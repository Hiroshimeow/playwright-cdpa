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
    RATE_LIMIT = "rate_limit"
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


class FailureCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    BROWSER_OFFLINE = "browser_offline"
    AUTH_REQUIRED = "auth_required"
    FRONTEND_DRIFT = "frontend_drift"
    SCHEMA_DRIFT = "schema_drift"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    EXTERNAL_FAILURE = "external_failure"
    EXTERNAL_TIMEOUT = "external_timeout"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"
    IDENTITY_MISSING = "identity_missing"
    IDENTITY_CONFLICT = "identity_conflict"
    GRAPH_AMBIGUOUS = "graph_ambiguous"
    GRAPH_CONVERGENCE = "graph_convergence"
    OWNERSHIP_CONFLICT = "ownership_conflict"
    OWNERSHIP_TIMEOUT = "ownership_timeout"
    CANCELLATION_UNPROVEN = "cancellation_unproven"
    CORRUPT_STATE = "corrupt_state"
    INVARIANT = "invariant"


class Disposition(str, Enum):
    INVALID_INPUT = "invalid_input"
    OWNERSHIP_TIMEOUT = "ownership_timeout"
    CANCELLATION_UNPROVEN = "cancellation_unproven"
    INVARIANT_FAILURE = "invariant_failure"
    EXTERNAL_FAILURE = "external_failure"
    GET_REQUIRED = "get_required"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


_DEFAULT_CODES = {
    FailureCategory.INVALID_INPUT: FailureCode.INVALID_INPUT,
    FailureCategory.BROWSER_OFFLINE: FailureCode.BROWSER_OFFLINE,
    FailureCategory.AUTH_REQUIRED: FailureCode.AUTH_REQUIRED,
    FailureCategory.FRONTEND_NOT_READY: FailureCode.FRONTEND_DRIFT,
    FailureCategory.NETWORK: FailureCode.NETWORK,
    FailureCategory.RATE_LIMIT: FailureCode.RATE_LIMITED,
    FailureCategory.BACKEND: FailureCode.EXTERNAL_FAILURE,
    FailureCategory.SCHEMA_DRIFT: FailureCode.SCHEMA_DRIFT,
    FailureCategory.UNSUPPORTED: FailureCode.UNSUPPORTED,
    FailureCategory.TIMEOUT: FailureCode.EXTERNAL_TIMEOUT,
    FailureCategory.AMBIGUOUS_OUTCOME: FailureCode.AMBIGUOUS_OUTCOME,
    FailureCategory.IDENTITY_MISSING: FailureCode.IDENTITY_MISSING,
    FailureCategory.IDENTITY_CONFLICT: FailureCode.IDENTITY_CONFLICT,
    FailureCategory.GRAPH_AMBIGUOUS: FailureCode.GRAPH_AMBIGUOUS,
    FailureCategory.GRAPH_CONVERGENCE: FailureCode.GRAPH_CONVERGENCE,
    FailureCategory.INVARIANT: FailureCode.INVARIANT,
    FailureCategory.CORRUPT_STATE: FailureCode.CORRUPT_STATE,
    FailureCategory.OWNERSHIP: FailureCode.OWNERSHIP_CONFLICT,
    FailureCategory.OWNERSHIP_TIMEOUT: FailureCode.OWNERSHIP_TIMEOUT,
    FailureCategory.CANCELLATION_UNPROVEN: FailureCode.CANCELLATION_UNPROVEN,
}

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
    code: FailureCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, FailureCategory):
            raise TypeError("failure category must be a FailureCategory")
        if type(self.message) is not str:
            raise ValueError("failure message must be exactly a string")
        if type(self.retryable) is not bool or type(self.external) is not bool:
            raise ValueError("failure flags must be exactly boolean")
        code = _DEFAULT_CODES[self.category] if self.code is None else self.code
        if not isinstance(code, FailureCode):
            raise TypeError("failure code must be a FailureCode")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", sanitize_diagnostic(self.message))

    @property
    def disposition(self) -> Disposition:
        if self.category == FailureCategory.INVALID_INPUT:
            return Disposition.INVALID_INPUT
        if self.category == FailureCategory.OWNERSHIP_TIMEOUT:
            return Disposition.OWNERSHIP_TIMEOUT
        if self.category == FailureCategory.CANCELLATION_UNPROVEN:
            return Disposition.CANCELLATION_UNPROVEN
        if self.category in _INVARIANT_FAILURE_CATEGORIES or not self.external:
            return Disposition.INVARIANT_FAILURE
        return Disposition.EXTERNAL_FAILURE

    def to_dict(self) -> dict[str, object]:
        assert self.code is not None
        return {
            "code": self.code.value,
            "category": self.category.value,
            "disposition": self.disposition.value,
            "message": sanitize_diagnostic(self.message),
            "retryable": self.retryable,
            "external": self.external,
        }


class CoreError(RuntimeError):
    category = FailureCategory.INVARIANT
    code = FailureCode.INVARIANT
    retryable = False
    external = False

    def as_failure(self) -> Failure:
        return Failure(
            self.category,
            sanitize_diagnostic(str(self)),
            self.retryable,
            self.external,
            self.code,
        )


class InvalidInputError(CoreError):
    category = FailureCategory.INVALID_INPUT
    code = FailureCode.INVALID_INPUT


class BrowserOfflineError(CoreError):
    category = FailureCategory.BROWSER_OFFLINE
    code = FailureCode.BROWSER_OFFLINE
    retryable = True
    external = True


class AuthenticationRequiredError(CoreError):
    category = FailureCategory.AUTH_REQUIRED
    code = FailureCode.AUTH_REQUIRED
    retryable = True
    external = True


class FrontendNotReadyError(CoreError):
    category = FailureCategory.FRONTEND_NOT_READY
    code = FailureCode.FRONTEND_DRIFT
    retryable = True
    external = True


class NetworkError(CoreError):
    category = FailureCategory.NETWORK
    code = FailureCode.NETWORK
    retryable = True
    external = True


class RateLimitedError(CoreError):
    category = FailureCategory.RATE_LIMIT
    code = FailureCode.RATE_LIMITED
    retryable = True
    external = True


class BackendError(CoreError):
    category = FailureCategory.BACKEND
    code = FailureCode.EXTERNAL_FAILURE
    external = True


class BackendUnavailableError(BackendError):
    retryable = True


class SchemaDriftError(CoreError):
    category = FailureCategory.SCHEMA_DRIFT
    code = FailureCode.SCHEMA_DRIFT


class UnsupportedOperationError(CoreError):
    category = FailureCategory.UNSUPPORTED
    code = FailureCode.UNSUPPORTED
    external = True


class OperationTimeoutError(CoreError):
    category = FailureCategory.TIMEOUT
    code = FailureCode.EXTERNAL_TIMEOUT
    retryable = True
    external = True


class AmbiguousOutcomeError(CoreError):
    category = FailureCategory.AMBIGUOUS_OUTCOME
    code = FailureCode.AMBIGUOUS_OUTCOME
    retryable = True


class IdentityMissingError(CoreError):
    category = FailureCategory.IDENTITY_MISSING
    code = FailureCode.IDENTITY_MISSING
    retryable = True


class ConflictingIdentityError(CoreError):
    category = FailureCategory.IDENTITY_CONFLICT
    code = FailureCode.IDENTITY_CONFLICT


class AmbiguousIdentityError(CoreError):
    category = FailureCategory.GRAPH_AMBIGUOUS
    code = FailureCode.GRAPH_AMBIGUOUS
    retryable = True


class GraphConvergenceTimeout(CoreError):
    category = FailureCategory.GRAPH_CONVERGENCE
    code = FailureCode.GRAPH_CONVERGENCE
    retryable = True


class CorruptStateError(CoreError):
    category = FailureCategory.CORRUPT_STATE
    code = FailureCode.CORRUPT_STATE


class ConcurrentStateError(CoreError):
    category = FailureCategory.INVARIANT
    code = FailureCode.INVARIANT


class OwnershipConflictError(CoreError):
    category = FailureCategory.OWNERSHIP
    code = FailureCode.OWNERSHIP_CONFLICT
    retryable = True


class OwnerWaitTimeoutError(CoreError):
    category = FailureCategory.OWNERSHIP_TIMEOUT
    code = FailureCode.OWNERSHIP_TIMEOUT
    retryable = True


class CancellationUnprovenError(CoreError):
    category = FailureCategory.CANCELLATION_UNPROVEN
    code = FailureCode.CANCELLATION_UNPROVEN
    retryable = True

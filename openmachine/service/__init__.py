"""Service lifecycle interface shared by CLI, application and MCP callers."""

from .state import PROTOCOL, STATE_CONTRACT, guard_writes, running_commit

__all__ = ["PROTOCOL", "STATE_CONTRACT", "guard_writes", "running_commit", "check_state",
           "probe_service", "prepare_payload", "socket_alive", "status", "supervise", "operate"]


def __getattr__(name: str):
    # Preserve the original callables without loading lifecycle code for metadata.
    if name in {"check_state", "probe_service"}:
        from . import checks
        value = getattr(checks, name)
    elif name == "prepare_payload":
        from .install import prepare_payload
        value = prepare_payload
    elif name in {"socket_alive", "status", "supervise"}:
        from . import runtime
        value = getattr(runtime, name)
    elif name == "operate":
        from .transition import operate
        value = operate
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

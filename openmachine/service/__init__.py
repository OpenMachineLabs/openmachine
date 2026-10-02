"""Service lifecycle interface shared by CLI, application and MCP callers."""

from .checks import check_state, probe_service
from .runtime import socket_alive, status, supervise
from .state import PROTOCOL, STATE_CONTRACT, guard_writes
from .transition import operate

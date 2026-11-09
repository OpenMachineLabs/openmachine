"""macOS Seatbelt confinement: Chat's per-operation process trees and Code's persistent lifetimes."""

import ctypes
import errno
import functools
import hashlib
import os
from pathlib import Path
import pwd
import re
import sys
from typing import NamedTuple

from openmachine.foundation.sanitize import Fault

BACKEND = "macos_seatbelt"
LIBRARY = "/usr/lib/libsandbox.1.dylib"
MACH_SERVICES = ("com.apple.system.opendirectoryd.libinfo", "com.apple.system.opendirectoryd.membership",
                 "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd",
                 "com.apple.diagnosticd", "com.apple.analyticsd", "com.apple.trustd.agent",
                 "com.apple.dnssd.service", "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent")
DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/dtracehelper", "/dev/ptmx")
# Default-denied reads under scope, relative to the account home: credentials, other apps' private
# data, cloud-synced and media libraries. A selected root re-allows reading inside it.
HOME_DENIED = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config/gh", ".config/gcloud", ".netrc",
               ".git-credentials", ".npmrc", ".pypirc", ".bash_history", ".zsh_history", ".zsh_sessions",
               ".sh_history", "Library", "Pictures", "Music", "Movies")
HOME_READABLE = ("Library/Developer",)  # Developer tools' data inside the denied Library.
SYSTEM_DENIED = ("/Volumes", "/Library/Keychains")  # External and network volumes; system keychains.
# Secret-named files outside selected roots: unreadable contents, anywhere. A subset of the typed
# tools' rule that programs can live with: public CA bundles (*.pem) and SDK "credentials" packages stay
# readable, so HTTPS and installed tools keep working. Inside a selected root, the project reads them.
SECRET_DIRECTORIES = (".ssh", ".aws", ".azure", ".gnupg", ".kube")
SECRET_FILES = r"\.env(\.[^/]*)?|\.netrc|\.pypirc|credentials(\.json)?|secrets\.toml|id_rsa|id_ed25519|[^/]*\.(key|p12|pfx)"
SECRET_DIRECTORY_REGEX = r"^(/.*)?/(" + "|".join(re.escape(name) for name in SECRET_DIRECTORIES) + r")(/.*)?$"
SECRET_FILE_REGEX = r"^(/.*)?/(" + SECRET_FILES + r")$"
KEPT_ENVIRONMENT = ("PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM")
# Git files that later run code outside any sandbox, relative to a .git directory: hooks,
# configuration and the commondir redirects of the repository, its worktrees and its submodules.
PROTECTED_GIT = r"(modules/.+/)?(hooks(/.*)?|config(\.worktree)?|commondir|worktrees/[^/]+/(config\.worktree|commondir))"
# Any .git entry (a repository, or a worktree or submodule link) and those files inside one, at any path.
PROTECTED_REGEX = r"^(/.*)?/\.git(/" + PROTECTED_GIT + ")?$"
PROTECTED_IDE_REGEX = r"^(/.*)?/\.(vscode|idea)(/.*)?$"


class SandboxError(Exception):
    """The profile could not be applied; the command has not started."""


def unavailable_reason() -> str | None:
    if sys.platform != "darwin":
        return "platform_unsupported"
    try:
        # System libraries live in the dyld shared cache, not as files on disk.
        ctypes.CDLL(LIBRARY).sandbox_init
    except (OSError, AttributeError):
        return "backend_missing"
    return None


def user_temp() -> Path:
    """Per-user Darwin temp (DARWIN_USER_TEMP_DIR), independent of $TMPDIR."""
    return Path(os.confstr(65537)).resolve(strict=True)


def private_directory(execution_id: str) -> Path:
    return user_temp() / "openmachine-scope" / execution_id


def account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def read_denials(home: Path) -> tuple[list[Path], list[Path]]:
    """Default-denied read areas and the developer exceptions inside them, as physical paths."""
    def physical(paths):
        return list(dict.fromkeys(Path(os.path.realpath(path)) for path in paths))
    return (physical([home / item for item in HOME_DENIED] + [Path(item) for item in SYSTEM_DENIED]),
            physical(home / item for item in HOME_READABLE))


def _literal(path: Path | str) -> str:
    text = str(path)
    if any(char in text for char in '"\\') or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise Fault("scope_unsupported", "A granted path contains characters the macOS backend cannot express.",
                    effect="known_no_effect")
    return '"' + text + '"'


def _regex(path: Path) -> str:
    _literal(path)
    return re.sub(r"([.^$|?*+()\[\]{}])", r"\\\1", str(path))


def _filters(kind: str, paths: list[Path]) -> str:
    return " ".join(f"({kind} {_literal(path)})" for path in paths)


def profile(*, readable: list[Path], writable: list[Path], private: Path, control: list[Path],
            denied: list[Path], exceptions: list[Path], checkout: tuple[Path, Path] | None = None) -> str:
    """Generate one profile: read the machine except denied areas, write only granted roots.

    For the same operation Seatbelt applies the last matching rule, so each later block narrows or
    re-allows the one before it and the protected and control denials come last. A later broader
    operation (file-read*) does not override an earlier narrower denial (file-read-data).

    Protected names are denied wherever the profile allows writes, private trees included: renaming or
    cloning a directory moves everything inside it without checking those paths. checkout is the
    destination and common Git dir of Core's own git worktree add, which alone creates that worktree's
    .git file and the commondir of its new registration.
    """
    xcrun = user_temp() / "xcrun_db"
    lines = [
        "(version 1)", "(deny default)",
        "(allow process-fork)", "(allow process-exec)",
        "(allow signal (target same-sandbox))", "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)", "(allow pseudo-tty)", "(allow ipc-posix-sem)",
        '(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))',
        "(allow mach-lookup (global-name " + " ".join(f'"{name}"' for name in MACH_SERVICES) + "))",
        "(allow network-outbound (remote ip))", "(allow network-inbound (local ip))",
        "(allow network-bind (local ip))", "(allow system-socket)",
        '(allow network-outbound (literal "/private/var/run/mDNSResponder"))',
        "(allow file-read*)",
        # Devices: only these, the command's own PTYs and metadata; never another terminal.
        '(deny file-read* file-write* (subpath "/dev"))',
        '(allow file-read* (literal "/dev"))', '(allow file-read-metadata (subpath "/dev"))',
        f"(allow file-read* file-write-data {_filters('literal', [Path(item) for item in DEVICES])})",
        '(allow file-read* file-write* (subpath "/dev/fd"))',
        '(allow file-read* file-write* (require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))',
        '(allow file-ioctl (regex #"^/dev/ttys[0-9]+$"))',
        # Default-denied areas, plus other scoped executions' private homes.
        f"(deny file-read* process-exec {_filters('subpath', [*denied, private.parent])})",
        f"(allow file-read-metadata {_filters('literal', denied)})",
    ]
    if exceptions:
        lines.append(f"(allow file-read* process-exec {_filters('subpath', exceptions)})")
    # A later file-read* does not override a file-read-data denial, so the selected roots below
    # re-allow file-read-data by name. Only regular files match by name: SDK directories called
    # "credentials" stay listable.
    lines.append(f'(deny file-read-data (regex #"{SECRET_DIRECTORY_REGEX}"))')
    lines.append(f'(deny file-read-data (require-all (regex #"{SECRET_FILE_REGEX}") (vnode-type REGULAR-FILE)))')
    if readable:
        lines.append(f"(allow file-read* file-read-data process-exec {_filters('subpath', readable)})")
    lines.append(f"(allow file-read* file-read-data file-write* process-exec {_filters('subpath', writable + [private])})")
    lines.append(f"(allow file-read* file-write* (prefix {_literal(str(xcrun))}))")
    lines.append(f'(deny file-write* (regex #"{PROTECTED_REGEX}") (regex #"{PROTECTED_IDE_REGEX}"))')
    if checkout is not None:
        # Create only: existing registrations' commondirs stay unchanged. A failed add removes its link.
        destination, common = checkout
        lines.append(f"(allow file-write-create file-write-unlink (literal {_literal(destination / '.git')}))")
        lines.append(f'(allow file-write-create (regex #"^{_regex(common)}/worktrees/[^/]+/commondir$"))')
    lines.append(f"(deny file-read* file-write* process-exec {_filters('subpath', control)})")
    return "\n".join(lines) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def own_terminal(text: str, terminal: str) -> str:
    """Let a PTY child reopen only its own slave, never another terminal."""
    return text + f"(allow file-read* file-write* (literal {_literal(terminal)}))\n"


def environment(base: dict, private: Path) -> dict:
    """Construct the command environment; nothing else from the service is inherited."""
    kept = {key: value for key, value in base.items() if key in KEPT_ENVIRONMENT or key.startswith("LC_")}
    return kept | {"HOME": str(private / "home"), "TMPDIR": str(private / "tmp") + "/",
                   "XDG_CACHE_HOME": str(private / "cache"), "XDG_CONFIG_HOME": str(private / "config"),
                   "XDG_DATA_HOME": str(private / "data"), "XDG_STATE_HOME": str(private / "state"),
                   "npm_config_cache": str(private / "cache" / "npm")}


def prepare(private: Path, identity: dict[str, str]) -> None:
    """Create the private home/temp tree and a Git config holding only the user's identity."""
    private.parent.mkdir(mode=0o700, exist_ok=True)
    private.mkdir(mode=0o700)
    for name in ("home", "tmp", "cache", "config", "data", "state"):
        (private / name).mkdir(mode=0o700)
    lines = [f"\t{key} = {value}" for key, value in identity.items()
             if value and not any(ord(char) < 32 for char in value)]
    if lines:
        (private / "home" / ".gitconfig").write_text("[user]\n" + "\n".join(lines) + "\n")


def apply(text: str) -> None:
    """Confine the calling process and every descendant; call only in the child before exec."""
    library = ctypes.CDLL(LIBRARY)
    library.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
    library.sandbox_init.restype = ctypes.c_int
    error = ctypes.c_char_p()
    if library.sandbox_init(text.encode(), 0, ctypes.byref(error)) != 0:
        raise SandboxError((error.value or b"sandbox_init failed").decode(errors="replace")[:200])


# Code lifetimes. One restricted lifetime instance is one sandbox_init instance: fork, setsid, exec and
# reparenting cannot leave it, and nested sandboxes, setuid exec and launchd delegation are refused.
# Each instance's profile ends with two marker rules, so the unsandboxed supervisor can classify any
# process from outside: a member is sandboxed, allows marker "a" and denies marker "b".
MARKER = "com.openmachine.life."
_PROC_PIDTBSDINFO = 3
_ZOMBIE = 5


class _BSDInfo(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32),
                ("svuid", ctypes.c_uint32), ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32), ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32), ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32),
                ("tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
                ("start_usec", ctypes.c_uint64)]


class Birth(NamedTuple):
    """A process identity that a reused PID cannot match: PID plus kernel start time."""
    pid: int
    start: int


@functools.cache
def _native():
    sandbox = ctypes.CDLL(LIBRARY)
    check = sandbox.sandbox_check
    # Fixed arguments only: ctypes passes extra arguments as C variadic arguments.
    check.argtypes, check.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int], ctypes.c_int
    no_report = ctypes.c_int.in_dll(sandbox, "SANDBOX_CHECK_NO_REPORT").value
    info = ctypes.CDLL("/usr/lib/libSystem.B.dylib").proc_pidinfo
    info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    info.restype = ctypes.c_int
    return check, no_report, info


def lifetime_unavailable_reason() -> str | None:
    reason = unavailable_reason()
    if reason is None:
        try:
            _native()
        except (OSError, AttributeError, ValueError):
            return "backend_missing"
    return reason


def has_acl(path: Path) -> bool:
    """Whether path carries an extended ACL; a link is inspected itself, never followed."""
    if sys.platform != "darwin":
        return False
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    system.acl_get_link_np.argtypes, system.acl_get_link_np.restype = [ctypes.c_char_p, ctypes.c_int], ctypes.c_void_p
    acl = system.acl_get_link_np(os.fsencode(path), 0x100)  # ACL_TYPE_EXTENDED
    if not acl:
        if ctypes.get_errno() != errno.ENOENT:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()), str(path))
        return False
    system.acl_free(ctypes.c_void_p(acl))
    return True


def lifetime_rules(marker: str, runtime: Path) -> str:
    """Append to a Code profile: Unix sockets and new PTYs only in the lifetime's runtime tree (tmux), then markers."""
    if re.fullmatch(r"[0-9a-f]{32}", marker) is None:
        raise ValueError("invalid lifetime marker")
    tree = f"(subpath {_literal(runtime)})"
    return "\n".join([
        # realpath (tmux resolves TMUX_TMPDIR) must stat the denied parent itself, never list it.
        f"(allow file-read-metadata (literal {_literal(runtime.parent)}))",
        f"(allow network-bind network-inbound (local unix-socket {tree}))",
        f"(allow network-outbound (remote unix-socket {tree}))",
        '(allow file-ioctl (literal "/dev/ptmx"))',
        f'(allow mach-lookup (global-name "{MARKER}{marker}.a"))',
        f'(deny mach-lookup (global-name "{MARKER}{marker}.b"))',
    ]) + "\n"


def birth(pid: int) -> Birth | None:
    """The live identity of pid, or None once it is gone or a zombie."""
    _, _, info = _native()
    value = _BSDInfo()
    if (info(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(value), ctypes.sizeof(value)) != ctypes.sizeof(value)
            or value.status == _ZOMBIE):
        return None
    return Birth(pid, value.start_sec * 1_000_000 + value.start_usec)


def member(pid: int, marker: str) -> bool | None:
    """True for a process of this lifetime, False for any other, None when it vanished mid-check."""
    check, no_report, _ = _native()
    sandboxed = check(pid, None, 0)
    if sandboxed <= 0:
        return None if sandboxed < 0 else False
    name = f"{MARKER}{marker}.".encode()
    allowed = check(pid, b"mach-lookup", 2 | no_report, name + b"a")
    denied = check(pid, b"mach-lookup", 2 | no_report, name + b"b")
    if allowed < 0 or denied < 0:
        return None
    return allowed == 0 and denied == 1


def signal_member(target: Birth, marker: str, signum: int) -> bool:
    """Signal only the same birth identity, and only while it is still a member."""
    current = birth(target.pid)
    if current is None or current.start != target.start or member(target.pid, marker) is not True:
        return False
    current = birth(target.pid)
    if current is None or current.start != target.start:
        return False
    try:
        os.kill(target.pid, signum)
    except ProcessLookupError:
        return False
    return True


def signal_group(leader: Birth, marker: str, signum: int) -> bool:
    """Signal a session command's process group, as sshd does, while its leader is verifiably unchanged.

    The group id is the leader's PID and every group member descends from it, so it stays in the lifetime.
    """
    if not signal_member(leader, marker, 0):
        return False
    try:
        os.killpg(leader.pid, signum)
    except ProcessLookupError:
        return False
    return True

"""macOS Seatbelt confinement for one scoped operation's launched process tree."""

import ctypes
import hashlib
import os
from pathlib import Path
import pwd
import re
import sys

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
KEPT_ENVIRONMENT = ("PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM")
# Git files that later run code outside any sandbox, relative to a .git directory: hooks,
# configuration and the commondir redirect of the repository, its worktrees and its submodules.
PROTECTED_GIT = r"(modules/.+/)?(hooks(/.*)?|config(\.worktree)?|commondir|worktrees/[^/]+/config\.worktree)"
PROTECTED_REGEX = r"(/.*)?/\.git/" + PROTECTED_GIT + "$"
PROTECTED_IDE_REGEX = r"(/.*)?/\.(vscode|idea)(/.*)?$"


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
            denied: list[Path], exceptions: list[Path]) -> str:
    """Generate one profile: read the machine except denied areas, write only granted roots.

    Seatbelt applies the last matching rule, so each later block narrows or re-allows the one
    before it and the protected and control denials come last.
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
    if readable:
        lines.append(f"(allow file-read* process-exec {_filters('subpath', readable)})")
    lines.append(f"(allow file-read* file-write* process-exec {_filters('subpath', writable + [private])})")
    lines.append(f"(allow file-read* file-write* (prefix {_literal(str(xcrun))}))")
    for root in writable:
        lines.append(f'(deny file-write* (regex #"^{_regex(root)}{PROTECTED_REGEX}"))')
        lines.append(f'(deny file-write* (regex #"^{_regex(root)}{PROTECTED_IDE_REGEX}"))')
        git = root / ".git"
        if os.path.lexists(git):
            # Renaming, replacing or repointing .git would carry hooks and config out of the regex.
            lines.append(f"(deny file-write* (literal {_literal(git)}))")
        if git.is_dir() and not git.is_symlink():
            # Existing worktrees' commondir redirects; new registrations still create theirs.
            lines.extend(f"(deny file-write* (literal {_literal(path)}))"
                         for path in sorted(git.glob("worktrees/*/commondir")))
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

"""Workspace folders: the one folder rule, the control paths and the writable grants of a Workspace session."""

from dataclasses import dataclass
import fcntl
import os
import pwd
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

from openmachine.access import git
from openmachine.access.macos import seatbelt
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE
from openmachine.foundation.sanitize import Fault

# An absolute path without NUL or control characters; the folder rule decides everything else.
Folder = Annotated[str, Field(max_length=1024, pattern=r"^/[^\x00-\x1f\x7f]*$")]
Folders = Annotated[list[Folder], Field(min_length=1, max_length=32)]


class FolderFault(Fault):
    """A folder the rule refuses, by its index in the request."""

    def __init__(self, code: str, index: int, folder: str):
        super().__init__(code, f"The Workspace folder {folder} cannot be used ({code}); change it in the "
                         "Open Machine app on this Mac.", effect="known_no_effect")
        self.index = index


@dataclass(frozen=True)
class Scope:
    """Workspace folders; the control paths stay denied inside them."""
    folders: tuple[Path, ...]
    control: tuple[Path, ...]

    def _controlled(self, path: Path) -> bool:
        return any(path == item or path.is_relative_to(item) for item in self.control)

    def _covered(self, path: Path) -> bool:
        return any(path == folder or path.is_relative_to(folder) for folder in self.folders)

    @staticmethod
    def _reciprocal(path: Path, common: str) -> bool:
        """Registration metadata is writable by Workspace work; only the worktree's own pointer proves it."""
        observed = git.context(path)
        return (observed["git_status"] == "observed" and observed["worktree"] == str(path)
                and observed["common_dir"] == common and observed["git_dir"] != common)

    def grants(self) -> list[Path]:
        """Each folder, and each registered worktree of a repository that is a folder."""
        found = list(self.folders)
        for folder in self.folders:
            observed = git.context(folder)
            if (observed["git_status"] != "observed" or Path(observed["worktree"]) != folder
                    or observed["git_dir"] != observed["common_dir"]):
                continue
            for item in git.registered_worktrees(folder):
                path = Path(item["worktree"])
                if (item["prunable"] or item["bare"] or not path.is_dir() or self._controlled(path)
                        or self._covered(path) or not self._reciprocal(path, observed["common_dir"])):
                    continue
                found.append(path)
        return found


def control_paths(state: Path) -> tuple[Path, ...]:
    """Core, Connector and Code host configuration and state; denied to restricted work even inside a folder."""
    candidates = [state, DEFAULT_CONFIG.parent, DEFAULT_STATE, DEFAULT_STATE.parent / "openmachine-connector",
                  DEFAULT_STATE.parent / "openmachine-code"]
    found = []
    for path in candidates:
        resolved = Path(os.path.realpath(path))
        if resolved not in found:
            found.append(resolved)
    return tuple(found)


def account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _inside(path: Path, other: Path) -> bool:
    if sys.platform == "darwin":  # macOS volumes match names in any case; refuse every spelling.
        path, other = Path(str(path).casefold()), Path(str(other).casefold())
    return path == other or path.is_relative_to(other)


def folders(paths: list[str], control: tuple[Path, ...], *, admission: bool) -> list[Path]:
    """Each folder's kernel path, in order, or a FolderFault for the first folder the rule refuses.

    The folders command reads one entry of each folder, so that a privacy denial shows while the owner
    is at the Mac. Admission reads none, so that no privacy prompt holds a launch, and refuses a folder
    whose kernel path is no longer the stored one.
    """
    home = account_home()
    # Everything the profile denies before a folder's allow, which would re-open it: the denied areas,
    # devices and Core's runtime trees.
    denied = [*seatbelt.read_denials(home)[0], Path("/dev"), seatbelt.user_temp() / "omc"]
    found = []
    for index, text in enumerate(paths):
        try:
            fd = os.open(text, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # The open descriptor's path: links, /tmp and /System/Volumes/Data spellings resolved, and
                # each component's on-disk name.
                path = Path(os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0")))
                if not admission:
                    with os.scandir(fd) as entries:
                        next(entries, None)
            finally:
                os.close(fd)
        except FileNotFoundError:
            raise FolderFault("folder_missing", index, text) from None
        except NotADirectoryError:
            raise FolderFault("folder_not_directory", index, text) from None
        except OSError:
            raise FolderFault("folder_access_denied", index, text) from None
        code = ("folder_changed" if admission and str(path) != text
                else "folder_unsupported" if any(char in '"\\' or ord(char) < 32 or ord(char) == 127 for char in str(path))
                else "folder_too_broad" if _inside(home, path)
                else "folder_protected" if any(_inside(path, item) or _inside(item, path) for item in denied)
                else "folder_conflict" if any(_inside(path, item) or _inside(item, path) for item in control)
                else None)
        if code:
            raise FolderFault(code, index, text)
        found.append(path)
    for index, path in enumerate(found):
        # A duplicate refuses the later one; a folder inside another refuses the inner one.
        if any(_inside(path, other) and (earlier < index or not _inside(other, path))
               for earlier, other in enumerate(found) if earlier != index):
            raise FolderFault("folder_overlap", index, paths[index])
    return found

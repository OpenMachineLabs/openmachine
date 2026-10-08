"""Workspace operations and inspection limits for controller and target callers."""

from .files import MAX_LINES, MAX_SOURCE, read_text_file
from .change_records import (READ_CHANGE_FORMATS, MAX_PREPARED, MAX_PARENTS, MAX_PARENT_DEPTH,
                             CHANGE_FORMAT, MAX_CHANGES, MAX_EDITS, MAX_RECORD, MAX_WORKSPACE_RESULT, check_change_state)
from .changes import apply_patch, bound_result, edit_text_file, read_change, write_text_file
from .git import MAX_CAPTURE
from .inspect import MAX_MATCHES, MAX_SCAN_BYTES, inspect
from .patch import LANGUAGE as PATCH_LANGUAGE, MAX_HUNKS, MAX_PATHS
from .worktrees import select as select_worktree

"""Workspace operations and inspection limits for controller and target callers."""

from .files import MAX_LINES, MAX_SOURCE, read_text_file
from .changes import (READ_CHANGE_FORMATS, MAX_PREPARED, MAX_PARENTS, MAX_PARENT_DEPTH, apply_patch, CHANGE_FORMAT, MAX_CHANGES, MAX_EDITS, MAX_RECORD, MAX_WORKSPACE_RESULT,
                      bound_result, check_change_state, edit_text_file, read_change, write_text_file)
from .git import MAX_CAPTURE
from .inspect import MAX_MATCHES, MAX_SCAN_BYTES, inspect
from .patch import LANGUAGE as PATCH_LANGUAGE, MAX_HUNKS, MAX_PATHS

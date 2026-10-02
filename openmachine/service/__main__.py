"""Retain the candidate-owned service check-state process entry."""

import json
from pathlib import Path
import sys

from openmachine.foundation.sanitize import Fault
from .checks import check_state


if __name__ == "__main__":
    # Candidate-owned cross-version validation, not a second public CLI or executor.
    try:
        if len(sys.argv) != 4 or sys.argv[1] != "check-state":
            raise Fault("invalid_service_request", "Use the openmachine service CLI.")
        print(json.dumps(check_state(Path(sys.argv[2]), Path(sys.argv[3]))))
    except (Fault, OSError) as error:
        print(json.dumps({"error": error.code if isinstance(error, Fault) else "state_unavailable"}))
        sys.exit(1)

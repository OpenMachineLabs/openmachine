import json


def export_json(rows: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(rows, f, indent=2)

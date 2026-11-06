import argparse

from report import export_json


def load_rows() -> list[dict]:
    return [{"name": "alpha", "count": 3}, {"name": "beta", "count": 5}]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Export the usage report.")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    export_json(load_rows(), args.out)


if __name__ == "__main__":
    main()

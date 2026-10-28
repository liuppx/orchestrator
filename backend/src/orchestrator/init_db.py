from __future__ import annotations

import argparse

from orchestrator.db import init_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the Orchestrator control-plane schema")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    init_database(args.database_url)
    print("control-plane schema initialized")


if __name__ == "__main__":
    main()

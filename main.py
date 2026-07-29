from __future__ import annotations

import argparse

from flow import run_flow


def main() -> None:
    parser = argparse.ArgumentParser(description="Thin PLAN -> REVIEW -> DEV flow")
    parser.add_argument("--task", required=True)
    parser.add_argument("--url-id")
    parser.add_argument("--project")
    args = parser.parse_args()

    run_flow(task=args.task, url_id=args.url_id, project_name=args.project)


if __name__ == "__main__":
    main()

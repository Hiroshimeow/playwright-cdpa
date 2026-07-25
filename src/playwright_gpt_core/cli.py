from __future__ import annotations

import argparse


def _shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--state-dir", default=".playwright-gpt")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep-helper-tab", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playwright-gpt")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send", help="send through the real ChatGPT frontend")
    send.add_argument("prompt")
    target = send.add_mutually_exclusive_group()
    target.add_argument("--fresh", action="store_true")
    target.add_argument("--conversation")
    send.add_argument("--wait-idle", action="store_true")
    _shared(send)
    watch = sub.add_parser("watch", help="watch an exact persisted turn")
    watch.add_argument("request_id")
    _shared(watch)
    cancel = sub.add_parser("cancel", help="request exact-turn cancellation")
    cancel.add_argument("request_id")
    _shared(cancel)
    get = sub.add_parser("get", help="read persisted turn metadata")
    get.add_argument("request_id")
    _shared(get)
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    raise SystemExit("service layer not initialized")

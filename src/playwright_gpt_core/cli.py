from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import CoreConfig
from .errors import CoreError, Failure, FailureCategory, InvalidInputError
from .models import Result, TurnState
from .redaction import safe_json_dumps
from .service import ChatGPTCore

EXIT_SUCCESS = 0
EXIT_INVALID = 2
EXIT_RECOVERABLE_EXTERNAL = 10
EXIT_TERMINAL_EXTERNAL = 11
EXIT_AMBIGUOUS = 12
EXIT_INVARIANT = 20
EXIT_OWNERSHIP = 21
EXIT_CANCELLED = 22
EXIT_CANCELLATION_UNPROVEN = 23


def _shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--send-timeout", type=float, default=90.0)
    parser.add_argument("--identity-timeout", type=float, default=30.0)
    parser.add_argument("--state-dir", default=".playwright-gpt")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep-helper-tab", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playwright-gpt")
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="send through the real ChatGPT frontend")
    send.add_argument("prompt")
    target = send.add_mutually_exclusive_group(required=True)
    target.add_argument("--fresh", action="store_true")
    target.add_argument("--conversation")
    send.add_argument("--wait-idle", action="store_true")
    _shared(send)

    watch = sub.add_parser("watch", help="watch an exact persisted request")
    watch.add_argument("request_id")
    _shared(watch)

    cancel = sub.add_parser("cancel", help="request exact-turn cancellation")
    cancel.add_argument("request_id")
    _shared(cancel)

    get = sub.add_parser("get", help="read persisted request metadata")
    get.add_argument("request_id")
    _shared(get)
    return parser


def _config(args: argparse.Namespace) -> CoreConfig:
    return CoreConfig(
        cdp_endpoint=args.cdp_endpoint,
        state_dir=Path(args.state_dir),
        timeout=args.timeout,
        poll=args.poll,
        send_timeout=args.send_timeout,
        identity_timeout=args.identity_timeout,
        keep_helper_tab=args.keep_helper_tab,
    ).validated()


async def _run(args: argparse.Namespace) -> Result:
    core = ChatGPTCore(_config(args))
    if args.command == "send":
        return await core.send(
            args.prompt,
            fresh=args.fresh,
            conversation=args.conversation,
            wait_idle=args.wait_idle,
        )
    if args.command == "watch":
        return await core.watch(args.request_id)
    if args.command == "cancel":
        return await core.cancel(args.request_id)
    if args.command == "get":
        return core.get(args.request_id)
    raise InvalidInputError(f"unsupported command {args.command}")


def exit_code(result: Result, *, command: str) -> int:
    if command == "get":
        return EXIT_SUCCESS
    if result.state == TurnState.CANCELLED:
        return EXIT_CANCELLED
    if command == "cancel" and result.state == TurnState.COMPLETE:
        return EXIT_SUCCESS
    if result.success:
        return EXIT_SUCCESS
    failure = result.failure
    if failure is None:
        return EXIT_AMBIGUOUS
    if failure.category == FailureCategory.INVALID_INPUT:
        return EXIT_INVALID
    if failure.category == FailureCategory.OWNERSHIP:
        return EXIT_OWNERSHIP
    if failure.category == FailureCategory.CANCELLATION_UNPROVEN:
        return EXIT_CANCELLATION_UNPROVEN
    if failure.category in {
        FailureCategory.TIMEOUT,
        FailureCategory.AMBIGUOUS_OUTCOME,
        FailureCategory.IDENTITY_MISSING,
        FailureCategory.GRAPH_AMBIGUOUS,
        FailureCategory.GRAPH_CONVERGENCE,
    }:
        return EXIT_AMBIGUOUS
    if failure.external:
        return EXIT_RECOVERABLE_EXTERNAL if failure.retryable else EXIT_TERMINAL_EXTERNAL
    return EXIT_INVARIANT


def _render(result: Result, *, json_mode: bool) -> None:
    if json_mode:
        print(safe_json_dumps(result.to_dict()))
        return
    if result.response is not None:
        print(result.response)
    else:
        print(safe_json_dumps(result.to_dict(), pretty=True))


def _error_result(exc: BaseException) -> Result:
    if isinstance(exc, CoreError):
        failure = exc.as_failure()
    elif isinstance(exc, FileNotFoundError):
        failure = Failure(FailureCategory.INVALID_INPUT, "request state was not found")
    else:
        failure = Failure(
            FailureCategory.INVARIANT,
            f"local invariant failure: {type(exc).__name__}: {exc}",
        )
    return Result(1, "-", TurnState.FAILED, failure=failure)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        result = _error_result(exc)
    _render(result, json_mode=args.json)
    return exit_code(result, command=args.command)


if __name__ == "__main__":
    raise SystemExit(main())

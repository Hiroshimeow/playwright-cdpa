from __future__ import annotations

from playwright_gpt_core.cli import build_parser


def test_cli_acceptance_surface() -> None:
    parser = build_parser()
    args = parser.parse_args(["send", "Reply OK", "--fresh", "--json"])
    assert args.command == "send"
    assert args.fresh is True
    args = parser.parse_args(["watch", "req-1", "--json"])
    assert args.command == "watch"
    args = parser.parse_args(["send", "next", "--conversation", "conv-1", "--wait-idle"])
    assert args.wait_idle is True

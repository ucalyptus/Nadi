"""CLI for the local Nadi MVP."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import local_stack
from .server import serve


def demo(db: str) -> dict:
    if db != ":memory":
        Path(db).unlink(missing_ok=True)
    stack = local_stack(db)
    gateway = stack["gateway"]
    created = gateway.create_session("demo", {"source": "cli"})
    sid = created["session_id"]
    echo = gateway.send_command(sid, "echo", {"text": "hello river"})
    tool = gateway.send_command(sid, "tool", {"name": "uppercase", "args": {"text": "nadi"}})
    model = gateway.send_command(sid, "model", {"prompt": "status"})
    events = gateway.get_session_events(sid)
    return {"session": created, "commands": [echo, tool, model], "events": events, "broker_tool_path_calls": stack["broker"].tool_path_calls}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nadi.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run full local e2e lifecycle")
    d.add_argument("--db", default="/tmp/nadi.db")
    s = sub.add_parser("serve", help="serve HTTP JSON API")
    s.add_argument("--db", default="/tmp/nadi.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if args.cmd == "demo":
        print(json.dumps(demo(args.db), indent=2, sort_keys=True))
        return 0
    if args.cmd == "serve":
        serve(args.db, args.host, args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

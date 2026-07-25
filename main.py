#!/usr/bin/env python3
import argparse
import json
import sys

from agent import feedback, loop


def main() -> None:
    parser = argparse.ArgumentParser(description="Gradient — self-evolving agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("once", help="run one ingest -> plan -> publish -> retrain-check pass")

    loop_parser = sub.add_parser("loop", help="run continuously")
    loop_parser.add_argument("--interval", type=int, default=60, help="seconds between passes")

    fb_parser = sub.add_parser("feedback", help="record accept/reject/share/invite on a plan")
    fb_parser.add_argument("plan_id")
    fb_parser.add_argument("decision", choices=["accept", "reject", "share", "invite"])

    args = parser.parse_args()

    if args.command == "once":
        result = loop.run_once()
        print(json.dumps(result, indent=2))
    elif args.command == "loop":
        loop.run_loop(interval_seconds=args.interval)
    elif args.command == "feedback":
        try:
            plan = feedback.record(args.plan_id, args.decision)
        except (KeyError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"{args.plan_id} -> {plan['status']}")


if __name__ == "__main__":
    main()

"""CLI entry point for agentic-ci."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="agentic-ci",
        description="Tooling for running AI coding agents in CI/CD environments",
    )
    sub = parser.add_subparsers(dest="command")

    # setup
    p_setup = sub.add_parser("setup", help="Bootstrap CI container")
    p_setup.add_argument("--workspace", default=None)
    p_setup.add_argument("--user", default="claude-ci")

    # run
    p_run = sub.add_parser("run", help="Run Claude Code with telemetry")
    p_run.add_argument("prompt")
    p_run.add_argument("workdir", nargs="?", default=".")
    p_run.add_argument("--model", default=None)

    # stream
    p_stream = sub.add_parser("stream", help="Parse stream-json from stdin")
    p_stream.add_argument("--wrap", type=int, default=0, metavar="COLS")
    p_stream.add_argument("--no-color", action="store_true")
    p_stream.add_argument("--claude-pid", type=int, default=0)

    # otel-collect
    sub.add_parser("otel-collect", help="Start OTLP metrics collector")

    # otel-summary
    p_summary = sub.add_parser("otel-summary", help="Print token/cost summary")
    p_summary.add_argument("log_file", nargs="?", default="/tmp/claude-otel.jsonl")

    # extract
    p_extract = sub.add_parser("extract", help="Extract JSON result from stream output")
    p_extract.add_argument("stream_file")
    p_extract.add_argument("output_dir")

    args, extra = parser.parse_known_args()

    if args.command == "setup":
        from agentic_ci.setup import setup

        setup(workspace=args.workspace, user=args.user)

    elif args.command == "run":
        from agentic_ci.runner import run

        sys.exit(run(args.prompt, args.workdir, model=args.model, extra_args=extra))

    elif args.command == "stream":
        from agentic_ci.stream import StreamProcessor

        processor = StreamProcessor(
            color=not args.no_color,
            wrap=args.wrap,
            claude_pid=args.claude_pid,
        )
        processor.process(sys.stdin)
        print()

    elif args.command == "otel-collect":
        from agentic_ci.otel_collector import main as collector_main

        collector_main()

    elif args.command == "otel-summary":
        from agentic_ci.otel_summary import print_summary

        print_summary(args.log_file)

    elif args.command == "extract":
        import json

        from agentic_ci.extract import extract_text_from_stream, parse_result, write_outputs

        text = extract_text_from_stream(args.stream_file)
        if not text:
            print("Error: no text content found", file=sys.stderr)
            sys.exit(1)
        try:
            result = parse_result(text)
        except json.JSONDecodeError as e:
            print(f"Error: could not parse JSON: {e}", file=sys.stderr)
            sys.exit(1)
        write_outputs(result, args.output_dir)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Colored CLI output helpers for agentic-ci."""

import os
import sys


def _use_color() -> bool:
    return not os.environ.get("NO_COLOR") and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def section(msg: str) -> None:
    """Print a section header: ▶ msg"""
    if _use_color():
        print(f"\033[1;36m▶ {msg}\033[0m", flush=True)
    else:
        print(f"▶ {msg}", flush=True)


def detail(label: str, value: str) -> None:
    """Print an indented detail line: '  label: value'."""
    print(f"  {label}: {value}", flush=True)


def info(msg: str) -> None:
    """Print an indented info line."""
    print(f"  {msg}", flush=True)

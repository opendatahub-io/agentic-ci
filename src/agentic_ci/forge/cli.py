"""CLI subcommands for forge operations.

Registered as the ``agentic-ci forge`` subcommand group.

Usage::

    agentic-ci forge mr-status <URL>
    agentic-ci forge mr-comments <URL>
    agentic-ci forge mr-general-comments <URL> [--since ISO]
    agentic-ci forge mr-reply <URL> <thread_id> <message>
    agentic-ci forge mr-resolve <URL> <thread_id>
    agentic-ci forge pipeline-failures <URL>
    agentic-ci forge mr-diff-position <URL>
    agentic-ci forge github-token --app-id ID --installation-id ID --private-key PEM
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from agentic_ci.forge import Forge


def cmd_mr_status(args: argparse.Namespace) -> None:
    """Get MR/PR state, source branch, and pipeline status."""
    forge = Forge.detect(args.url, github_token=args.token)
    result = forge.mr_status(args.url)
    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_mr_comments(args: argparse.Namespace) -> None:
    """Get unresolved review comment threads."""
    forge = Forge.detect(args.url, github_token=args.token)
    result = forge.review_comments(args.url)
    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_mr_general_comments(args: argparse.Namespace) -> None:
    """Get general (non-diff-positioned) MR/PR comments."""
    forge = Forge.detect(args.url, github_token=args.token)
    result = forge.general_comments(args.url, since=args.since)
    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_mr_reply(args: argparse.Namespace) -> None:
    """Reply to a review thread."""
    forge = Forge.detect(args.url, github_token=args.token)
    forge.reply(args.url, args.thread_id, args.message)
    print(f"Replied to thread {args.thread_id}")


def cmd_mr_resolve(args: argparse.Namespace) -> None:
    """Resolve a review thread."""
    forge = Forge.detect(args.url, github_token=args.token)
    forge.resolve(args.url, args.thread_id)
    print(f"Resolved thread {args.thread_id}")


def cmd_pipeline_failures(args: argparse.Namespace) -> None:
    """Get failed CI job names and logs."""
    forge = Forge.detect(args.url, github_token=args.token)
    result = forge.pipeline_failures(args.url)
    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_mr_update(args: argparse.Namespace) -> None:
    """Update MR/PR title and/or description."""
    forge = Forge.detect(args.url, github_token=args.token)
    forge.update_description(args.url, title=args.title, description=args.description)
    print(f"Updated {args.url}")


def cmd_mr_diff_position(args: argparse.Namespace) -> None:
    """Get the first changed line position and diff refs (GitLab only)."""
    from agentic_ci.forge.gitlab import GitLabForge

    forge = Forge.detect(args.url, github_token=args.token)
    if not isinstance(forge, GitLabForge):
        print("Error: mr-diff-position is only supported for GitLab", file=sys.stderr)
        sys.exit(1)
    result = forge.mr_diff_position(args.url)
    json.dump(result, sys.stdout, indent=2)
    print()


def cmd_github_token(args: argparse.Namespace) -> None:
    """Generate a short-lived GitHub App installation token."""
    from agentic_ci.forge.github import generate_github_jwt, get_installation_token

    private_key = args.private_key
    if os.path.isfile(private_key):
        try:
            with open(private_key, encoding="utf-8") as f:
                private_key = f.read()
        except OSError as exc:
            print(f"Error: failed to read private key file: {exc}", file=sys.stderr)
            sys.exit(1)
    jwt_token = generate_github_jwt(args.app_id, private_key)
    token = get_installation_token(jwt_token, args.installation_id)
    print(token)


def register_subcommands(forge_parser: argparse.ArgumentParser) -> None:
    """Register forge subcommands on the given parser.

    Called from the main ``agentic-ci`` CLI to wire up the ``forge``
    subcommand group.
    """
    forge_parser.add_argument(
        "--token",
        help="GitHub token for authentication (required for GitHub operations)",
    )
    subparsers = forge_parser.add_subparsers(dest="forge_command", required=True)

    p_status = subparsers.add_parser(
        "mr-status", help="Get MR/PR state, source branch, pipeline status"
    )
    p_status.add_argument("url", help="MR/PR URL")
    p_status.set_defaults(func=cmd_mr_status)

    p_comments = subparsers.add_parser("mr-comments", help="Get unresolved review threads")
    p_comments.add_argument("url", help="MR/PR URL")
    p_comments.set_defaults(func=cmd_mr_comments)

    p_general = subparsers.add_parser(
        "mr-general-comments", help="Get general (non-diff-positioned) MR/PR comments"
    )
    p_general.add_argument("url", help="MR/PR URL")
    p_general.add_argument(
        "--since", help="Only return comments created after this ISO 8601 timestamp"
    )
    p_general.set_defaults(func=cmd_mr_general_comments)

    p_reply = subparsers.add_parser("mr-reply", help="Reply to a review thread")
    p_reply.add_argument("url", help="MR/PR URL")
    p_reply.add_argument("thread_id", help="Thread/discussion ID")
    p_reply.add_argument("message", help="Reply message")
    p_reply.set_defaults(func=cmd_mr_reply)

    p_resolve = subparsers.add_parser("mr-resolve", help="Resolve a review thread")
    p_resolve.add_argument("url", help="MR/PR URL")
    p_resolve.add_argument("thread_id", help="Thread/discussion ID")
    p_resolve.set_defaults(func=cmd_mr_resolve)

    p_pipeline = subparsers.add_parser(
        "pipeline-failures", help="Get failed pipeline job names and logs"
    )
    p_pipeline.add_argument("url", help="MR/PR URL")
    p_pipeline.set_defaults(func=cmd_pipeline_failures)

    p_update = subparsers.add_parser("mr-update", help="Update MR/PR title and/or description")
    p_update.add_argument("url", help="MR/PR URL")
    p_update.add_argument("--title", help="New title")
    p_update.add_argument("--description", help="New description")
    p_update.set_defaults(func=cmd_mr_update)

    p_diffpos = subparsers.add_parser(
        "mr-diff-position",
        help="Get first changed line position and diff refs (GitLab only)",
    )
    p_diffpos.add_argument("url", help="MR URL")
    p_diffpos.set_defaults(func=cmd_mr_diff_position)

    p_gh_token = subparsers.add_parser(
        "github-token", help="Generate a GitHub App installation token"
    )
    p_gh_token.add_argument("--app-id", required=True, help="GitHub App ID")
    p_gh_token.add_argument("--installation-id", required=True, help="GitHub App installation ID")
    p_gh_token.add_argument(
        "--private-key", required=True, help="PEM private key string or path to PEM file"
    )
    p_gh_token.set_defaults(func=cmd_github_token)

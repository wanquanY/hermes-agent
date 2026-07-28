"""Human-facing ``hermes lark`` command."""

from __future__ import annotations

import argparse
import json
from typing import Any

from plugins.lark_cli.service import LarkCliService


def register_lark_cli(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="lark_action", required=True)

    status = subparsers.add_parser("status", help="Check binding and authorization")
    status.add_argument("--no-verify", action="store_true")

    bind = subparsers.add_parser("bind", help="Bind to the active Hermes Feishu app")
    bind.add_argument(
        "--identity",
        choices=("bot-only", "user-default"),
        default="bot-only",
    )

    login = subparsers.add_parser("login", help="Start user device authorization")
    login.add_argument("--domain", action="append", default=[])
    login.add_argument("--scope", action="append", default=[])
    login.add_argument("--exclude", action="append", default=[])
    login.add_argument("--recommend", action="store_true")

    complete = subparsers.add_parser("complete", help="Complete device authorization")
    complete.add_argument("authorization_id")

    subparsers.add_parser("logout", help="Remove the current user authorization")

    check = subparsers.add_parser("check", help="Check granted OAuth scopes")
    check.add_argument("scope", nargs="+")

    run = subparsers.add_parser("run", help="Run a lark-cli business command")
    run.add_argument("--as", dest="identity", choices=("auto", "bot", "user"), default="auto")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("command")
    run.add_argument("arguments", nargs=argparse.REMAINDER)

    skills = subparsers.add_parser("skills", help="Read official embedded Skills")
    skill_subparsers = skills.add_subparsers(dest="skill_action", required=True)
    skill_list = skill_subparsers.add_parser("list")
    skill_list.add_argument("path", nargs="?", default="")
    skill_read = skill_subparsers.add_parser("read")
    skill_read.add_argument("name")
    skill_read.add_argument("path", nargs="?", default="")


def lark_command(args: argparse.Namespace) -> int:
    service = LarkCliService()
    action = args.lark_action
    if action == "status":
        result = service.status(verify=not args.no_verify)
    elif action == "bind":
        result = service.bind(identity=args.identity)
    elif action == "login":
        result = service.start_login(
            domains=args.domain,
            scopes=args.scope,
            excludes=args.exclude,
            recommend=args.recommend,
        )
    elif action == "complete":
        result = service.complete_login(authorization_id=args.authorization_id)
    elif action == "logout":
        result = service.logout()
    elif action == "check":
        result = service.check_scopes(args.scope)
    elif action == "run":
        result = service.run_business_command(
            command=args.command,
            arguments=args.arguments,
            identity=args.identity,
            dry_run=args.dry_run,
        )
    elif action == "skills" and args.skill_action == "list":
        result = service.list_skills(args.path)
    elif action == "skills" and args.skill_action == "read":
        result = service.read_skill(args.name, args.path)
    else:
        result = {"ok": False, "error": f"unsupported action: {action}"}

    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result.get("ok") else 1


def _json_default(value: Any) -> str:
    return str(value)

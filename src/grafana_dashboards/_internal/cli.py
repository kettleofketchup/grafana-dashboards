# Why does this file exist, and why not put this in `__main__`?
#
# You might be tempted to import things from `__main__` later,
# but that will cause problems: the code will get executed twice:
#
# - When you run `python -m grafana_dashboards` python will execute
#   `__main__.py` as a script. That means there won't be any
#   `grafana_dashboards.__main__` in `sys.modules`.
# - When you import `__main__` it will get executed again (as a module) because
#   there's no `grafana_dashboards.__main__` in `sys.modules`.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from grafana_foundation_sdk.cog.encoder import JSONEncoder

from grafana_dashboards._internal import debug
from grafana_dashboards._internal.envelope import wrap_v2
from grafana_dashboards._internal.validate import validate_v2
from grafana_dashboards.dashboards import all_dashboards


class _DebugInfo(argparse.Action):
    def __init__(self, nargs: int | str | None = 0, **kwargs: Any) -> None:
        super().__init__(nargs=nargs, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        debug._print_debug_info()
        sys.exit(0)


def get_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser.

    Returns:
        An argparse parser.
    """
    parser = argparse.ArgumentParser(prog="kgd")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {debug._get_version()}")
    parser.add_argument("--debug-info", action=_DebugInfo, help="Print debug information.")

    sub = parser.add_subparsers(dest="command")

    p_gen = sub.add_parser("generate", help="Render dashboard JSON to a directory.")
    p_gen.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("dist"),
        help="Output directory (default: dist/). Created if missing.",
    )
    p_gen.add_argument(
        "-d",
        "--dashboard",
        action="append",
        default=None,
        help="Generate only this dashboard slug. May be repeated. Defaults to all.",
    )
    p_gen.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the v2 structural validator (not recommended).",
    )

    sub.add_parser("list", help="List registered dashboard slugs.")

    return parser


def _cmd_list() -> int:
    for slug in sorted(all_dashboards()):
        print(slug)
    return 0


def _cmd_generate(output_dir: Path, only: list[str] | None, *, validate: bool) -> int:
    registry = all_dashboards()
    selected = sorted(only) if only else sorted(registry)
    missing = [s for s in selected if s not in registry]
    if missing:
        print(f"unknown dashboard slug(s): {', '.join(missing)}", file=sys.stderr)
        print(f"available: {', '.join(sorted(registry))}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    encoder = JSONEncoder(sort_keys=False, indent=2)

    total_issues = 0
    for slug in selected:
        spec = registry[slug]()
        body = json.loads(encoder.encode(spec.builder.build()))
        dashboard = wrap_v2(body, uid=spec.uid)

        if validate:
            issues = validate_v2(dashboard)
            for issue in issues:
                print(f"{slug}: {issue}", file=sys.stderr)
            total_issues += len(issues)

        out_path = output_dir / f"{spec.uid}.json"
        out_path.write_text(json.dumps(dashboard, indent=2) + "\n")
        print(f"wrote {out_path}")

    if total_issues:
        print(f"\n{total_issues} validation issue(s) — fix before publishing.", file=sys.stderr)
        return 1
    return 0


def main(args: list[str] | None = None) -> int:
    """Run the main program.

    This function is executed when you type `kgd` or `python -m grafana_dashboards`.

    Parameters:
        args: Arguments passed from the command line.

    Returns:
        An exit code.
    """
    parser = get_parser()
    opts = parser.parse_args(args=args)

    if opts.command == "generate":
        return _cmd_generate(opts.output, opts.dashboard, validate=not opts.no_validate)
    if opts.command == "list":
        return _cmd_list()

    parser.print_help()
    return 0

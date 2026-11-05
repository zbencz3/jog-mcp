"""Build and check the acts/ corpus from legislatie.just.ro.

    jog-mcp-fetch                  fetch every act in acts.yaml, write the changed ones
    jog-mcp-fetch --only ID        just one act
    jog-mcp-fetch --dry-run        report, write nothing
    jog-mcp-fetch --check [--deep] report what each doc_id actually contains

This is the only part of the project that writes, and it is never reachable
from the MCP server: the server reads acts/ and imports none of this.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_config(path: str = "") -> dict:
    import yaml
    with open(path or os.path.join(REPO, "acts.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(argv: list | None = None) -> int:
    from . import acts

    ap = argparse.ArgumentParser(prog="jog-mcp-fetch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="", help="path to acts.yaml")
    ap.add_argument("--only", metavar="ID", default="", help="just this act")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--check", action="store_true",
                    help="report what each tracked doc_id contains")
    ap.add_argument("--deep", action="store_true",
                    help="--check: also fetch the alt_ids")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.check:
        problems = acts.check(cfg, deep=args.deep, only=args.only)
        print(f"\n{problems} problem(s)")
        return 1 if problems else 0
    print(f"corpus: {acts.acts_root(cfg)}")
    results = acts.refresh(cfg, only=args.only, dry_run=args.dry_run)
    failed = [r for r in results if r["status"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

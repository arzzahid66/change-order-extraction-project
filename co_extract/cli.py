"""python -m co_extract <file-or-dir> [--mode auto|llm|rules] [--out DIR]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from .pipeline import run

SUPPORTED = {".pdf", ".txt", ".eml", ".md", ".png", ".jpg", ".jpeg"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="co_extract", description=__doc__)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--mode", choices=["auto", "llm", "rules"], default="auto")
    ap.add_argument("--out", type=Path, help="write one JSON per input here (default: stdout)")
    args = ap.parse_args(argv)
    load_dotenv()

    files = [p for i in args.inputs for p in (sorted(i.iterdir()) if i.is_dir() else [i])
             if p.suffix.lower() in SUPPORTED]
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    for f in files:
        result = run(f, mode=args.mode)
        payload = result.model_dump_json(indent=2)
        if args.out:
            (args.out / f"{f.stem}.json").write_text(payload, encoding="utf-8")
            for co in result.change_orders:
                tag = "REVIEW" if co.needs_review else "AUTO  "
                print(f"[{tag}] {f.name:<32} CO {co.change_order_number.value!s:<8} "
                      f"total={co.total_amount.value!s:<12} conf={co.overall_confidence}", file=sys.stderr)
        else:
            print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

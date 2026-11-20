"""Command-line entry point: compile a .rig file to OpenTofu/Terraform HCL."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .codegen import CodegenError, generate
from .lexer import LexError
from .parser import ParseError, parse


def main(argv: list[str] | None = None) -> int:
    arg_parser = argparse.ArgumentParser(
        prog="infrarig",
        description="Compile the infrarig experimental language to OpenTofu/Terraform HCL.",
    )
    arg_parser.add_argument("source", type=Path, help="path to a .rig DSL source file")
    arg_parser.add_argument("-o", "--out", type=Path, help="write output here instead of stdout")
    args = arg_parser.parse_args(argv)

    source = args.source.read_text()
    try:
        output = generate(parse(source))
    except (LexError, ParseError, CodegenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        args.out.write_text(output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

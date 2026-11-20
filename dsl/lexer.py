"""Tokenizer for the infrarig experimental language."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Token:
    kind: str
    value: str
    line: int


_TOKEN_SPEC = [
    ("COMMENT", r"#[^\n]*"),
    ("STRING", r'"(?:[^"\\]|\\.)*"'),
    ("NUMBER", r"-?\d+(?:\.\d+)?"),
    ("IDENT", r"[A-Za-z_][A-Za-z0-9_.]*"),
    ("LBRACE", r"\{"),
    ("RBRACE", r"\}"),
    ("LBRACKET", r"\["),
    ("RBRACKET", r"\]"),
    ("EQUALS", r"="),
    ("COMMA", r","),
    ("NEWLINE", r"\n"),
    ("SKIP", r"[ \t\r]+"),
]
_MASTER_RE = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC)
)


class LexError(Exception):
    pass


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    line = 1
    pos = 0
    while pos < len(source):
        match = _MASTER_RE.match(source, pos)
        if not match:
            raise LexError(f"line {line}: unexpected character {source[pos]!r}")
        kind = match.lastgroup
        value = match.group()
        if kind == "NEWLINE":
            line += 1
        elif kind not in ("SKIP", "COMMENT"):
            tokens.append(Token(kind, value, line))
        pos = match.end()
    tokens.append(Token("EOF", "", line))
    return tokens

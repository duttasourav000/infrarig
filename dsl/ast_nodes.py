"""AST node types produced by the parser."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResourceDecl:
    type: str
    name: str
    attrs: dict[str, Any]
    line: int


@dataclass
class RelationStmt:
    left: str
    verb: str
    right: list[str]
    attrs: dict[str, Any]
    line: int


@dataclass
class Program:
    resources: list[ResourceDecl] = field(default_factory=list)
    relations: list[RelationStmt] = field(default_factory=list)

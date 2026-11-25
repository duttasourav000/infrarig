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
    # `web mirrors "us-west-2" as usw2`: the target is a string literal
    # (a region), and `as` names the copy. Only component mirrors use these.
    literal_target: bool = False
    alias: str | None = None


@dataclass
class ComponentDecl:
    name: str
    members: list[str]
    line: int


@dataclass
class Program:
    resources: list[ResourceDecl] = field(default_factory=list)
    relations: list[RelationStmt] = field(default_factory=list)
    components: list[ComponentDecl] = field(default_factory=list)

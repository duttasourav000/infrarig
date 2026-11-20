"""Recursive-descent parser for the infrarig experimental language.

Grammar (informal):

    program    := statement*
    statement  := relation_stmt | resource_decl
    relation_stmt := IDENT VERB target block?
    target     := IDENT | "[" IDENT ("," IDENT)* "]"
    resource_decl := IDENT IDENT block?
    block      := "{" (IDENT "=" value ","?)* "}"
    value      := STRING | NUMBER | BOOL | "[" (value ","?)* "]"

A statement is disambiguated by its second token: if it is one of the verb
keywords, the statement is a relation; otherwise the second identifier is
a resource name and the first is its type.
"""
from __future__ import annotations

from typing import Any

from .ast_nodes import Program, RelationStmt, ResourceDecl
from .lexer import Token, tokenize

VERBS = {"reads", "writes", "replicates"}


class ParseError(Exception):
    pass


def _decode_string(raw: str) -> str:
    # Backslash-escapes inside a STRING token (e.g. \" so a string can
    # contain a literal quote - needed for attrs that hold a raw HCL
    # expression, like Secret's `value`).
    return raw.replace('\\"', '"').replace("\\\\", "\\")


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> Token:
        tok = self.peek()
        if tok.kind != kind:
            raise ParseError(
                f"line {tok.line}: expected {kind}, got {tok.kind} {tok.value!r}"
            )
        return self.advance()

    def parse_program(self) -> Program:
        program = Program()
        while self.peek().kind != "EOF":
            self._parse_statement(program)
        return program

    def _parse_statement(self, program: Program) -> None:
        first = self.expect("IDENT")
        second = self.peek()

        if second.kind == "IDENT" and second.value in VERBS:
            self.advance()
            right = self._parse_target()
            attrs = self._parse_optional_block()
            program.relations.append(
                RelationStmt(left=first.value, verb=second.value, right=right, attrs=attrs, line=first.line)
            )
            return

        name = self.expect("IDENT")
        attrs = self._parse_optional_block()
        program.resources.append(
            ResourceDecl(type=first.value, name=name.value, attrs=attrs, line=first.line)
        )

    def _parse_target(self) -> list[str]:
        if self.peek().kind != "LBRACKET":
            return [self.expect("IDENT").value]
        self.advance()
        refs: list[str] = []
        while self.peek().kind != "RBRACKET":
            refs.append(self.expect("IDENT").value)
            if self.peek().kind == "COMMA":
                self.advance()
        self.expect("RBRACKET")
        return refs

    def _parse_optional_block(self) -> dict[str, Any]:
        if self.peek().kind != "LBRACE":
            return {}
        self.advance()
        attrs: dict[str, Any] = {}
        while self.peek().kind != "RBRACE":
            key = self.expect("IDENT")
            self.expect("EQUALS")
            attrs[key.value] = self._parse_value()
            if self.peek().kind == "COMMA":
                self.advance()
        self.expect("RBRACE")
        return attrs

    def _parse_value(self) -> Any:
        tok = self.peek()
        if tok.kind == "STRING":
            self.advance()
            return _decode_string(tok.value[1:-1])
        if tok.kind == "NUMBER":
            self.advance()
            return float(tok.value) if "." in tok.value else int(tok.value)
        if tok.kind == "LBRACKET":
            self.advance()
            items: list[Any] = []
            while self.peek().kind != "RBRACKET":
                items.append(self._parse_value())
                if self.peek().kind == "COMMA":
                    self.advance()
            self.expect("RBRACKET")
            return items
        if tok.kind == "IDENT" and tok.value in ("true", "false"):
            self.advance()
            return tok.value == "true"
        raise ParseError(f"line {tok.line}: unexpected token {tok.kind} {tok.value!r} in value")


def parse(source: str) -> Program:
    return Parser(tokenize(source)).parse_program()

"""Expands `Component` mirrors into plain resources and relations.

    Component web [app, uploads]

    web mirrors "us-west-2" {
      app { ami = "ami-...", subnet_ids = ["subnet-..."] }
    }

copies every member of `web` into us-west-2 as `web_us_west_2_app` and
`web_us_west_2_uploads` (also referable as `web.us_west_2.app`). With
`web mirrors "us-west-2" as usw2`, the label is `usw2` instead:
`web_usw2_app`, `web.usw2.app`. Codegen never sees components: this pass
rewrites the program into the resources and relations it already
understands.

- Each member is copied with its attributes, then the per-member override
  block (`app { ... }`) is applied on top.
- Relations whose left side is a member are copied, with every member they
  mention swapped for its copy. Relations that only target a member, from
  outside the component, are not copied.
- Every Bucket member mirrors all of its copies, and the copies mirror each
  other too, so each copy replicates to every other one.
- Copies keep the AWS-level names of the original where those are regional
  (launch template/ASG `name`) or shared on purpose (`role_name`,
  `log_group`), so a server and its copies share one IAM role. A copied
  Bucket's `bucket` defaults to `<bucket>-<region>`, because S3 bucket
  names are global.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any

from .ast_nodes import ComponentDecl, Program, RelationStmt, ResourceDecl
from .errors import CodegenError

# Types a component can hold. Registry and Secret already have their own
# cross-region story (`replica_region`, and a single secret), and copying
# them as independent per-region resources would silently fork them.
_COPYABLE_TYPES = {"ScaledServer", "Bucket"}

# Attributes that only make sense in one region, so a copied ScaledServer
# must override them. `subnet_ids` is required even when the original
# relies on the `var.subnet_ids` fallback: that variable is one region's.
_ALWAYS_REGIONAL = ("ami", "subnet_ids")
_REGIONAL_IF_SET = ("security_group_ids",)

# Relation attributes that become AWS names; suffixed on copies so they
# don't collide with the original relation's.
_RELATION_NAME_ATTRS = ("name", "role_name")


@dataclass
class _Mirror:
    component: ComponentDecl
    label: str  # `usw2` from `as usw2`, else the region as an identifier
    instance: str  # `<component>_<label>`: the prefix for copy names
    region: str
    overrides: dict[str, dict[str, Any]]
    line: int


def expand_components(program: Program) -> Program:
    _reject_nested_blocks(program)

    components = {c.name: c for c in program.components}
    if not components:
        for stmt in program.relations:
            _reject_nested_relation_attrs(stmt)
        return program

    by_name = {r.name: r for r in program.resources}
    _validate_components(program.components, by_name)

    mirrors: list[_Mirror] = []
    plain_relations: list[RelationStmt] = []
    for stmt in program.relations:
        if stmt.left in components:
            mirrors.append(_parse_mirror(stmt, components[stmt.left], by_name))
        else:
            _reject_nested_relation_attrs(stmt)
            plain_relations.append(stmt)

    instance_names = [m.instance for m in mirrors]
    for m in mirrors:
        if instance_names.count(m.instance) > 1:
            hint = " (use `as <name>` to tell them apart)" if m.label == _sanitize(m.region) else ""
            raise CodegenError(
                f"line {m.line}: component '{m.component.name}' is mirrored as '{m.label}' more than once{hint}"
            )
        same_region = [o for o in mirrors if o.component is m.component and o.region == m.region]
        if len(same_region) > 1:
            raise CodegenError(f"line {m.line}: component '{m.component.name}' is mirrored to {m.region} more than once")

    # `web.app` -> `app`, `web.usw2.app` -> `web_usw2_app`
    dotted: dict[str, str] = {}
    for component in program.components:
        for member in component.members:
            dotted[f"{component.name}.{member}"] = member
    for m in mirrors:
        for member in m.component.members:
            dotted[f"{m.component.name}.{m.label}.{member}"] = _copy_name(m.instance, member)

    # Pin bare type refs (`ScaledServer reads Bucket`) to the resource they
    # mean now, before copies make them ambiguous.
    plain_relations = [_rewrite_refs(stmt, program.resources, dotted) for stmt in plain_relations]

    resources = list(program.resources)
    relations = list(plain_relations)
    taken = set(by_name)
    for m in mirrors:
        copies = {member: _copy_name(m.instance, member) for member in m.component.members}
        for member, copy in copies.items():
            if copy in taken:
                raise CodegenError(
                    f"line {m.line}: copy of '{member}' would be named '{copy}', which is already taken"
                )
            taken.add(copy)
            resources.append(_copy_resource(by_name[member], copy, m))
        for stmt in plain_relations:
            if stmt.left in copies:
                relations.append(_copy_relation(stmt, copies, m.instance))

    # Every bucket member mirrors all its copies, and the copies each other.
    for component in program.components:
        component_mirrors = [m for m in mirrors if m.component is component]
        for member in component.members:
            if by_name[member].type != "Bucket" or not component_mirrors:
                continue
            group = [member] + [_copy_name(m.instance, member) for m in component_mirrors]
            line = component_mirrors[0].line
            for a, b in combinations(group, 2):
                relations.append(RelationStmt(left=a, verb="mirrors", right=[b], attrs={}, line=line))

    return Program(resources=resources, relations=relations, components=[])


def _copy_name(instance: str, member: str) -> str:
    return f"{instance}_{member}"


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", value.lower())


def _validate_components(
    components: list[ComponentDecl], by_name: dict[str, ResourceDecl]
) -> None:
    members_of: dict[str, str] = {}
    seen: set[str] = set()
    for component in components:
        if component.name in seen:
            raise CodegenError(f"line {component.line}: component '{component.name}' is declared more than once")
        seen.add(component.name)
        if component.name in by_name:
            raise CodegenError(f"line {component.line}: '{component.name}' is already the name of a resource")
        if not component.members:
            raise CodegenError(f"line {component.line}: component '{component.name}' has no members")
        for member in component.members:
            if member not in by_name:
                raise CodegenError(
                    f"line {component.line}: component '{component.name}' member '{member}' is not a "
                    "declared resource name (bare type names aren't allowed here)"
                )
            decl = by_name[member]
            if decl.type not in _COPYABLE_TYPES:
                raise CodegenError(
                    f"line {component.line}: component '{component.name}' can't hold {decl.type} "
                    f"'{member}' (supported: {', '.join(sorted(_COPYABLE_TYPES))})"
                )
            if member in members_of:
                raise CodegenError(
                    f"line {component.line}: '{member}' is already a member of component '{members_of[member]}'"
                )
            members_of[member] = component.name


def _parse_mirror(stmt: RelationStmt, component: ComponentDecl, by_name: dict[str, ResourceDecl]) -> _Mirror:
    if stmt.verb != "mirrors":
        raise CodegenError(f"line {stmt.line}: a component only supports 'mirrors', not '{stmt.verb}'")
    if not stmt.literal_target:
        raise CodegenError(
            f"line {stmt.line}: mirror a component to a region, e.g. "
            f'`{component.name} mirrors "us-west-2"` (optionally `as <name>`)'
        )
    region = stmt.right[0]
    if not region:
        raise CodegenError(f"line {stmt.line}: mirror region can't be empty")
    label = stmt.alias or _sanitize(region)
    instance = f"{component.name}_{label}"

    overrides: dict[str, dict[str, Any]] = {}
    for key, value in stmt.attrs.items():
        if not isinstance(value, dict):
            hint = " (the region is the mirror's target)" if key == "region" else ""
            raise CodegenError(
                f"line {stmt.line}: unknown attribute '{key}' on a component mirror{hint}; "
                f"only per-member blocks like `{component.members[0]} {{ ... }}` are allowed"
            )
        if key not in component.members:
            raise CodegenError(f"line {stmt.line}: '{key}' is not a member of component '{component.name}'")
        for attr, attr_value in value.items():
            if isinstance(attr_value, dict):
                raise CodegenError(f"line {stmt.line}: nested block '{attr}' isn't allowed inside '{key}'")
            if attr == "region":
                raise CodegenError(
                    f"line {stmt.line}: set the region on the mirror, not on member '{key}'"
                )
        overrides[key] = value

    for member in component.members:
        decl = by_name[member]
        if decl.type != "ScaledServer":
            continue
        provided = overrides.get(member, {})
        required = list(_ALWAYS_REGIONAL) + [a for a in _REGIONAL_IF_SET if a in decl.attrs]
        missing = [a for a in required if a not in provided]
        if missing:
            raise CodegenError(
                f"line {stmt.line}: mirroring '{member}' to {region} needs region-specific "
                f"override(s): {', '.join(missing)} (e.g. `{member} {{ {missing[0]} = ... }}`)"
            )

    return _Mirror(
        component=component, label=label, instance=instance, region=region, overrides=overrides, line=stmt.line
    )


def _copy_resource(original: ResourceDecl, copy_name: str, mirror: _Mirror) -> ResourceDecl:
    original_region = original.attrs.get("region")
    if original_region == mirror.region:
        raise CodegenError(
            f"line {mirror.line}: '{original.name}' is already in {mirror.region}; mirror to a different region"
        )
    if not original_region:
        raise CodegenError(
            f"line {mirror.line}: component member '{original.name}' needs its own 'region' to be mirrored"
        )

    attrs = dict(original.attrs)
    attrs["region"] = mirror.region
    if original.type == "ScaledServer":
        attrs.setdefault("name", original.name)
        attrs.setdefault("role_name", f"{original.name}-role")
        attrs.setdefault("log_group", original.name)
    elif original.type == "Bucket":
        if "bucket" in attrs:
            attrs["bucket"] = f"{attrs['bucket']}-{mirror.region}"
    attrs.update(mirror.overrides.get(original.name, {}))
    return ResourceDecl(type=original.type, name=copy_name, attrs=attrs, line=mirror.line)


def _rewrite_refs(stmt: RelationStmt, resources: list[ResourceDecl], dotted: dict[str, str]) -> RelationStmt:
    names = {r.name for r in resources}

    def rewrite(ref: str) -> str:
        if ref in dotted:
            return dotted[ref]
        if "." in ref:
            raise CodegenError(f"line {stmt.line}: unknown component reference '{ref}'")
        if ref in names:
            return ref
        of_type = [r.name for r in resources if r.type == ref]
        # Leave unknown/ambiguous refs alone; codegen reports them.
        return of_type[0] if len(of_type) == 1 else ref

    return replace(stmt, left=rewrite(stmt.left), right=[rewrite(r) for r in stmt.right])


def _copy_relation(stmt: RelationStmt, copies: dict[str, str], instance: str) -> RelationStmt:
    attrs = dict(stmt.attrs)
    for key in _RELATION_NAME_ATTRS:
        if key in attrs:
            attrs[key] = f"{attrs[key]}-{instance}"
    return replace(
        stmt,
        left=copies[stmt.left],
        right=[copies.get(r, r) for r in stmt.right],
        attrs=attrs,
    )


def _reject_nested_blocks(program: Program) -> None:
    for decl in program.resources:
        for key, value in decl.attrs.items():
            if isinstance(value, dict):
                raise CodegenError(
                    f"line {decl.line}: nested block '{key}' isn't allowed in a resource "
                    "(only in a component mirror)"
                )


def _reject_nested_relation_attrs(stmt: RelationStmt) -> None:
    if stmt.literal_target:
        raise CodegenError(
            f"line {stmt.line}: '{stmt.left} {stmt.verb} \"{stmt.right[0]}\"': only a Component can "
            "target a region string"
        )
    for key, value in stmt.attrs.items():
        if isinstance(value, dict):
            raise CodegenError(
                f"line {stmt.line}: nested block '{key}' is only allowed when mirroring a component"
            )

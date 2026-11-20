"""Compiles a infrarig Program into OpenTofu/Terraform HCL.

Resource types: `ScaledServer`, `Bucket`, `Registry` (container registry),
`Secret` (a Secrets Manager secret - with
a `value` attribute it's created here too; a raw HCL expression, e.g.
referencing `var.ssm_params`, not a quoted literal - keeps real values
out of the DSL source. Without `value`, it's assumed to be managed
elsewhere and only the IAM read grant is generated).
Verbs: `reads` (Bucket/Secret/Registry), `writes` (Bucket), `replicates` (Bucket -> Bucket, cross-region).

`ScaledServer` compiles to a launch template + autoscaling group (the
shape used in modules/regional/main.tf), not a single instance. Multiple
`ScaledServer`s can share one IAM identity by setting the same
`role_name` attribute - this mirrors the real setup, where one global
role (`EC2.example.com`) backs every server in every region.

A resource's `region` attribute decides which provider config it uses.
There's no hardcoded default region: whichever region is declared first
in the program (in source order) is treated as the primary region (the
root, unaliased provider) and needs no `provider` meta-argument; every
other region gets an auto-generated aliased `provider "aws"` block.
`Secret`/`Registry` need a resolvable region (their own, or the
program's primary) to build an ARN - if a program never declares any
region at all, it's only valid as long as it never uses one of those.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .ast_nodes import Program, RelationStmt, ResourceDecl

SUPPORTED_TYPES = {"ScaledServer", "Bucket", "Registry", "Secret"}

_DEFAULT_INSTANCE_TYPE = "t2.nano"
_DEFAULT_MIN_SIZE = 1
_DEFAULT_MAX_SIZE = 1
_DEFAULT_DESIRED_CAPACITY = 1

# verb -> actions, keyed by the target resource type
_ACTIONS = {
    ("Bucket", "reads"): ["s3:GetObject"],
    ("Bucket", "writes"): ["s3:PutObject"],
    ("Secret", "reads"): ["secretsmanager:GetSecretValue"],
}

_ECR_PULL_ACTIONS = [
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchGetImage",
    "ecr:BatchCheckLayerAvailability",
]


class CodegenError(Exception):
    pass


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", value.lower())


def _primary_region(program: Program) -> str | None:
    # Whichever region is declared first, in source order, needs no
    # provider alias - there's no hardcoded default region.
    for decl in program.resources:
        region = decl.attrs.get("region")
        if region:
            return region
    return None


def _region_of(decl: ResourceDecl, primary_region: str | None) -> str | None:
    return decl.attrs.get("region", primary_region)


def _require_region(decl: ResourceDecl, primary_region: str | None) -> str:
    region = _region_of(decl, primary_region)
    if not region:
        raise CodegenError(
            f"{decl.type} resource '{decl.name}' needs a 'region' (none was given, and no other "
            "resource in this program declares one to use as a default)"
        )
    return region


def _provider_meta(decl: ResourceDecl, primary_region: str | None, indent: str = "  ") -> str:
    region = _region_of(decl, primary_region)
    if not region or region == primary_region:
        return ""
    return f"\n{indent}provider = aws.{_sanitize(region)}"


def _collect_regions(program: Program, primary_region: str | None) -> list[str]:
    regions: set[str] = set()
    for decl in program.resources:
        region = decl.attrs.get("region")
        if region:
            regions.add(region)
        if decl.type == "Registry" and decl.attrs.get("replica_region"):
            regions.add(decl.attrs["replica_region"])
    regions.discard(primary_region)
    return sorted(regions)


def _emit_provider_alias(region: str) -> str:
    return f'provider "aws" {{\n  alias  = "{_sanitize(region)}"\n  region = "{region}"\n}}'


def _make_resolver(program: Program) -> Any:
    by_name = {r.name: r for r in program.resources}
    by_type: dict[str, list[ResourceDecl]] = defaultdict(list)
    for r in program.resources:
        by_type[r.type].append(r)

    def resolve(ref: str) -> ResourceDecl:
        if ref in by_name:
            return by_name[ref]
        if ref in by_type:
            candidates = by_type[ref]
            if len(candidates) == 1:
                return candidates[0]
            raise CodegenError(
                f"'{ref}' is ambiguous: {len(candidates)} {ref} resources declared, "
                "reference one by name instead"
            )
        raise CodegenError(f"unknown resource reference '{ref}'")

    return resolve


def _role_address_and_name(decl: ResourceDecl) -> tuple[str, str]:
    role_name = decl.attrs.get("role_name", f"{decl.name}-role")
    return _sanitize(role_name), role_name


def _emit_bucket(decl: ResourceDecl, primary_region: str | None) -> list[str]:
    bucket = decl.attrs.get("bucket")
    if not bucket:
        raise CodegenError(f"Bucket resource '{decl.name}' is missing a 'bucket' attribute")
    provider = _provider_meta(decl, primary_region)
    blocks = [
        f'resource "aws_s3_bucket" "{decl.name}" {{\n'
        f'  bucket = "{bucket}"{provider}\n'
        f"}}",
        f'resource "aws_s3_bucket_versioning" "{decl.name}" {{\n'
        f"  bucket = aws_s3_bucket.{decl.name}.id{provider}\n\n"
        f"  versioning_configuration {{\n"
        f'    status = "Enabled"\n'
        f"  }}\n"
        f"}}",
        f'resource "aws_s3_bucket_lifecycle_configuration" "{decl.name}" {{\n'
        f"  bucket = aws_s3_bucket.{decl.name}.id{provider}\n\n"
        f"  rule {{\n"
        f'    id     = "Delete unused data"\n'
        f'    status = "Enabled"\n\n'
        f"    filter {{}}\n\n"
        f"    expiration {{\n"
        f"      expired_object_delete_marker = true\n"
        f"    }}\n\n"
        f"    noncurrent_version_expiration {{\n"
        f"      noncurrent_days           = 1\n"
        f"      newer_noncurrent_versions = 1\n"
        f"    }}\n\n"
        f"    abort_incomplete_multipart_upload {{\n"
        f"      days_after_initiation = 1\n"
        f"    }}\n"
        f"  }}\n"
        f"}}",
    ]
    return blocks


def _emit_registry(decl: ResourceDecl, account_id_expr: str, primary_region: str | None) -> list[str]:
    repository = decl.attrs.get("repository")
    if not repository:
        raise CodegenError(f"Registry resource '{decl.name}' is missing a 'repository' attribute")
    provider = _provider_meta(decl, primary_region)
    tags = decl.attrs.get("tags", {"Purpose": decl.name})
    tags_hcl = ", ".join(f'"{k}" = "{v}"' for k, v in tags.items())
    blocks = [
        f'resource "aws_ecr_repository" "{decl.name}" {{\n'
        f'  name                 = "{repository}"{provider}\n'
        f'  image_tag_mutability = "MUTABLE"\n'
        f"  tags                 = {{ {tags_hcl} }}\n\n"
        f"  encryption_configuration {{\n"
        f'    encryption_type = "AES256"\n'
        f"  }}\n"
        f"}}"
    ]
    replica_region = decl.attrs.get("replica_region")
    if replica_region:
        blocks.append(
            f'resource "aws_ecr_replication_configuration" "{decl.name}" {{\n'
            f"  replication_configuration {{\n"
            f"    rule {{\n"
            f"      destination {{\n"
            f'        region      = "{replica_region}"\n'
            f"        registry_id = {account_id_expr}\n"
            f"      }}\n\n"
            f"      repository_filter {{\n"
            f'        filter      = "{repository}"\n'
            f'        filter_type = "PREFIX_MATCH"\n'
            f"      }}\n"
            f"    }}\n"
            f"  }}\n"
            f"}}"
        )
    return blocks


def _repo_arns(registry: ResourceDecl, account_id_expr: str, primary_region: str | None) -> list[str]:
    # replica_region only drives the aws_ecr_replication_configuration
    # resource (see _emit_registry) - it does NOT add a second ARN here.
    # Cover a replica's own ARN by also listing a second Registry decl in
    # the same `reads [...]` target list.
    repository = registry.attrs.get("repository")
    region = _require_region(registry, primary_region)
    return [f'"arn:aws:ecr:{region}:${{{account_id_expr}}}:repository/{repository}"']


def _secret_arn(secret: ResourceDecl, account_id_expr: str, primary_region: str | None) -> str:
    name = secret.attrs.get("name")
    if not name:
        raise CodegenError(f"Secret resource '{secret.name}' is missing a 'name' attribute")
    if "value" in secret.attrs:
        # This DSL creates the secret itself (see _emit_secret) - reference
        # its exact ARN rather than guessing.
        return f"aws_secretsmanager_secret.{secret.name}.arn"
    # Otherwise the secret is managed outside this DSL. Secrets Manager
    # appends a random 6-character suffix to every secret's ARN, which we
    # don't know ahead of time - "??????" (6 single-char wildcards) is
    # AWS's own recommended pattern for scoping a policy to a secret by
    # name without knowing that suffix.
    region = _require_region(secret, primary_region)
    return f'"arn:aws:secretsmanager:{region}:${{{account_id_expr}}}:secret:{name}-??????"'


def _emit_secret(decl: ResourceDecl) -> list[str]:
    # No `value` means the secret is managed elsewhere - stay fully
    # virtual, same as a Secret only ever used for its IAM read grant.
    value_expr = decl.attrs.get("value")
    if not value_expr:
        return []
    name = decl.attrs.get("name")
    if not name:
        raise CodegenError(f"Secret resource '{decl.name}' is missing a 'name' attribute")
    blocks = [
        f'resource "aws_secretsmanager_secret" "{decl.name}" {{\n  name = "{name}"\n}}',
        f'resource "aws_secretsmanager_secret_version" "{decl.name}" {{\n'
        f"  secret_id     = aws_secretsmanager_secret.{decl.name}.id\n"
        f"  secret_string = {value_expr}\n"
        f"}}",
    ]
    return blocks


def _bucket_arns(targets: list[ResourceDecl]) -> list[str]:
    return [f'"${{aws_s3_bucket.{t.name}.arn}}/*"' for t in targets]


def _action_block(actions: list[str], indent: str) -> str:
    # AWS returns a single-action policy's "Action" as a bare string, not a
    # one-element list - matching that avoids a spurious plan diff.
    if len(actions) == 1:
        return f'{indent}Action = "{actions[0]}"'
    lines = ",\n".join(f'{indent}  "{a}"' for a in actions)
    return f"{indent}Action = [\n{lines}\n{indent}]"


def _emit_assume_role_trust(service: str) -> str:
    return (
        f"jsonencode({{\n"
        f'    Version = "2012-10-17"\n'
        f"    Statement = [{{\n"
        f'      Action = "sts:AssumeRole"\n'
        f'      Effect = "Allow"\n'
        f"      Principal = {{\n"
        f'        Service = "{service}"\n'
        f"      }}\n"
        f"    }}]\n"
        f"  }})"
    )


def _emit_iam_role(address: str, aws_name: str, description: str | None) -> str:
    trust = _emit_assume_role_trust("ec2.amazonaws.com")
    description_line = f'  description = "{description}"\n' if description else ""
    return (
        f'resource "aws_iam_role" "{address}" {{\n'
        f'  name = "{aws_name}"\n'
        f"{description_line}\n"
        f"  assume_role_policy = {trust}\n"
        f"}}"
    )


def _emit_iam_instance_profile(address: str, aws_name: str) -> str:
    return (
        f'resource "aws_iam_instance_profile" "{address}" {{\n'
        f'  name = "{aws_name}"\n'
        f"  role = aws_iam_role.{address}.name\n"
        f"}}"
    )


def _emit_ssm_core_attachment(address: str) -> str:
    return (
        f'resource "aws_iam_role_policy_attachment" "{address}_ssm_core" {{\n'
        f"  role       = aws_iam_role.{address}.name\n"
        f'  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"\n'
        f"}}"
    )


def _emit_monitoring_policy(address: str, log_group: str, regions: list[str], account_id_expr: str) -> str:
    def arns(suffix: str) -> str:
        items = ",\n".join(f'            "arn:aws:logs:{r}:${{{account_id_expr}}}:{suffix}"' for r in regions)
        return f"[\n{items},\n        ]"

    return (
        f'resource "aws_iam_role_policy" "{address}_monitoring" {{\n'
        f'  name = "Monitoring"\n'
        f"  role = aws_iam_role.{address}.id\n\n"
        f"  policy = jsonencode({{\n"
        f'    Version = "2012-10-17"\n'
        f"    Statement = [\n"
        f"      {{\n"
        f'        Sid      = "VisualEditor0"\n'
        f'        Effect   = "Allow"\n'
        f'        Action   = "cloudwatch:PutMetricData"\n'
        f'        Resource = "*"\n'
        f"      }},\n"
        f"      {{\n"
        f'        Sid      = "VisualEditor1"\n'
        f'        Effect   = "Allow"\n'
        f'        Action   = "logs:CreateLogStream"\n'
        f"        Resource = {arns(f'log-group:{log_group}:log-stream:*')}\n"
        f"      }},\n"
        f"      {{\n"
        f'        Sid      = "VisualEditor2"\n'
        f'        Effect   = "Allow"\n'
        f'        Action   = "logs:CreateLogGroup"\n'
        f"        Resource = {arns('log-group:*')}\n"
        f"      }},\n"
        f"      {{\n"
        f'        Sid      = "VisualEditor3"\n'
        f'        Effect   = "Allow"\n'
        f'        Action   = "logs:PutLogEvents"\n'
        f"        Resource = {arns(f'log-group:{log_group}:log-stream:backend')}\n"
        f"      }},\n"
        f"    ]\n"
        f"  }})\n"
        f"}}"
    )


def _emit_relation_policy(
    role_address: str, policy_name: str, actions: list[str], resources: list[str]
) -> str:
    action_block = _action_block(actions, "      ")
    resource_lines = ",\n".join(f"        {r}" for r in resources)
    return (
        f'resource "aws_iam_role_policy" "{policy_name}" {{\n'
        f'  name = "{policy_name}"\n'
        f"  role = aws_iam_role.{role_address}.id\n\n"
        f"  policy = jsonencode({{\n"
        f'    Version = "2012-10-17"\n'
        f"    Statement = [{{\n"
        f'      Sid    = "VisualEditor0"\n'
        f'      Effect = "Allow"\n'
        f"{action_block}\n"
        f"      Resource = [\n"
        f"{resource_lines}\n"
        f"      ]\n"
        f"    }}]\n"
        f"  }})\n"
        f"}}"
    )


def _emit_ecr_auth_policy(role_address: str, policy_name: str, repo_arns: list[str]) -> str:
    resource_lines = ",\n".join(f"          {r}" for r in repo_arns)
    return (
        f'resource "aws_iam_role_policy" "{policy_name}" {{\n'
        f'  name = "{policy_name}"\n'
        f"  role = aws_iam_role.{role_address}.id\n\n"
        f"  policy = jsonencode({{\n"
        f'    Version = "2012-10-17"\n'
        f"    Statement = [\n"
        f"      {{\n"
        f'        Sid      = "VisualEditor0"\n'
        f'        Effect   = "Allow"\n'
        f'        Action   = "ecr:GetAuthorizationToken"\n'
        f'        Resource = "*"\n'
        f"      }},\n"
        f"      {{\n"
        f'        Sid    = "VisualEditor1"\n'
        f'        Effect = "Allow"\n'
        f"        Action = [\n"
        + ",\n".join(f'          "{a}"' for a in _ECR_PULL_ACTIONS)
        + "\n"
        f"        ]\n"
        f"        Resource = [\n"
        f"{resource_lines}\n"
        f"        ]\n"
        f"      }},\n"
        f"    ]\n"
        f"  }})\n"
        f"}}"
    )


def _emit_launch_template(decl: ResourceDecl, role_address: str | None, primary_region: str | None) -> str:
    aws_name = decl.attrs.get("name", decl.name)
    ami = decl.attrs.get("ami")
    if not ami:
        raise CodegenError(f"ScaledServer resource '{decl.name}' is missing an 'ami' attribute")
    instance_type = decl.attrs.get("instance_type", _DEFAULT_INSTANCE_TYPE)
    provider = _provider_meta(decl, primary_region)
    lines = [
        f'resource "aws_launch_template" "{decl.name}" {{',
        f'  name                   = "{aws_name}"',
        f'  image_id               = "{ami}"',
        f'  instance_type          = "{instance_type}"',
        f"  update_default_version = true" + provider,
    ]
    security_group_ids = decl.attrs.get("security_group_ids")
    if security_group_ids:
        ids = ", ".join(f'"{s}"' for s in security_group_ids)
        lines.append(f"  vpc_security_group_ids = [{ids}]")
    user_data_path = decl.attrs.get("user_data_path")
    if user_data_path:
        lines.append(f'  user_data              = filebase64("{user_data_path}")')
    if role_address:
        lines.append("")
        lines.append("  iam_instance_profile {")
        lines.append(f"    arn = aws_iam_instance_profile.{role_address}.arn")
        lines.append("  }")
    lines.append("")
    lines.append("  metadata_options {")
    lines.append('    http_endpoint               = "enabled"')
    lines.append("    http_put_response_hop_limit = 2")
    lines.append('    http_tokens                 = "required"')
    lines.append("  }")
    lines.append("")
    for resource_type in ("instance", "volume", "network-interface"):
        lines.append("  tag_specifications {")
        lines.append(f'    resource_type = "{resource_type}"')
        lines.append(f'    tags          = {{ "Purpose" = "{aws_name}" }}')
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


def _emit_autoscaling_group(decl: ResourceDecl, primary_region: str | None) -> str:
    aws_name = decl.attrs.get("name", decl.name)
    min_size = decl.attrs.get("min_size", _DEFAULT_MIN_SIZE)
    max_size = decl.attrs.get("max_size", _DEFAULT_MAX_SIZE)
    desired_capacity = decl.attrs.get("desired_capacity", _DEFAULT_DESIRED_CAPACITY)
    subnet_ids = decl.attrs.get("subnet_ids")
    vpc_zone_identifier = (
        "[" + ", ".join(f'"{s}"' for s in subnet_ids) + "]" if subnet_ids else "var.subnet_ids"
    )
    provider = _provider_meta(decl, primary_region)
    return (
        f'resource "aws_autoscaling_group" "{decl.name}" {{\n'
        f'  name                = "{aws_name}"\n'
        f"  min_size            = {min_size}\n"
        f"  max_size            = {max_size}\n"
        f"  desired_capacity    = {desired_capacity}\n"
        f'  health_check_type   = "EC2"\n'
        f"  vpc_zone_identifier = {vpc_zone_identifier}{provider}\n\n"
        f"  launch_template {{\n"
        f"    name    = aws_launch_template.{decl.name}.name\n"
        f'    version = "$Latest"\n'
        f"  }}\n"
        f"}}"
    )


def _emit_bucket_replication(
    source: ResourceDecl,
    destinations: list[ResourceDecl],
    role_address: str,
    role_name: str,
    policy_name: str,
    rule_ids: dict[str, str],
) -> list[str]:
    trust = _emit_assume_role_trust("s3.amazonaws.com")
    blocks = [
        f'resource "aws_iam_role" "{role_address}" {{\n'
        f'  name = "{role_name}"\n\n'
        f"  assume_role_policy = {trust}\n"
        f"}}"
    ]
    dest_object_arns = ",\n".join(f'          "${{aws_s3_bucket.{d.name}.arn}}/*"' for d in destinations)
    blocks.append(
        f'resource "aws_iam_role_policy" "{role_address}_policy" {{\n'
        f'  name = "{policy_name}"\n'
        f"  role = aws_iam_role.{role_address}.id\n\n"
        f"  policy = jsonencode({{\n"
        f'    Version = "2012-10-17"\n'
        f"    Statement = [\n"
        f"      {{\n"
        f'        Effect   = "Allow"\n'
        f'        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]\n'
        f"        Resource = aws_s3_bucket.{source.name}.arn\n"
        f"      }},\n"
        f"      {{\n"
        f'        Effect = "Allow"\n'
        f"        Action = [\n"
        f'          "s3:GetObjectVersionForReplication",\n'
        f'          "s3:GetObjectVersionAcl",\n'
        f'          "s3:GetObjectVersionTagging",\n'
        f"        ]\n"
        f'        Resource = "${{aws_s3_bucket.{source.name}.arn}}/*"\n'
        f"      }},\n"
        f"      {{\n"
        f'        Effect = "Allow"\n'
        f"        Action = [\n"
        f'          "s3:ReplicateObject",\n'
        f'          "s3:ReplicateDelete",\n'
        f'          "s3:ReplicateTags",\n'
        f"        ]\n"
        f"        Resource = [\n{dest_object_arns}\n        ]\n"
        f"      }},\n"
        f"    ]\n"
        f"  }})\n"
        f"}}"
    )
    rules = []
    for dest in destinations:
        rule_id = rule_ids.get(dest.name, f"replicate-to-{dest.name}")
        rules.append(
            f"  rule {{\n"
            f'    id     = "{rule_id}"\n'
            f'    status = "Enabled"\n\n'
            f"    destination {{\n"
            f"      bucket        = aws_s3_bucket.{dest.name}.arn\n"
            f'      storage_class = "STANDARD"\n'
            f"    }}\n"
            f"  }}"
        )
    blocks.append(
        f'resource "aws_s3_bucket_replication_configuration" "{source.name}" {{\n'
        f"  depends_on = [aws_s3_bucket_versioning.{source.name}]\n\n"
        f"  role   = aws_iam_role.{role_address}.arn\n"
        f"  bucket = aws_s3_bucket.{source.name}.id\n\n"
        + "\n\n".join(rules)
        + "\n}"
    )
    return blocks


def generate(program: Program) -> str:
    for decl in program.resources:
        if decl.type not in SUPPORTED_TYPES:
            raise CodegenError(
                f"line {decl.line}: unsupported resource type '{decl.type}' "
                f"(supported: {', '.join(sorted(SUPPORTED_TYPES))})"
            )

    resolve = _make_resolver(program)
    needs_account_id = False
    primary_region = _primary_region(program)

    # Resolve every relation up front.
    resolved: list[tuple[ResourceDecl, str, list[ResourceDecl], RelationStmt]] = []
    for stmt in program.relations:
        left = resolve(stmt.left)
        targets = [resolve(r) for r in stmt.right]
        target_types = {t.type for t in targets}
        if len(target_types) != 1:
            raise CodegenError(f"line {stmt.line}: all targets of one relation must be the same type")
        target_type = next(iter(target_types))
        key = (left.type, stmt.verb, target_type)
        if key not in {
            ("ScaledServer", "reads", "Bucket"),
            ("ScaledServer", "writes", "Bucket"),
            ("ScaledServer", "reads", "Secret"),
            ("ScaledServer", "reads", "Registry"),
            ("Bucket", "replicates", "Bucket"),
        }:
            raise CodegenError(
                f"line {stmt.line}: '{stmt.verb}' from {left.type} to {target_type} is not supported"
            )
        allowed_attrs = {"name", "role_name", "rule_id"} if key == ("Bucket", "replicates", "Bucket") else {"name"}
        unknown_attrs = set(stmt.attrs) - allowed_attrs
        if unknown_attrs:
            raise CodegenError(f"line {stmt.line}: unknown relation attribute(s): {', '.join(unknown_attrs)}")
        if target_type in ("Registry", "Secret"):
            needs_account_id = True
        resolved.append((left, stmt.verb, targets, stmt))

    # Figure out which ScaledServers need a role, and the shared role address/name per server.
    role_of_server: dict[str, tuple[str, str]] = {}
    for left, verb, targets, stmt in resolved:
        if left.type == "ScaledServer":
            role_of_server[left.name] = _role_address_and_name(left)

    # A ScaledServer with no relation of its own still shares a role if its
    # role_name matches one already in use by a server that does have one
    # (e.g. a second region's server reusing the same global identity).
    aws_name_to_role = {aws_name: (address, aws_name) for address, aws_name in role_of_server.values()}
    for decl in program.resources:
        if decl.type != "ScaledServer" or decl.name in role_of_server:
            continue
        role_name = decl.attrs.get("role_name")
        if role_name and role_name in aws_name_to_role:
            role_of_server[decl.name] = aws_name_to_role[role_name]

    account_id_expr = "data.aws_caller_identity.current.account_id"
    if role_of_server:
        needs_account_id = True

    blocks: list[str] = []

    regions = _collect_regions(program, primary_region)
    for region in regions:
        blocks.append(_emit_provider_alias(region))

    if needs_account_id:
        blocks.append('data "aws_caller_identity" "current" {}')

    for decl in program.resources:
        if decl.type == "Bucket":
            blocks.extend(_emit_bucket(decl, primary_region))
        elif decl.type == "Registry":
            blocks.extend(_emit_registry(decl, account_id_expr, primary_region))
        elif decl.type == "Secret":
            blocks.extend(_emit_secret(decl))

    # One role/instance-profile/SSM-core/monitoring set per distinct role address.
    emitted_roles: set[str] = set()
    for decl in program.resources:
        if decl.type != "ScaledServer" or decl.name not in role_of_server:
            continue
        address, aws_name = role_of_server[decl.name]
        if address in emitted_roles:
            continue
        emitted_roles.add(address)
        server_regions = sorted(
            {
                _require_region(d, primary_region)
                for d in program.resources
                if d.type == "ScaledServer" and role_of_server.get(d.name, (None, None))[0] == address
            }
        )
        blocks.append(_emit_iam_role(address, aws_name, decl.attrs.get("role_description")))
        blocks.append(_emit_iam_instance_profile(address, aws_name))
        blocks.append(_emit_ssm_core_attachment(address))
        blocks.append(
            _emit_monitoring_policy(address, decl.attrs.get("log_group", decl.name), server_regions, account_id_expr)
        )

    policy_names_used: set[str] = set()
    for left, verb, targets, stmt in resolved:
        if left.type != "ScaledServer":
            continue
        role_address, _ = role_of_server[left.name]
        default_name = f"{left.name}-{verb}-{'-'.join(t.name for t in targets)}"
        policy_name = stmt.attrs.get("name", default_name)
        if policy_name in policy_names_used:
            raise CodegenError(f"line {stmt.line}: duplicate policy name '{policy_name}'")
        policy_names_used.add(policy_name)

        if verb in ("reads", "writes") and targets[0].type == "Bucket":
            actions = _ACTIONS[("Bucket", verb)]
            blocks.append(_emit_relation_policy(role_address, policy_name, actions, _bucket_arns(targets)))
        elif verb == "reads" and targets[0].type == "Secret":
            actions = _ACTIONS[("Secret", "reads")]
            arns = [_secret_arn(t, account_id_expr, primary_region) for t in targets]
            blocks.append(_emit_relation_policy(role_address, policy_name, actions, arns))
        elif verb == "reads" and targets[0].type == "Registry":
            arns: list[str] = []
            for t in targets:
                arns.extend(_repo_arns(t, account_id_expr, primary_region))
            blocks.append(_emit_ecr_auth_policy(role_address, policy_name, arns))

    # Bucket -> Bucket replication, grouped by source.
    replications: dict[str, list[ResourceDecl]] = defaultdict(list)
    replication_overrides: dict[str, dict[str, Any]] = {}
    rule_ids_by_source: dict[str, dict[str, str]] = defaultdict(dict)
    for left, verb, targets, stmt in resolved:
        if left.type == "Bucket" and verb == "replicates":
            replications[left.name].extend(targets)
            overrides = replication_overrides.setdefault(left.name, {})
            if "role_name" in stmt.attrs:
                overrides["role_name"] = stmt.attrs["role_name"]
            if "name" in stmt.attrs:
                overrides["policy_name"] = stmt.attrs["name"]
            if "rule_id" in stmt.attrs:
                for t in targets:
                    rule_ids_by_source[left.name][t.name] = stmt.attrs["rule_id"]
    for source_name, destinations in replications.items():
        source = resolve(source_name)
        role_address = _sanitize(f"{source.name}-replication")
        overrides = replication_overrides.get(source_name, {})
        role_name = overrides.get("role_name", role_address)
        policy_name = overrides.get("policy_name", role_address)
        blocks.extend(
            _emit_bucket_replication(
                source, destinations, role_address, role_name, policy_name, rule_ids_by_source[source_name]
            )
        )

    for decl in program.resources:
        if decl.type == "ScaledServer":
            role_address = role_of_server.get(decl.name, (None, None))[0]
            blocks.append(_emit_launch_template(decl, role_address, primary_region))
            blocks.append(_emit_autoscaling_group(decl, primary_region))

    header = "# --- generated by infrarig (experimental, do not apply as-is) ---\n"
    return header + "\n\n".join(blocks) + "\n"

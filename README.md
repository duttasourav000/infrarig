# infrarig (experimental)

A tiny DSL that compiles to OpenTofu/Terraform HCL. The idea: describe
infrastructure as resources and the relationships between them (e.g. "this
server reads that bucket"), and let the compiler generate the IAM plumbing
(roles, instance profiles, scoped policies) instead of writing it by hand.

This is a standalone experiment. The output is plain HCL printed to stdout
(or a file); nothing here is wired into `tofu plan`/`apply`, and the
generated header says as much: review before applying.

## Status

Four resource types and three verbs. Codegen only targets AWS, but the type
names are meant to stay cloud-agnostic.

| Type           | Compiles to                                                            |
| -------------- | ---------------------------------------------------------------------- |
| `ScaledServer` | `aws_launch_template` + `aws_autoscaling_group` (not a single instance) |
| `Bucket`       | `aws_s3_bucket` + versioning + a lifecycle rule that expires old versions |
| `Registry`     | `aws_ecr_repository` (+ `aws_ecr_replication_configuration` if replicated) |
| `Secret`       | `aws_secretsmanager_secret` + version if `value` is set; otherwise nothing, just the IAM grant |

Supported relations (anything else is a compile error):

| Relation                          | Grants                                                       |
| --------------------------------- | ------------------------------------------------------------ |
| `ScaledServer reads Bucket`       | `s3:GetObject` on the bucket's objects                       |
| `ScaledServer writes Bucket`      | `s3:PutObject` on the bucket's objects                       |
| `ScaledServer reads Secret`       | `secretsmanager:GetSecretValue`                              |
| `ScaledServer reads Registry`     | `ecr:GetAuthorizationToken` + image pull actions             |
| `Bucket replicates Bucket`        | S3 replication role, policy and `aws_s3_bucket_replication_configuration` |

## Syntax

```
ScaledServer app {
  ami           = "ami-0123456789abcdef0"
  instance_type = "t2.nano"
  region        = "us-east-1"
}

Bucket uploads {
  bucket = "example-app.user"
}

ScaledServer reads Bucket
ScaledServer writes Bucket
```

- `<Type> <name> { attrs }` declares a resource. The block is optional.
  Values are strings, integers, `true`/`false`, or lists (`[...]`). Commas
  between attributes are optional, and `#` starts a comment.
- `<ref> <verb> <target> { attrs }` declares a relationship. A ref is a
  declared resource name, or a bare type keyword (`ScaledServer`,
  `Bucket`, ...) when there's exactly one resource of that type. That's
  why `ScaledServer reads Bucket` works as written when there's only one
  of each.
- A target can be a list: `app reads [uploads, logs]`. All targets in a
  list must be the same type, and they share one generated policy.
- Each relation generates its own scoped IAM policy. Need read and write?
  Write both statements (see [examples/basic.rig](examples/basic.rig)).

### Resource attributes

- **`ScaledServer`**
  - `ami` (required; there's no neutral default AMI across accounts/regions)
  - `instance_type` (default `t2.nano`), `min_size` / `max_size` /
    `desired_capacity` (default `1`)
  - `subnet_ids`: list. Falls back to `var.subnet_ids`, since networking
    isn't modeled yet.
  - `security_group_ids`: list
  - `user_data_path`: wired in via `filebase64(...)`
  - `name`: AWS name for the launch template and ASG (default: the
    resource name)
  - `role_name`: IAM role name (default `<name>-role`). Servers that share
    a `role_name` share one role and instance profile, even across regions.
  - `role_description`, `log_group` (default: the resource name)
- **`Bucket`**: `bucket` (required; the bucket name)
- **`Registry`**: `repository` (required), `replica_region` (adds an ECR
  replication rule to that region). `replica_region` doesn't add the
  replica's ARN to `reads` grants. To grant pulls from the replica, declare
  it as a second `Registry` and list both in the target.
- **`Secret`**: `name` (required). Optional `value` is a raw HCL
  expression, not a literal, e.g. `value = "jsonencode(var.params)"`. It
  keeps real values out of the DSL source. Without `value`, the secret is
  assumed to be managed elsewhere. Its grant then uses AWS's
  `<name>-??????` ARN pattern, to cover the random suffix AWS adds.

### Relation attributes

- `name`: the generated policy's name (default
  `<server>-<verb>-<targets>`). Duplicate names are an error.
- For `replicates` only: `role_name` (default `<source>-replication`) and
  `rule_id` (default `replicate-to-<dest>`).

### Regions

Any resource can set `region`. There's no hardcoded default. Whichever
region is declared first, in source order, becomes the primary region and
uses the root (unaliased) provider. Every other region, including
`Registry.replica_region`, gets an auto-generated
aliased `provider "aws"` block. A program that never declares a region is
valid only if it doesn't use `Secret`/`Registry`, because those
need a region to build an ARN.

## What a server's IAM role includes

Any `ScaledServer` with at least one relation, or one that shares a
`role_name` with such a server, gets:

- `aws_iam_role` with a trust policy for `ec2.amazonaws.com`
- `aws_iam_instance_profile`, attached to the launch template
- the `AmazonSSMManagedInstanceCore` managed policy
- a `Monitoring` policy: `cloudwatch:PutMetricData`, plus CloudWatch Logs
  access to `log_group` in each region the role is used in
- one scoped policy per relation (see the table above)

A `ScaledServer` with no relations still gets a launch template and ASG,
just with no IAM resources. Every launch template requires IMDSv2.

## Running it

The package is imported as `infrarig`, so run these from the directory
that contains the `infrarig/` checkout:

```bash
python3 -m infrarig.dsl.cli infrarig/examples/basic.rig
python3 -m infrarig.dsl.cli infrarig/examples/named.rig -o /tmp/named.tf
```

Errors from lexing, parsing or codegen go to stderr with a non-zero exit code.

Run the test suite (same directory):

```bash
python3 -m unittest discover -s infrarig/tests -t .
```

### Building the `rig` executable

[tools/build.sh](tools/build.sh) packages the compiler into one
self-contained executable, `rig`. It's a Python zipapp: a single
file that needs only `python3` to run and works from any directory. Run
these from inside the checkout:

```bash
tools/build.sh
output/rig examples/basic.rig
output/rig examples/named.rig -o /tmp/named.tf
```

By default the executable is written to `output/rig` in the checkout
(git-ignored). To write it somewhere else, pass an output directory. It's
created if missing, and a relative path is relative to your current
directory:

```bash
tools/build.sh ~/.local/bin      # writes ~/.local/bin/rig
```

`rig` takes the same arguments as the module. Rebuild after changing code
under `dsl/`, because the executable holds a copy of the source.

To run `rig` from anywhere, build it straight into a directory on your
`PATH`, as in the `~/.local/bin` example above, then:

```bash
rig path/to/program.rig
```

If `~/.local/bin` isn't on your `PATH` yet, add
`export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`.

## Examples

- [examples/basic.rig](examples/basic.rig): one server reading and
  writing one bucket, using bare type refs.
- [examples/named.rig](examples/named.rig): multiple resources of the
  same type, referenced by name.

The tests in [tests/test_codegen.py](tests/test_codegen.py) cover the
other types: multi-region, shared roles, `Registry`, `Secret` and
replication.

## Layout

```
infrarig/
  dsl/
    lexer.py       # source -> tokens
    parser.py      # tokens -> AST (ResourceDecl / RelationStmt)
    ast_nodes.py   # AST dataclasses
    codegen.py     # AST -> HCL string
    cli.py         # `python3 -m infrarig.dsl.cli <file.rig>`
  tools/
    build.sh       # builds <output-dir>/rig, default output/rig
  examples/        # sample .rig programs
  tests/           # unittest suite for lexer/parser/codegen
```

## Known limitations

- No map literals, so `Registry` tags can't be set from the DSL yet
  (they default to `{ Purpose = <name> }`).
- The `Monitoring` policy's `PutLogEvents` grant is hardcoded to a log
  stream named `backend`.
- A `Secret` with `value` is always created in the primary region.

## Next steps (not yet built)

- More resource types (IAM users/groups, queues, databases, CDNs...)
- More verbs (e.g. `exposes`, for inbound traffic)
- Modeling networking (VPC/subnets) instead of falling back to `var.subnet_ids`
- A real HCL writer instead of hand-built string templates
- A second backend (e.g. GCP) to actually prove out the cloud-agnostic naming

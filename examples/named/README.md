# named

Two servers and two buckets, referenced by name.

From the repo root (assumes `output/rig` is built, see [tools/build.sh](../../tools/build.sh)):

```bash
output/rig examples/named/named.rig -o examples/named/generated.tf
cd examples/named
tofu init
tofu plan
```

[main.tf](main.tf) supplies the provider and `subnet_ids`, which the generated
HCL doesn't declare. Replace its placeholder values before planning.

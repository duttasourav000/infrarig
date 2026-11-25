# basic

One auto-scaled server reading and writing one bucket, using bare type refs.

From the repo root (assumes `output/rig` is built, see [tools/build.sh](../../tools/build.sh)):

```bash
output/rig examples/basic/basic.rig -o examples/basic/generated.tf
cd examples/basic
tofu init
tofu plan
```

[main.tf](main.tf) supplies the provider and `subnet_ids`, which the generated
HCL doesn't declare. Replace its placeholder values before planning.

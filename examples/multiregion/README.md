# multiregion

An app and its bucket, declared once for `us-east-1` and copied to
`us-west-2` with a `Component`:

```
Component web [app, uploads]

web mirrors "us-west-2" as usw2 {
  app { ami = "...", subnet_ids = ["..."] }
}
```

This generates:

- `app` + `uploads` in us-east-1, exactly as in a single-region program
- `web_usw2_app` (launch template + ASG) in us-west-2, sharing `app`'s IAM
  role
- `web_usw2_uploads` in us-west-2, named `example-app.uploads-us-west-2`
- bidirectional replication between `uploads` and `web_usw2_uploads`
- `web_usw2_app` reading and writing `web_usw2_uploads`, its own region's
  bucket

Without `as usw2`, the copies are named after the region:
`web_us_west_2_app` and so on. To add a third region, add another
`web mirrors "<region>"` statement. Every bucket
copy then replicates to every other one.

From the repo root (assumes `output/rig` is built, see [tools/build.sh](../../tools/build.sh)):

```bash
output/rig examples/multiregion/multiregion.rig -o examples/multiregion/generated.tf
cd examples/multiregion
tofu init
tofu plan
```

[main.tf](main.tf) supplies the root provider. Before planning, replace the
placeholder AMIs, subnet IDs and bucket name in
[multiregion.rig](multiregion.rig). AMIs and subnets are region-specific, and
bucket names must be globally unique.

## Checking the mirror

After `tofu apply`, write an object into one bucket and read it back from the
other. Replication is asynchronous, so allow up to a few minutes:

```bash
echo hello > /tmp/hello.txt
aws s3 cp /tmp/hello.txt s3://example-app.uploads/hello.txt
aws s3api head-object --bucket example-app.uploads-us-west-2 --key hello.txt \
  --query ReplicationStatus   # "REPLICA" once it has arrived

# and the other direction
aws s3 cp /tmp/hello.txt s3://example-app.uploads-us-west-2/hello2.txt
aws s3api head-object --bucket example-app.uploads --key hello2.txt \
  --query ReplicationStatus
```

Replication only covers objects written after the config exists. Objects
already in a bucket aren't copied over.

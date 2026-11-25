import unittest
from pathlib import Path

from infrarig.dsl.codegen import CodegenError, generate
from infrarig.dsl.parser import parse

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class CodegenTests(unittest.TestCase):
    def test_basic_example_generates_iam_s3_and_asg(self):
        program = parse((EXAMPLES / "basic" / "basic.rig").read_text())
        output = generate(program)

        self.assertIn('resource "aws_s3_bucket" "uploads"', output)
        self.assertIn('bucket = "example-app.user"', output)
        self.assertIn('resource "aws_s3_bucket_versioning" "uploads"', output)
        self.assertIn('resource "aws_iam_role" "app_role"', output)
        self.assertIn('resource "aws_iam_instance_profile" "app_role"', output)
        self.assertIn('resource "aws_iam_role_policy_attachment" "app_role_ssm_core"', output)
        self.assertIn('resource "aws_iam_role_policy" "app_role_monitoring"', output)
        self.assertIn("s3:GetObject", output)
        self.assertIn("s3:PutObject", output)
        self.assertIn("aws_s3_bucket.uploads.arn", output)
        self.assertIn('resource "aws_launch_template" "app"', output)
        self.assertIn("arn = aws_iam_instance_profile.app_role.arn", output)
        self.assertIn('resource "aws_autoscaling_group" "app"', output)
        self.assertIn("aws_launch_template.app.name", output)
        self.assertNotIn("aws_instance", output)

    def test_multiregion_example_mirrors_component_across_regions(self):
        output = generate(parse((EXAMPLES / "multiregion" / "multiregion.rig").read_text()))

        self.assertIn('provider "aws" {\n  alias  = "us_west_2"', output)
        # The server and its copy share one role.
        self.assertEqual(output.count('resource "aws_iam_role" "app_role"'), 1)
        self.assertIn('resource "aws_autoscaling_group" "web_usw2_app"', output)
        self.assertIn('bucket = "example-app.uploads-us-west-2"', output)
        # One replication config per direction, each in its source bucket's region
        # and waiting on versioning for both buckets.
        use1 = output.split('resource "aws_s3_bucket_replication_configuration" "uploads"')[1].split("rule {")[0]
        usw2 = output.split('resource "aws_s3_bucket_replication_configuration" "web_usw2_uploads"')[1]
        usw2 = usw2.split("rule {")[0]
        self.assertNotIn("provider", use1)
        self.assertIn("provider = aws.us_west_2", usw2)
        for config in (use1, usw2):
            self.assertRegex(config, r"aws_s3_bucket_versioning\.uploads\b")
            self.assertIn("aws_s3_bucket_versioning.web_usw2_uploads", config)
        self.assertNotIn("var.subnet_ids", output)

    def test_reads_does_not_grant_write_or_list(self):
        source = """
        ScaledServer app { ami = "ami-123", region = "ap-southeast-1" }
        Bucket uploads { bucket = "b" }
        app reads uploads
        """
        output = generate(parse(source))
        start = output.index('resource "aws_iam_role_policy" "app-reads-uploads"')
        end = output.index("\n}\n", start)
        self.assertIn("s3:GetObject", output[start:end])
        self.assertNotIn("s3:PutObject", output[start:end])
        self.assertNotIn("s3:ListBucket", output[start:end])

    def test_scaled_server_without_relation_has_no_iam_resources(self):
        program = parse('ScaledServer app {\n  ami = "ami-123"\n  instance_type = "t2.nano"\n}')
        output = generate(program)
        self.assertNotIn("aws_iam_role", output)
        self.assertIn('resource "aws_launch_template" "app"', output)
        self.assertIn('resource "aws_autoscaling_group" "app"', output)

    def test_shared_role_name_deduplicates_role(self):
        source = """
        ScaledServer web { role_name = "Shared", ami = "ami-123", region = "ap-southeast-1" }
        ScaledServer worker { role_name = "Shared", ami = "ami-123" }
        Bucket uploads { bucket = "b" }
        web reads uploads
        worker writes uploads
        """
        output = generate(parse(source))
        self.assertEqual(output.count('resource "aws_iam_role" "shared"'), 1)
        self.assertIn("arn = aws_iam_instance_profile.shared.arn", output)

    def test_shared_role_propagates_to_server_without_its_own_relation(self):
        source = """
        ScaledServer app { role_name = "EC2.example.com", ami = "ami-123", region = "us-east-1" }
        ScaledServer app_uw2 { role_name = "EC2.example.com", ami = "ami-123", region = "us-west-2" }
        Bucket uploads { bucket = "b" }
        app reads uploads
        """
        output = generate(parse(source))
        self.assertEqual(output.count('resource "aws_iam_role" "ec2_example_com"'), 1)
        start = output.index('resource "aws_launch_template" "app_uw2"')
        end = output.index("\n}\n", start)
        self.assertIn("arn = aws_iam_instance_profile.ec2_example_com.arn", output[start:end])

    def test_target_list_combines_into_one_policy(self):
        source = """
        ScaledServer app { ami = "ami-123", region = "ap-southeast-1" }
        Bucket uploads { bucket = "example.com.user" }
        Bucket uploads_uw2 { bucket = "example.com.user-uw2" }
        app reads [uploads, uploads_uw2] { name = "Data" }
        """
        output = generate(parse(source))
        start = output.index('resource "aws_iam_role_policy" "Data"')
        end = output.index("\n}\n", start)
        block = output[start:end]
        self.assertIn("aws_s3_bucket.uploads.arn", block)
        self.assertIn("aws_s3_bucket.uploads_uw2.arn", block)

    def test_reads_registry_generates_ecr_policy(self):
        source = """
        ScaledServer app { ami = "ami-123" }
        Registry backend { repository = "example.com/backend", region = "ap-southeast-1" }
        app reads backend
        """
        output = generate(parse(source))
        self.assertIn('resource "aws_ecr_repository" "backend"', output)
        self.assertIn("ecr:GetAuthorizationToken", output)
        self.assertIn("ecr:BatchGetImage", output)
        self.assertIn("arn:aws:ecr:ap-southeast-1:${data.aws_caller_identity.current.account_id}:repository/example.com/backend", output)

    def test_registry_replica_region_emits_replication_config(self):
        source = """
        ScaledServer app { ami = "ami-123" }
        Registry backend {
          repository = "example.com/backend"
          region = "ap-southeast-1"
          replica_region = "us-west-2"
        }
        app reads backend
        """
        output = generate(parse(source))
        self.assertIn('resource "aws_ecr_replication_configuration" "backend"', output)
        self.assertIn('region      = "us-west-2"', output)
        self.assertIn('provider "aws" {\n  alias  = "us_west_2"', output)

    def test_reads_target_list_combines_both_registry_arns(self):
        source = """
        ScaledServer app { ami = "ami-123" }
        Registry backend { repository = "example.com/backend", region = "ap-southeast-1" }
        Registry backend_uw2 { repository = "example.com/backend", region = "us-west-2" }
        app reads [backend, backend_uw2]
        """
        output = generate(parse(source))
        self.assertIn("arn:aws:ecr:ap-southeast-1:", output)
        self.assertIn("arn:aws:ecr:us-west-2:", output)
        self.assertEqual(output.count("repository/example.com/backend\""), 2)

    def test_reads_secret_generates_secretsmanager_policy(self):
        source = """
        ScaledServer app { ami = "ami-123", region = "ap-southeast-1" }
        Secret params { name = "example.com" }
        app reads params
        """
        output = generate(parse(source))
        self.assertIn("secretsmanager:GetSecretValue", output)
        self.assertIn("arn:aws:secretsmanager:ap-southeast-1:", output)
        self.assertIn(":secret:example.com-??????", output)
        self.assertNotIn('resource "aws_secretsmanager_secret"', output)

    def test_secret_with_value_creates_resource_and_exact_arn(self):
        source = """
        ScaledServer app { ami = "ami-123", region = "ap-southeast-1" }
        Secret params {
          name  = "example.com"
          value = "jsonencode(merge(var.ssm_params, { REGION = \\"ap-southeast-1\\" }))"
        }
        app reads params
        """
        output = generate(parse(source))
        self.assertIn('resource "aws_secretsmanager_secret" "params"', output)
        self.assertIn('resource "aws_secretsmanager_secret_version" "params"', output)
        self.assertIn(
            'secret_string = jsonencode(merge(var.ssm_params, { REGION = "ap-southeast-1" }))', output
        )
        self.assertIn("Resource = [\n        aws_secretsmanager_secret.params.arn\n", output)
        self.assertNotIn("??????", output)

    def test_secret_missing_name_raises(self):
        source = """
        ScaledServer app { ami = "ami-123", region = "ap-southeast-1" }
        Secret params {}
        app reads params
        """
        with self.assertRaisesRegex(CodegenError, "missing a 'name' attribute"):
            generate(parse(source))

    def test_bucket_replicates_bucket(self):
        source = """
        Bucket uploads { bucket = "example.com.user", region = "ap-southeast-1" }
        Bucket uploads_uw2 { bucket = "example.com.user-uw2", region = "us-west-2" }
        uploads replicates uploads_uw2
        """
        output = generate(parse(source))
        self.assertIn('resource "aws_s3_bucket_replication_configuration" "uploads"', output)
        self.assertIn("s3:ReplicateObject", output)
        self.assertIn('provider "aws" {\n  alias  = "us_west_2"', output)

    def test_replication_config_uses_source_bucket_provider(self):
        source = """
        Bucket uploads { bucket = "example.com.user", region = "ap-southeast-1" }
        Bucket uploads_uw2 { bucket = "example.com.user-uw2", region = "us-west-2" }
        uploads_uw2 replicates uploads
        """
        output = generate(parse(source))
        config = output.split('resource "aws_s3_bucket_replication_configuration" "uploads_uw2"')[1]
        self.assertIn("provider = aws.us_west_2", config.split("rule {")[0])

    def test_bucket_mirrors_bucket_replicates_both_ways(self):
        source = """
        Bucket uploads1 { bucket = "example.com.user", region = "ap-southeast-1" }
        Bucket uploads2 { bucket = "example.com.user-uw2", region = "us-west-2" }
        uploads1 mirrors uploads2
        """
        output = generate(parse(source))
        self.assertIn('resource "aws_s3_bucket_replication_configuration" "uploads1"', output)
        self.assertIn('resource "aws_s3_bucket_replication_configuration" "uploads2"', output)
        self.assertIn('resource "aws_iam_role" "uploads1_replication"', output)
        self.assertIn('resource "aws_iam_role" "uploads2_replication"', output)
        self.assertIn('id     = "replicate-to-uploads2"', output)
        self.assertIn('id     = "replicate-to-uploads1"', output)

    def test_mirror_overlapping_replicates_raises(self):
        source = """
        Bucket a { bucket = "a", region = "us-east-1" }
        Bucket b { bucket = "b", region = "us-west-2" }
        a mirrors b
        b replicates a
        """
        with self.assertRaisesRegex(CodegenError, "already replicates"):
            generate(parse(source))

    def test_bucket_mirrors_itself_raises(self):
        source = """
        Bucket a { bucket = "a", region = "us-east-1" }
        a mirrors a
        """
        with self.assertRaisesRegex(CodegenError, "itself"):
            generate(parse(source))

    def test_mirror_rejects_attrs(self):
        source = """
        Bucket a { bucket = "a", region = "us-east-1" }
        Bucket b { bucket = "b", region = "us-west-2" }
        a mirrors b { role_name = "x" }
        """
        with self.assertRaises(CodegenError):
            generate(parse(source))

    def test_mismatched_target_types_raises(self):
        source = """
        ScaledServer app
        Bucket uploads { bucket = "b" }
        Secret params { name = "p" }
        app reads [uploads, params]
        """
        with self.assertRaises(CodegenError):
            generate(parse(source))

    def test_unknown_relation_attr_raises(self):
        source = """
        ScaledServer app
        Bucket uploads { bucket = "b" }
        app reads uploads { actions = ["read"] }
        """
        with self.assertRaises(CodegenError):
            generate(parse(source))

    def test_ambiguous_bare_type_reference_raises(self):
        source = """
        ScaledServer web
        ScaledServer worker
        Bucket uploads { bucket = "b" }
        ScaledServer reads Bucket
        """
        with self.assertRaises(CodegenError):
            generate(parse(source))

    def test_unsupported_relation_direction_raises(self):
        source = """
        ScaledServer app
        Bucket uploads { bucket = "b" }
        Bucket reads ScaledServer
        """
        with self.assertRaises(CodegenError):
            generate(parse(source))

    def test_missing_bucket_raises(self):
        with self.assertRaises(CodegenError):
            generate(parse("Bucket uploads"))

    def test_explicit_subnet_ids_used_in_asg(self):
        program = parse(
            'ScaledServer app {\n  ami = "ami-123"\n  subnet_ids = ["subnet-aaa", "subnet-bbb"]\n}'
        )
        output = generate(program)
        self.assertIn('vpc_zone_identifier = ["subnet-aaa", "subnet-bbb"]', output)

    def test_default_vpc_zone_identifier_references_variable(self):
        program = parse('ScaledServer app { ami = "ami-123" }')
        output = generate(program)
        self.assertIn("vpc_zone_identifier = var.subnet_ids", output)

    def test_missing_ami_raises(self):
        with self.assertRaisesRegex(CodegenError, "missing an 'ami' attribute"):
            generate(parse("ScaledServer app"))

    def test_missing_region_raises_when_nothing_resolves_it(self):
        source = """
        ScaledServer app { ami = "ami-123" }
        Secret params { name = "x" }
        app reads params
        """
        with self.assertRaisesRegex(CodegenError, "needs a 'region'"):
            generate(parse(source))


if __name__ == "__main__":
    unittest.main()

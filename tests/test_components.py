import unittest

from infrarig.dsl.codegen import CodegenError, generate
from infrarig.dsl.components import expand_components
from infrarig.dsl.parser import parse

BASE = """
ScaledServer app {
  ami        = "ami-1"
  region     = "us-east-1"
  subnet_ids = ["subnet-1"]
}
Bucket uploads { bucket = "example.uploads", region = "us-east-1" }
app reads uploads
app writes uploads
Component web [app, uploads]
"""

USW2 = """
web mirrors "us-west-2" as usw2 {
  app { ami = "ami-2", subnet_ids = ["subnet-2"] }
}
"""

EUW1 = """
web mirrors "eu-west-1" {
  app { ami = "ami-3", subnet_ids = ["subnet-3"] }
}
"""


def expand(source):
    return expand_components(parse(source))


def relations(program):
    return {(r.left, r.verb, tuple(r.right)) for r in program.relations}


class ComponentTests(unittest.TestCase):
    def test_copies_members_with_overrides_and_defaults(self):
        program = expand(BASE + USW2)
        by_name = {r.name: r for r in program.resources}
        app = by_name["web_usw2_app"]
        self.assertEqual(app.attrs["region"], "us-west-2")
        self.assertEqual(app.attrs["ami"], "ami-2")
        self.assertEqual(app.attrs["subnet_ids"], ["subnet-2"])
        # Regional AWS names are kept; the IAM role is shared with the original.
        self.assertEqual(app.attrs["name"], "app")
        self.assertEqual(app.attrs["role_name"], "app-role")
        self.assertEqual(by_name["web_usw2_uploads"].attrs["bucket"], "example.uploads-us-west-2")
        # The original is untouched.
        self.assertEqual(by_name["app"].attrs["region"], "us-east-1")

    def test_copies_relations_and_mirrors_buckets(self):
        rels = relations(expand(BASE + USW2))
        self.assertIn(("web_usw2_app", "reads", ("web_usw2_uploads",)), rels)
        self.assertIn(("web_usw2_app", "writes", ("web_usw2_uploads",)), rels)
        self.assertIn(("uploads", "mirrors", ("web_usw2_uploads",)), rels)

    def test_three_regions_mirror_full_mesh(self):
        rels = relations(expand(BASE + USW2 + EUW1))
        mirrors = {(left, right[0]) for left, verb, right in rels if verb == "mirrors"}
        self.assertEqual(
            mirrors,
            {
                ("uploads", "web_usw2_uploads"),
                ("uploads", "web_eu_west_1_uploads"),
                ("web_usw2_uploads", "web_eu_west_1_uploads"),
            },
        )
        output = generate(parse(BASE + USW2 + EUW1))
        self.assertEqual(output.count('resource "aws_s3_bucket_replication_configuration"'), 3)
        self.assertEqual(output.count('resource "aws_iam_role" "app_role"'), 1)

    def test_bucket_override_wins_over_region_suffix(self):
        source = BASE + """
        web mirrors "us-west-2" {
          app { ami = "ami-2", subnet_ids = ["subnet-2"] }
          uploads { bucket = "custom-name" }
        }
        """
        by_name = {r.name: r for r in expand(source).resources}
        self.assertEqual(by_name["web_us_west_2_uploads"].attrs["bucket"], "custom-name")

    def test_dotted_refs_reach_copies_and_originals(self):
        source = BASE + USW2 + """
        Bucket logs { bucket = "logs", region = "us-east-1" }
        web.usw2.app writes logs
        web.app writes logs { name = "app-logs" }
        """
        rels = relations(expand(source))
        self.assertIn(("web_usw2_app", "writes", ("logs",)), rels)
        self.assertIn(("app", "writes", ("logs",)), rels)

    def test_relation_to_outside_resource_is_copied_and_name_suffixed(self):
        source = BASE + """
        Bucket logs { bucket = "logs", region = "us-east-1" }
        app writes logs { name = "app-logs" }
        """ + USW2
        program = expand(source)
        copied = [r for r in program.relations if r.left == "web_usw2_app" and r.right == ["logs"]]
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].attrs["name"], "app-logs-web_usw2")
        generate(parse(source))  # no duplicate policy name

    def test_relation_into_component_from_outside_is_not_copied(self):
        source = BASE + """
        ScaledServer admin { ami = "ami-9", region = "us-east-1" }
        admin reads uploads
        """ + USW2
        rels = relations(expand(source))
        self.assertNotIn(("admin", "reads", ("web_usw2_uploads",)), rels)

    def test_bare_type_refs_are_pinned_before_copying(self):
        source = """
        ScaledServer app { ami = "ami-1", region = "us-east-1", subnet_ids = ["s"] }
        Bucket uploads { bucket = "u", region = "us-east-1" }
        ScaledServer reads Bucket
        Component web [app, uploads]
        """ + USW2
        rels = relations(expand(source))
        self.assertIn(("app", "reads", ("uploads",)), rels)
        self.assertIn(("web_usw2_app", "reads", ("web_usw2_uploads",)), rels)

    def test_server_copy_requires_regional_overrides(self):
        source = BASE + 'web mirrors "us-west-2"'
        with self.assertRaisesRegex(CodegenError, "ami, subnet_ids"):
            expand(source)

    def test_security_groups_must_be_overridden_when_set(self):
        source = BASE.replace('subnet_ids = ["subnet-1"]', 'subnet_ids = ["subnet-1"]\n  security_group_ids = ["sg-1"]')
        with self.assertRaisesRegex(CodegenError, "security_group_ids"):
            expand(source + USW2)

    def test_region_label_names_copies_by_default(self):
        program = expand(BASE + 'web mirrors "us-west-2" { app { ami = "a", subnet_ids = ["s"] } }')
        names = {r.name for r in program.resources}
        self.assertTrue({"web_us_west_2_app", "web_us_west_2_uploads"} <= names)

    def test_default_label_dotted_ref(self):
        source = BASE + """
        Bucket logs { bucket = "logs", region = "us-east-1" }
        web mirrors "us-west-2" { app { ami = "a", subnet_ids = ["s"] } }
        web.us_west_2.app writes logs
        """
        self.assertIn(("web_us_west_2_app", "writes", ("logs",)), relations(expand(source)))

    def test_old_identifier_target_raises_with_hint(self):
        source = BASE + 'web mirrors web_usw2 { region = "us-west-2" }'
        with self.assertRaisesRegex(CodegenError, 'web mirrors "us-west-2"'):
            expand(source)

    def test_region_attr_in_mirror_block_raises(self):
        source = BASE + 'web mirrors "us-west-2" { region = "eu-west-1" }'
        with self.assertRaisesRegex(CodegenError, "region is the mirror's target"):
            expand(source)

    def test_same_region_twice_raises(self):
        source = BASE + USW2 + """
        web mirrors "us-west-2" as other {
          app { ami = "ami-2", subnet_ids = ["subnet-2"] }
        }
        """
        with self.assertRaisesRegex(CodegenError, "mirrored to us-west-2 more than once"):
            expand(source)

    def test_region_string_on_plain_relation_raises(self):
        with self.assertRaisesRegex(CodegenError, "only a Component"):
            expand(BASE + 'uploads mirrors "us-west-2"')

    def test_mirror_to_same_region_raises(self):
        source = BASE + """
        web mirrors "us-east-1" {
          app { ami = "ami-2", subnet_ids = ["subnet-2"] }
        }
        """
        with self.assertRaisesRegex(CodegenError, "already in us-east-1"):
            expand(source)

    def test_member_without_region_raises(self):
        source = """
        Bucket uploads { bucket = "u" }
        Component data [uploads]
        data mirrors "us-west-2"
        """
        with self.assertRaisesRegex(CodegenError, "needs its own 'region'"):
            expand(source)

    def test_unknown_member_override_raises(self):
        source = BASE + """
        web mirrors "us-west-2" {
          app { ami = "ami-2", subnet_ids = ["subnet-2"] }
          logs { bucket = "x" }
        }
        """
        with self.assertRaisesRegex(CodegenError, "'logs' is not a member"):
            expand(source)

    def test_unsupported_member_type_raises(self):
        source = """
        Secret params { name = "p", region = "us-east-1" }
        Component cfg [params]
        """
        with self.assertRaisesRegex(CodegenError, "can't hold Secret"):
            expand(source)

    def test_unknown_member_raises(self):
        with self.assertRaisesRegex(CodegenError, "not a declared resource"):
            expand("Component web [app]")

    def test_member_in_two_components_raises(self):
        with self.assertRaisesRegex(CodegenError, "already a member"):
            expand(BASE + "Component other [uploads]")

    def test_duplicate_instance_raises(self):
        with self.assertRaisesRegex(CodegenError, "more than once"):
            expand(BASE + USW2 + USW2)

    def test_component_only_supports_mirrors(self):
        with self.assertRaisesRegex(CodegenError, "only supports 'mirrors'"):
            expand(BASE + "web reads uploads")

    def test_nested_block_outside_component_mirror_raises(self):
        with self.assertRaisesRegex(CodegenError, "nested block"):
            expand('Bucket uploads { bucket = "u", tags { a = "b" } }')
        with self.assertRaisesRegex(CodegenError, "nested block"):
            expand(BASE + 'app reads uploads { x { a = "b" } }')


if __name__ == "__main__":
    unittest.main()

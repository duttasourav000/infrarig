import unittest

from infrarig.dsl.parser import ParseError, parse


class ParserTests(unittest.TestCase):
    def test_resource_decl_with_attrs(self):
        program = parse('Bucket uploads {\n  bucket = "example.com.user"\n}')
        self.assertEqual(len(program.resources), 1)
        decl = program.resources[0]
        self.assertEqual((decl.type, decl.name), ("Bucket", "uploads"))
        self.assertEqual(decl.attrs, {"bucket": "example.com.user"})

    def test_resource_decl_without_block(self):
        program = parse("ScaledServer app")
        self.assertEqual(program.resources[0].attrs, {})

    def test_reads_by_type(self):
        program = parse("ScaledServer reads Bucket")
        self.assertEqual(len(program.relations), 1)
        stmt = program.relations[0]
        self.assertEqual((stmt.left, stmt.verb, stmt.right), ("ScaledServer", "reads", ["Bucket"]))

    def test_writes_by_name(self):
        program = parse("web writes uploads")
        stmt = program.relations[0]
        self.assertEqual((stmt.left, stmt.verb, stmt.right), ("web", "writes", ["uploads"]))

    def test_target_list(self):
        program = parse("app reads [uploads, uploads_uw2]")
        stmt = program.relations[0]
        self.assertEqual(stmt.right, ["uploads", "uploads_uw2"])

    def test_relation_name_override(self):
        program = parse('app reads uploads {\n  name = "Data"\n}')
        stmt = program.relations[0]
        self.assertEqual(stmt.attrs, {"name": "Data"})

    def test_full_program(self):
        source = """
        ScaledServer app {
          instance_type = "t2.nano"
        }

        Bucket uploads {
          bucket = "example.com.user"
        }

        ScaledServer reads Bucket
        ScaledServer writes Bucket
        """
        program = parse(source)
        self.assertEqual(len(program.resources), 2)
        self.assertEqual(len(program.relations), 2)

    def test_component_decl(self):
        program = parse("Component web [app, uploads]")
        self.assertEqual(len(program.components), 1)
        self.assertEqual((program.components[0].name, program.components[0].members), ("web", ["app", "uploads"]))
        self.assertEqual(program.resources, [])

    def test_nested_block_parses_to_dict(self):
        program = parse('web mirrors "us-west-2" {\n  app { ami = "ami-1" }\n}')
        stmt = program.relations[0]
        self.assertEqual(stmt.attrs, {"app": {"ami": "ami-1"}})
        self.assertEqual((stmt.right, stmt.literal_target, stmt.alias), (["us-west-2"], True, None))

    def test_mirror_region_with_alias(self):
        stmt = parse('web mirrors "us-west-2" as usw2').relations[0]
        self.assertEqual((stmt.right, stmt.literal_target, stmt.alias), (["us-west-2"], True, "usw2"))

    def test_missing_equals_raises(self):
        with self.assertRaises(ParseError):
            parse('Bucket uploads {\n  bucket "x"\n}')


if __name__ == "__main__":
    unittest.main()

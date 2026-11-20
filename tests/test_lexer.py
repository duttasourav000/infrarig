import unittest

from infrarig.dsl.lexer import LexError, tokenize


class TokenizeTests(unittest.TestCase):
    def test_basic_tokens(self):
        kinds = [t.kind for t in tokenize('ec2 app { ami = "x" }')]
        self.assertEqual(
            kinds,
            ["IDENT", "IDENT", "LBRACE", "IDENT", "EQUALS", "STRING", "RBRACE", "EOF"],
        )

    def test_comments_and_whitespace_are_skipped(self):
        tokens = tokenize("# comment\n  ec2 app\n")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, ["IDENT", "IDENT", "EOF"])

    def test_list_value_tokens(self):
        kinds = [t.kind for t in tokenize('actions = ["read", "write"]')]
        self.assertEqual(
            kinds,
            ["IDENT", "EQUALS", "LBRACKET", "STRING", "COMMA", "STRING", "RBRACKET", "EOF"],
        )

    def test_unexpected_character_raises(self):
        with self.assertRaises(LexError):
            tokenize("ec2 app @")


if __name__ == "__main__":
    unittest.main()

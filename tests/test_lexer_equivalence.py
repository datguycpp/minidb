"""Two-path lexer equivalence: the fast tokenizer against the retained char-loop reference.

Why this file exists: `tokenize` consumes whitespace, identifiers and numbers as regex runs and
derives line/col from what it consumed, while `_tokenize_scanner` tracks positions character by
character. Run-then-derive is the design most likely to diverge on positions, and the failure it
produces (right tokens at wrong offsets) normalizes away downstream, so corpus and fuzz cannot see
it. The comparison is therefore made at the token layer, on identical inputs, over seven fields.

Contract:
* identity is asserted everywhere EXCEPT a closed, named exception set (`EXCEPTIONS`), each entry
  pinned to both sides' expected output -- fast path matches the oracle-verified text, reference
  keeps its pre-fix behaviour. Growth requires a spec amendment carrying oracle evidence.
* the fixture `tests/lexer_oddities.json` is checked against a live run first: an absolute anchor
  that has drifted from the engine is worse than none.
* canary (a): the comparison count must equal the declared part-sum, whose terms are known before
  any generator runs, and the executed counter is separate from the lists it walks -- an empty or
  truncated generator fails rather than passing trivially.
* canary (b): the comparator must flag a deliberately-different pair, proving it can fire.
"""

import glob
import json
import os
import random
import unittest

from minidb.sql.lexer import _tokenize_scanner, tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = 4242
RANDOM_COUNT = 3000

TRAPS = [
    "SELECT 'a\nb', col FROM t",
    "SELECT 1\n\nFROM t\n",
    "-- x\nSELECT 1",
    "SELECT 1 /* a\nb */ + 2",
    "  \n\t SELECT 1",
]

# Closed *rule*, not a growing list: 0399c54 made the fast path reject a number immediately
# followed by identifier characters, naming the whole run -- `1e`/`2e+`/`1e5e5`/`123abc` all go
# through this one rule (executed against SQLite 3.54.0 and 3.51.0). The reference is the pre-fix
# scanner, so any input containing such a run is expected to disagree.
import re

SANCTIONED = re.compile(r'unrecognized token: "(\.?\d[0-9A-Za-z_$.]*)"')


def sanctioned(fast):
    """True when the fast side's disagreement is the documented post-0399c54 rejection rule."""
    return (isinstance(fast, tuple) and fast[0] == "EXC" and fast[1] == "LexError"
            and SANCTIONED.fullmatch(fast[2]) is not None
            and re.search(r"[A-Za-z_$]", SANCTIONED.fullmatch(fast[2]).group(1)) is not None)

# Class-edge probe against the pinned CLI (executed, 3.54.0): SQLite accepts both, MiniDB rejects
# both. They are declared divergences -- LIMITS.md row -- not sanctioned quirks, so they are pinned
# here rather than left to the rule's masking.
DECLARED_DIVERGENCES = {
    "SELECT 0x1f": 'unrecognized token: "0x1f"',
    "SELECT 1_000": 'unrecognized token: "1_000"',
}

ALPHABET = "abc_ 0123456789()[],;.<>=!+-*/%'\"`[]|~\n\t\xc4"


def stream(fn, sql):
    """Seven-field token stream, or an exception triple -- both are comparable values."""
    try:
        return [(t.kind, t.value, t.raw, t.quote, t.line, t.col, t.pos) for t in fn(sql)]
    except Exception as exc:  # the exception IS the observable for a rejected input
        return ("EXC", type(exc).__name__, str(exc))


def differ(fast, ref):
    return fast != ref


def build_inputs():
    corpus = sorted(glob.glob(os.path.join(ROOT, "tests", "corpus", "*")))
    corpus_sql = [open(path).read() for path in corpus]
    rnd = random.Random(SEED)
    randoms = ["".join(rnd.choice(ALPHABET) for _ in range(rnd.randint(1, 60)))
               for _ in range(RANDOM_COUNT)]
    fixture = json.load(open(os.path.join(ROOT, "tests", "lexer_oddities.json")))
    return corpus_sql, randoms, list(TRAPS), list(fixture), fixture


class TestLexerEquivalence(unittest.TestCase):

    def test_the_fixture_still_matches_a_live_run(self):
        corpus_sql, randoms, traps, oddities, fixture = build_inputs()
        for sql, expected in fixture.items():
            live = stream(tokenize, sql)
            if isinstance(expected, list) and len(expected) == 1 and isinstance(expected[0], list):
                expected = expected[0]      # fixture values are single-entry wrap lists
            expected = [tuple(tok) for tok in expected[1]] if expected[0] == "OK" else tuple(expected)
            self.assertEqual(live, expected, f"fixture drifted from the engine: {sql!r}")

    def test_the_two_paths_agree_on_every_input_outside_the_exception_set(self):
        corpus_sql, randoms, traps, oddities, _ = build_inputs()
        declared = {"corpus": len(corpus_sql), "random": RANDOM_COUNT,
                    "traps": len(TRAPS), "oddities": len(oddities)}
        for name, size in declared.items():
            self.assertGreater(size, 0, f"declared {name} count must be non-zero")

        compared = 0
        tokens_compared = 0
        raised = 0
        for sql in corpus_sql + randoms + traps + oddities:
            compared += 1
            fast, ref = stream(tokenize, sql), stream(_tokenize_scanner, sql)
            if isinstance(fast, tuple):
                raised += 1
            else:
                tokens_compared += len(fast)
            if sanctioned(fast) and fast != ref:
                continue
            self.assertFalse(differ(fast, ref),
                             f"tokenization differs on {sql!r}\n  fast: {fast}\n   ref: {ref}")

        # Coverage units, not just input counts: an input that raises tests nothing past its first
        # token, so `0 mismatches` is ambiguous without these two numbers beside it.
        print(f"coverage: inputs={compared} tokens={tokens_compared} raised={raised}")

        # canary (a): executed counter against declared part-sum, never against len() of a concat.
        self.assertEqual(compared, sum(declared.values()),
                         f"executed {compared} comparisons, declared {sum(declared.values())}")

    def test_the_comparator_can_actually_fire(self):
        left = stream(tokenize, "SELECT 1")
        right = [(k, v, r, q, line, col + 1, pos + 1) for (k, v, r, q, line, col, pos) in left]
        # canary (b): if this asserts False the machinery works; the assertion's success is the proof.
        self.assertTrue(differ(left, right), "comparator failed to flag a shifted position")

    def test_declared_divergences_are_pinned_separately_from_the_rule(self):
        for sql, message in DECLARED_DIVERGENCES.items():
            self.assertEqual(stream(tokenize, sql), ("EXC", "LexError", message),
                             f"declared divergence moved: {sql!r}")

    def test_the_sanctioned_rule_is_no_wider_than_the_spec(self):
        self.assertTrue(sanctioned(("EXC", "LexError", 'unrecognized token: "2e"')))
        self.assertTrue(sanctioned(("EXC", "LexError", 'unrecognized token: "123abc"')))
        self.assertFalse(sanctioned(("EXC", "LexError", 'unrecognized token: "123"')),
                         "a pure number must not count as sanctioned")
        self.assertFalse(sanctioned(("EXC", "ParseError", "near \".5\": syntax error")))


if __name__ == "__main__":
    unittest.main()

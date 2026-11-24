"""The anchors of the rules: checks' copy of Textual's slug makes the same ones as Textual's Markdown viewer"""
from textual._slug import slug as textual_slug

from slurp.checks import engine


def test_same_as_textual(check):
  odd = ["  Hello, World!  ", "a_b-c d", "emoji 😀 here", "x/y: (z) [w]", "Ünïcode ß", "tab\there", "100% sure?"]
  headings = [r.heading for r in engine.RULES.values()] + odd
  differ = [h for h in headings if engine.slug(h) != textual_slug(h)]
  check("every rule's heading (and odd text): the same anchor", not differ and len(headings) > 20, differ[:5])

"""Notes in Markdown: web addresses and the paths that exist become links; code blocks and links stay as they are"""
from slurp.ui.notes import candidates, linked

NOTES = """# Run 3
see /scratch/me/run3/out.log:12 and `~/proj/input.dat`, logs/a_b.txt.
https://example.org/x_y. [doc](https://a.b) and <https://c.d>
```
/in/a/code/block
```
and/or /does/not/exist
"""


def test_the_candidates(check):
  got = candidates(NOTES)
  check("paths outside code blocks and links: absolute (with :line), ~, in `code`, relative with a slash",
        got[:3] == ["/scratch/me/run3/out.log:12", "~/proj/input.dat", "logs/a_b.txt"] and "/in/a/code/block" not in got
        and "/does/not/exist" in got and not any("example.org" in w for w in got), got)


def test_linked(check):
  found = {"/scratch/me/run3/out.log:12": "/scratch/me/run3/out.log", "~/proj/input.dat": "/home/me/proj/input.dat",
           "logs/a_b.txt": "/scratch/me/run3/logs/a_b.txt"}
  out = linked(NOTES, found)
  check("an existing path: a link (as written, :line kept)", "[/scratch/me/run3/out.log:12](</scratch/me/run3/out.log:12>)" in out, out)
  check("in `code`: the code is the link's text", "[`~/proj/input.dat`](<~/proj/input.dat>)" in out, out)
  check("_ in the shown text escaped, not in the link", r"[logs/a\_b.txt](<logs/a_b.txt>)" in out, out)
  check("a web address: a link, without the full stop after it", r"[https://example.org/x\_y](<https://example.org/x_y>)." in out, out)
  check("links and autolinks stay", "[doc](https://a.b)" in out and "<https://c.d>" in out and "[doc](<" not in out, out)
  check("a code block stays, a path that does not exist stays text", "```\n/in/a/code/block\n```" in out and "and/or /does/not/exist" in out, out)
  check("nothing found: only the web addresses", linked(NOTES).count("](<") == 1)

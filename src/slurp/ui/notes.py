"""Notes in Markdown (of categories and jobs): what is shown is the text with its web addresses and the paths in it that
exist on the cluster as links; a click on one opens it (a page in the browser, a file or directory in slurp)"""

import re

from ..cluster.paths import resolve_path
from ..cluster.remote import in_background

URL = re.compile(r"https?://[^\s<>()\[\]`'\"]+[^\s<>()\[\]`'\".,;:!?]")
# (a path: absolute, ~/…, $VAR/…, or relative with a slash in it; maybe with :line)
PATH = re.compile(r"(?:~|\$\{?\w+\}?)?/?[\w.+-]+(?:/[\w.+-]*)+(?::\d+)?|~/?|/[\w.+-]+")
MAX_PATHS = 40 # paths looked up at most (each a round trip on a remote cluster)

def segments(text):
  """The text in parts: (part, kind) with kind "code" (a fenced block, kept as it is), "inline" (a `code` span),
  "link" (a Markdown link or autolink, kept), "text"""
  out = []
  fence = None
  for line in text.splitlines(keepends=True):
    m = re.match(r"\s*(```|~~~)", line)
    if fence is not None:
      out.append((line, "code"))
      if m and m.group(1) == fence:
        fence = None
      continue
    if m:
      fence = m.group(1)
      out.append((line, "code"))
      continue
    pos = 0
    for k in re.finditer(r"`[^`\n]+`|!?\[[^\]\n]*\]\([^)\n]*\)|<https?://[^>\n]+>", line):
      if k.start() > pos:
        out.append((line[pos:k.start()], "text"))
      out.append((k.group(0), "inline" if k.group(0).startswith("`") else "link"))
      pos = k.end()
    if pos < len(line):
      out.append((line[pos:], "text"))
  return out

def candidates(text):
  """The words of the text (outside code blocks and links) that may be paths: [word]"""
  words = []
  for part, kind in segments(text):
    if kind == "text":
      for m in PATH.finditer(URL.sub(" ", part)):
        words.append(m.group(0).rstrip(".,;:"))
    elif kind == "inline" and PATH.fullmatch(part[1:-1].strip()):
      words.append(part[1:-1].strip())
  return list(dict.fromkeys(w for w in words if len(w) > 1))[:MAX_PATHS]

def linked(text, paths=None):
  """The text with its web addresses, and the words that are existing paths (paths: {word: path}), as links"""
  paths = paths or {}
  def link(word, shown):
    return f"[{shown}](<{word}>)"
  out = []
  for part, kind in segments(text):
    if kind == "text":
      pieces, pos = [], 0
      for m in sorted([*URL.finditer(part), *(x for x in PATH.finditer(part) if x.group(0).rstrip(".,;:") in paths)],
                      key=lambda m: m.start()):
        if m.start() < pos:
          continue
        word = m.group(0) if m.re is URL else m.group(0).rstrip(".,;:")
        pieces.append(part[pos:m.start()])
        pieces.append(link(word, word.replace("_", r"\_").replace("*", r"\*")))
        pos = m.start() + len(word)
      pieces.append(part[pos:])
      out.append("".join(pieces))
    elif kind == "inline" and part[1:-1].strip() in paths:
      out.append(link(part[1:-1].strip(), part))
    else:
      out.append(part)
  return "".join(out)

def show_notes(screen, widget, text, bases=(), env=None):
  """Show notes in a Markdown widget: at once with the web addresses as links, then (looked up in the background)
  with the paths that exist too"""
  widget.notes_markdown = linked(text) # (what is shown)
  widget.update(widget.notes_markdown)
  words = candidates(text)
  if not words:
    return
  def work():
    found = {}
    for word in words:
      path = resolve_path(word, bases, env)
      if path:
        found[word] = path
    return found
  def looked_up(found):
    if found and getattr(widget, "notes_text", None) == text and widget.is_attached:
      widget.notes_markdown = linked(text, found)
      widget.update(widget.notes_markdown)
  widget.notes_text = text
  in_background(screen, work, looked_up, group="notes-paths")

def open_link(screen, href, crumbs, bases=(), env=None):
  """A click on a link of notes: a web page in the browser, a path (as written, looked up again) in slurp"""
  from .screens.files import open_resolved
  href = (href or "").strip().strip("<>")
  if href.startswith(("http://", "https://")):
    screen.app.open_page(href)
  elif href and not re.match(r"\w+:", href):
    open_resolved(screen, href, crumbs, bases, env, quiet=False)

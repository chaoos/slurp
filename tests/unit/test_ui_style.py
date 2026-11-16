"""The look of the windows: every button is compact (one line, as wide as its label), as the rows of buttons are
laid out for them; a three-line one next to them stands out (and is how they got uneven)"""
import ast
import os

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "slurp", "ui")


def test_every_button_is_compact():
  loose = []
  for root, _, files in os.walk(UI):
    for name in files:
      if name.endswith(".py"):
        path = os.path.join(root, name)
        for node in ast.walk(ast.parse(open(path).read())):
          if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "Button":
            compact = next((k.value for k in node.keywords if k.arg == "compact"), None)
            if not (isinstance(compact, ast.Constant) and compact.value is True):
              loose.append(f"{os.path.relpath(path, UI)}:{node.lineno}")
  assert not loose, f"buttons without compact=True: {loose}"

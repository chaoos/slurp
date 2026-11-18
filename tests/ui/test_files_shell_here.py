"""The directory browser: Shell here (s, its button): a shell in a subtab that starts in the directory shown (the
user's login shell, there); a directory with spaces too"""
import asyncio
import os
import subprocess

from slurp.ui.app import Slurp


def test_shell_here(check, tmp_path):
  from slurp.ui.screens.files import DirScreen, open_path, shell_in
  folder = tmp_path / "my runs"
  folder.mkdir()
  (folder / "a.txt").write_text("a")

  script = tmp_path / "fakeshell"
  script.write_text('#!/bin/sh\npwd\necho "args: $*"\n')
  script.chmod(0o755)
  out = subprocess.run(shell_in(str(folder)), capture_output=True, text=True, env={**os.environ, "SHELL": str(script)}).stdout
  check("the command: in the directory, then the login shell", out.splitlines() == [str(folder), "args: -l"], out)

  async def main():
    app = Slurp()
    async with app.run_test(size=(160, 45)) as pilot:
      await pilot.pause(0.5)
      opened = []
      app.open_shell = lambda args, title, focus=True, via=None: opened.append((args, title))
      open_path(app, ["files"], str(folder)); await pilot.pause(0.8)
      screen = app.screen
      check("the directory browser, with its button", isinstance(screen, DirScreen) and bool(screen.query("#dshell")), type(screen).__name__)
      screen.query_one("#dshell").press(); await pilot.pause(0.3)
      check("Shell here: a shell in it", opened and opened[-1][0] == shell_in(str(folder)) and "my runs" in opened[-1][1], opened)
      screen.query_one("#dir").focus(); await pilot.press("s"); await pilot.pause(0.3)
      check("s: the same", len(opened) == 2 and opened[-1][0] == shell_in(str(folder)), opened)
      await pilot.press("q")

  asyncio.run(main())

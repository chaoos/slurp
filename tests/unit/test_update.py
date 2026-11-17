"""A newer slurp: PyPI asked at most once a day (kept in the cache), offline nothing; versions compared; the command
that updates it, as it was installed"""
import io
import json
import time

from slurp import update


def fake_pypi(monkeypatch, versions, calls):
  def urlopen(request, timeout=None):
    calls.append(request.full_url)
    if versions is None:
      raise OSError("offline")
    return io.BytesIO(json.dumps({"info": {"version": versions[-1]}, "releases": {v: [{}] for v in versions}}).encode())
  monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)


def test_the_newest_version(check, monkeypatch):
  calls = []
  fake_pypi(monkeypatch, ["0.8.0", "0.8.1", "0.10.0", "0.9.5", "0.11.0rc1"], calls)
  check("the newest release (not a pre-release), from slurp-ui", update.latest_version() == "0.10.0" and calls == [update.URL], calls)
  check("asked again the same day: from the cache, not PyPI", update.latest_version() == "0.10.0" and len(calls) == 1)
  check("a day later: asked again", update.latest_version(now=time.time() + 25 * 3600) == "0.10.0" and len(calls) == 2)


def test_offline(check, monkeypatch):
  calls = []
  fake_pypi(monkeypatch, None, calls)
  check("offline: nothing (no error)", update.latest_version() is None and len(calls) == 1)


def test_versions(check):
  check("0.10.0 is newer than 0.9.2", update.newer("0.10.0", "0.9.2"))
  check("the same: not newer", not update.newer("0.8.1", "0.8.1"))
  check("a release is newer than its pre-release, not the other way", update.newer("0.9.0", "0.9.0rc1") and not update.newer("0.9.0rc1", "0.9.0"))
  check("nothing known: not newer", not update.newer(None, "0.8.1"))


def test_how_to_update(check):
  how, command = update.how_to_update(prefix="/home/u/.local/share/pipx/venvs/slurp-ui", repo=None)
  check("pipx", how == "pipx" and command == "pipx upgrade slurp-ui", command)
  check("uv tool", update.how_to_update(prefix="/home/u/.local/share/uv/tools/slurp-ui", repo=None)[1] == "uv tool upgrade slurp-ui")
  how, command = update.how_to_update(prefix="/home/u/venv", executable="/home/u/venv/bin/python", repo=None)
  check("pip in a venv: that python's pip", command == "/home/u/venv/bin/python -m pip install -U 'slurp-ui[all]'", command)
  how, command = update.how_to_update(executable="/x/python", repo="/src/slurp")
  check("a git checkout: pull, install again", command == "git -C /src/slurp pull && /x/python -m pip install -e '/src/slurp[all]'", command)


def test_off(check, monkeypatch):
  monkeypatch.setenv("SLURP_NO_UPDATE_CHECK", "1")
  check("off for the tests (and by choice)", not update.enabled())
  monkeypatch.delenv("SLURP_NO_UPDATE_CHECK")
  from slurp.config import update_config
  update_config(update_check=False)
  check("…and with update_check false in the config", not update.enabled())
  update_config(update_check=None)
  check("on by default", update.enabled())

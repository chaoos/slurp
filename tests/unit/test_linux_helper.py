"""The slurp helper for Linux without a lab: it reads the links slurp makes and refuses broken ones, resolves the
folders of a drive, and its setup is a valid shell script that carries it"""
import base64
import gzip
import re
import subprocess
import urllib.parse

import pytest

from slurp.links import linux, share
from slurp.links.linux import slurp_helper as h


def url(kind="drive", **change):
  fp = share.host_fingerprint()
  q = {"v": "1", "sv": "0.8.0", "id": "0123abcd", "t": "drive: lab", "r": "openssh", "rh": "relay.example.org", "j": "me@relay.example.org",
       "h": "localhost", "p": "40022", "u": "slurp", "fp": fp, "a": share.host_alias(), "x": "0",
       "path": "/home/me", "dirs": "HOME=/home/me\nSCRATCH=/scratch/me"}
  if kind == "web":
    q |= {"wp": "8123", "tok": "tok123tok123"}
  q.update(change)
  q = {k: v for k, v in q.items() if v is not None}
  return f"slurp://{kind}?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote, safe="")


def test_reads_slurps_links():
  link = h.parse_link(url())
  assert link["kind"] == "drive" and link["port"] == 40022 and link["jump"] == "me@relay.example.org" and not link["straight"]
  assert link["dirs"] == [("HOME", "/home/me"), ("SCRATCH", "/scratch/me")]
  assert h.alias_of(share.host_fingerprint()) == share.host_alias() # (the same name for slurp's host key as slurp's)
  web = h.parse_link(url("web"))
  assert web["web_port"] == 8123 and web["token"] == "tok123tok123"
  direct = h.parse_link(url(r="direct", h="slurp.example.org", j=None))
  assert direct["straight"] and direct["jump"] == ""


@pytest.mark.parametrize("relay", ["me@relay.example.org", "relay.example.org", "ssh-j.com", "direct://slurp.example.org:2222"])
def test_slurps_own_links(relay):
  """slurp makes one slurp:// link for both helpers (windows/winhelper.link_url); an own relay with a user too"""
  from slurp.links.windows import winhelper
  link = share.Link("drive", "drive: lab", "lab", lambda: [], relay=relay, path="/home/me", dirs=[("HOME", "/home/me")])
  link.relay_port, link.port = 40022, 2222
  parsed = h.parse_link(winhelper.link_url(link))
  assert parsed["id"] == link.id and parsed["dirs"] == [("HOME", "/home/me")] and "@" not in parsed["relay_host"]
  assert parsed["jump"] == link.jump()


@pytest.mark.parametrize("change, why", [
  ({"v": "2"}, "newer slurp helper"),
  ({"id": "zz"}, "'id'"),
  ({"fp": "SHA256:" + "A" * 43}, "do not match"), # (a host key whose name is not the link's)
  ({"h": "evil.example.org"}, "host is not valid"), # (through a relay: only slurp's end of the tunnel)
  ({"p": "70000"}, "port is not valid"),
  ({"j": "me@relay;rm -rf ~"}, "'j'"),
  ({"u": "root user"}, "'u'"),
])
def test_refuses_broken_links(change, why):
  with pytest.raises(h.Failed, match=re.escape(why)):
    h.parse_link(url(**change))
  with pytest.raises(h.Failed, match="not a slurp link"):
    h.parse_link("slurp://exec?v=1")


def test_folders_of_a_drive():
  link = h.parse_link(url())
  assert h.resolve_folder(link, "$SCRATCH/run1") == "/scratch/me/run1"
  assert h.resolve_folder(link, "runs/a/") == "/home/me/runs/a"
  assert h.resolve_folder(link, "~/x") == "/home/me/x"
  assert h.resolve_folder(link, "/") == "/"
  for bad in ("$PROJECT", "/home/me/../other", "a\nb"):
    with pytest.raises(h.Failed):
      h.resolve_folder(link, bad)


def test_setup_is_a_shell_script_with_the_helper():
  script = linux.setup_script()
  assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
  blob = re.search(r"echo '([A-Za-z0-9+/=]{1000,})' \| base64 -d \| gzip -d > \"\$dir/slurp-helper.new\"", script)
  helper = gzip.decompress(base64.b64decode(blob.group(1))).decode()
  from slurp import __version__
  assert f"VERSION = {__version__!r}" in helper and "def parse_link" in helper
  compile(helper, "slurp-helper", "exec")
  assert f"{share.host_alias()} {share.host_fingerprint()}" in script # (it trusts this slurp)
  command = linux.setup_command()
  assert "\n" not in command and gzip.decompress(base64.b64decode(command.split()[1])).decode() == script

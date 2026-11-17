"""A directory listed with permissions, owners, extended ACLs and what the user may do: the script that lists it on a
cluster (run here with sh) and the local listing give the same"""
import os
import shutil
import subprocess

import pytest

from slurp.cluster import listing


@pytest.fixture
def tree(tmp_path):
  if os.geteuid() == 0:
    pytest.skip("root may read and write everything")
  d = tmp_path / "d"
  (d / "sub").mkdir(parents=True)
  (d / "closed").mkdir()
  (d / "notes.txt").write_text("hello\n")
  (d / "secret").write_text("x")
  (d / "readonly").write_text("x")
  (d / "with space").write_text("x")
  os.symlink("notes.txt", d / "link")
  os.chmod(d / "secret", 0o000)
  os.chmod(d / "readonly", 0o444)
  os.chmod(d / "closed", 0o000)
  acl = False
  if shutil.which("setfacl"):
    acl = subprocess.run(["setfacl", "-m", "u:root:r--", str(d / "notes.txt"), str(d / "with space")]).returncode == 0 \
          and subprocess.run(["setfacl", "-d", "-m", "u:root:rwx", str(d)]).returncode == 0
  yield d, acl
  os.chmod(d / "closed", 0o700)


def by_name(entries):
  return {e["name"]: e for e in entries}


def test_the_cluster_script_and_the_local_listing(check, tree):
  d, acl = tree
  out = subprocess.run(["sh", "-c", listing.SCRIPT, "sh", str(d)], capture_output=True, text=True).stdout
  this, entries = listing.parse(out, str(d))
  local_this, local_entries = listing.local_listing(str(d))
  got, local = by_name(entries), by_name(local_entries)
  check("the same entries", set(got) == set(local) == {"sub", "closed", "notes.txt", "secret", "readonly", "with space",
                                                        "link"}, (sorted(got), sorted(local)))
  for name in got:
    a, b = got[name], local.get(name, {})
    for key in ("dir", "size", "link", "mode", "owner", "group", "access", "acl"):
      check(f"{name}: {key} the same", a[key] == b.get(key), (a[key], b.get(key)))
  check("the directory itself", this["mode"].startswith("d") and this["access"] == "rwx"
        and all(this[k] == local_this[k] for k in ("mode", "owner", "access", "acl")), (this, local_this))
  check("permissions as ls -l", got["readonly"]["mode"] == "-r--r--r--" and got["sub"]["mode"].startswith("drwx")
        and got["link"]["mode"].startswith("l"), got["readonly"]["mode"])
  check("a file that cannot be read", not listing.can_read(got["secret"]) and not listing.can_write(got["secret"]))
  check("a read-only file", listing.can_read(got["readonly"]) and not listing.can_write(got["readonly"]))
  check("a directory that cannot be entered", not listing.can_read(got["closed"]) and not listing.can_write(got["closed"]))
  check("a file and a directory of the user", all(listing.can_read(got[n]) and listing.can_write(got[n]) for n in ("notes.txt", "sub")))
  if acl:
    check("extended ACL entries (also of a name with a space), with + after the permissions",
          "user:root:r--" in got["notes.txt"]["acl"] and "user:root:r--" in got["with space"]["acl"]
          and listing.mode_text(got["notes.txt"]).endswith("+") and not got["readonly"]["acl"], got["notes.txt"]["acl"])
    check("the default ACL of the directory, short", "default:user:root:rwx" in this["acl"]
          and "d:u:root:rwx" in listing.short_acl(this["acl"]), this["acl"])
  check("the limit", len(listing.parse(out, str(d), limit=2)[1]) == 2)


def test_a_directory_that_cannot_be_listed(check, tree):
  d, _ = tree
  out = subprocess.run(["sh", "-c", listing.SCRIPT, "sh", str(d / "closed")], capture_output=True, text=True).stdout
  this, entries = listing.parse(out, str(d / "closed"))
  local_this, local_entries = listing.local_listing(str(d / "closed"))
  check("only itself, which says why", entries == [] == local_entries and this["access"] == local_this["access"] == "---"
        and not listing.can_read(this), (this, local_this))


def test_getfacl_output(check):
  text = ("# file: /d\n# owner: me\n# group: g\nuser::rwx\ngroup::r-x\nmask::rwx\nother::---\n"
          "default:user::rwx\ndefault:user:bob:rwx\n\n"
          "# file: /d/a\\040b\n# owner: me\n# group: g\nuser::rw-\nuser:bob:rw-\t#effective:r--\ngroup::r--\nmask::r--\n"
          "other::---\n\n")
  check("extended entries only, names unescaped, #effective dropped", listing.parse_getfacl(text, "/d") == {
        "": ["mask::rwx", "default:user:bob:rwx"], "a b": ["user:bob:rw-", "mask::r--"]}, listing.parse_getfacl(text, "/d"))

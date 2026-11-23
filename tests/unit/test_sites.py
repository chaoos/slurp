"""Site packs (slurp.sites): the built-in ones are valid and versioned (schema, version: a date); a pack of a newer
schema is left out, one without a schema is read as 1; yours take precedence; the rule catalogue says which pack and
version applies"""
import datetime
import tomllib

from slurp import sites
from slurp.checks import engine as c


def test_the_builtin_packs(check):
  packs = [p for p in sites.load_packs() if p["_builtin"]]
  check("the built-in packs are there", {p["name"] for p in packs} >= {"CSCS Alps", "JSC JEDI", "JSC JURECA", "JSC JUSUF", "JSC JUWELS"},
        [p.get("name") for p in packs])
  for p in packs:
    file = p["_path"].rsplit("/", 1)[-1]
    with open(p["_path"], "rb") as f:
      raw = tomllib.load(f)
    version = str(raw.get("version", ""))
    check(f"{file}: a schema this slurp reads, written out", raw.get("schema") == sites.SCHEMA, raw.get("schema"))
    check(f"{file}: a version, a real date (YYYY.MM.DD)", bool(sites.VERSION.fullmatch(version))
          and datetime.datetime.strptime(version, "%Y.%m.%d") <= datetime.datetime.now(), version)
    check(f"{file}: a name and patterns to match", p.get("name") and p.get("match"))


def test_schema_and_precedence(check, tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  rules = tmp_path / "slurp" / "rules"
  rules.mkdir(parents=True)
  (rules / "a-old.toml").write_text('name = "Mine"\nmatch = ["daint*"]\n')
  (rules / "b-newer.toml").write_text(f'schema = {sites.SCHEMA + 1}\nname = "Future"\nmatch = ["*"]\n')
  (rules / "c-text.toml").write_text('schema = "1"\nname = "Text"\nmatch = ["*"]\n')
  packs = sites.load_packs()
  names = [p["name"] for p in packs]
  check("without a schema: read (as 1)", "Mine" in names, names)
  check("a newer schema, or one that is not a number: left out", "Future" not in names and "Text" not in names, names)
  facts = c.Facts(env={"HOSTNAME": "daint-ln001"})
  pack = sites.pack_for("daint", "", facts)
  check("yours before the built-in one", pack.get("name") == "Mine" and not pack["_builtin"], pack.get("name"))
  check("…said so, with the file (no version: none said)", sites.pack_text(pack) == f"Mine (yours: {rules / 'a-old.toml'})", sites.pack_text(pack))


def test_the_catalogue_says_it(check, tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path)) # (none of yours)
  facts = c.Facts(env={"HOSTNAME": "daint-ln001"})
  facts.site = sites.pack_for("daint", "", facts)
  version = facts.site.get("version")
  text = c.rules_markdown(facts, "daint")
  check("the catalogue of a tab: its site pack, version, built in", f"**Site pack:** CSCS Alps, version {version} (built in)." in text)
  text = c.rules_markdown(c.Facts(), "mycluster")
  check("…none: said", "**Site pack:** none for mycluster" in text)
  check("without a cluster (slurp check --rules): no line", "Site pack" not in c.rules_markdown())


def test_where_the_user_sees_it(check, tmp_path, monkeypatch):
  import subprocess
  import sys
  import types
  from slurp.ui.screens import help as H
  from slurp.ui.screens.overview import OverviewScreen
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  rules = tmp_path / "slurp" / "rules"
  rules.mkdir(parents=True)
  (rules / "mine.toml").write_text('version = "2026.11.01"\nname = "Mine"\nmatch = ["mycluster"]\n')
  alps = next(p for p in sites.load_packs() if p.get("name") == "CSCS Alps")
  out = subprocess.run([sys.executable, "-m", "slurp", "--version"], capture_output=True, text=True).stdout
  lines = out.splitlines()
  check("slurp --version: the program, then each pack on its line, with its version, built in or yours (its file)",
        lines[0].startswith("slurp ") and lines[1] == f"site packs (format {sites.SCHEMA}):"
        and any(line.split()[:3] == ["CSCS", "Alps", alps["version"]] and line.endswith("built in") for line in lines)
        and any(line.split()[:2] == ["Mine", "2026.11.01"] and line.endswith(f"yours: {rules / 'mine.toml'}") for line in lines), out)
  out = subprocess.run([sys.executable, "-m", "slurp", "check", "--version"], capture_output=True, text=True).stdout
  check("…slurp check --version too", "site packs" in out and "CSCS Alps" in out, out)
  line = sites.packs_line()
  check("About slurp, its version section: the packs in one line, the built-in ones and yours",
        f"built in: CSCS Alps {alps['version']}" in line and "yours (" in line and "Mine 2026.11.01" in line, line)
  cluster = types.SimpleNamespace(name="daint", destination="me@daint.alps.cscs.ch", connection=None)
  app = types.SimpleNamespace(clusters=[cluster], cluster=cluster, is_web=False, size=types.SimpleNamespace(width=100, height=30), view_kind="terminal")
  monkeypatch.setattr("slurp.links.holder.under_holder", lambda: False)
  facts = H.about_facts(app)
  text = H.about_text(facts)
  check("…and its tab's pack", f"its site pack: CSCS Alps, version {alps['version']} (built in)" in text and "site packs: built in:" in text, text)
  ov = OverviewScreen.__new__(OverviewScreen)
  site = str(OverviewScreen.site(ov, types.SimpleNamespace(name="daint", destination="")))
  check("the overview: the tab's pack, its version, before status and docs", site.startswith(f"CSCS Alps · pack {alps['version']}")
        and "status.cscs.ch" in site, site)
  site = str(OverviewScreen.site(ov, types.SimpleNamespace(name="mycluster", destination="")))
  check("…yours: said", site == "Mine · pack 2026.11.01 (yours)", site)
  check("…no pack: nothing", str(OverviewScreen.site(ov, types.SimpleNamespace(name="elsewhere", destination=""))) == "")


def test_the_clusters_of_the_sites(check, tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  clusters = {c["id"]: c for c in sites.site_clusters()}
  daint = clusters.get("cscs-alps/daint", {})
  check("the built-in clusters: Alps and JSC's", {"cscs-alps/daint", "cscs-alps/eiger", "cscs-alps/santis", "cscs-alps/clariden",
        "juelich-jureca/jureca", "juelich-juwels/juwels-cluster", "juelich-juwels/juwels-booster", "juelich-jusuf/jusuf",
        "juelich-jedi/jedi"} <= set(clusters), sorted(clusters))
  check("…a cluster takes the site's [login]", daint.get("jump") == "{user}@ela.cscs.ch" and daint.get("pre_login") == "cscs-key sign --headless"
        and daint.get("site") == "CSCS Alps" and daint.get("builtin") and daint.get("version"), daint)
  fill = sites.login_settings(daint, "me")
  check("the form's fields, the user filled in (in the jump host too)", fill["jump"] == "me@ela.cscs.ch" and fill["user"] == "me"
        and fill["identities"] == ["~/.ssh/cscs-key"] and fill["pre_login_fallback"] is True)
  check("…no user yet: the jump host without one", sites.login_settings(daint)["jump"] == "ela.cscs.ch")
  check("JEDI's pack applies to its login host too", sites.site_pack("x", "me@login.jedi.fz-juelich.de").get("name") == "JSC JEDI")
  rules = tmp_path / "slurp" / "rules"
  rules.mkdir(parents=True)
  (rules / "lab.toml").write_text('''name = "Lab"
match = ["lab*"]
[login]
jump = "{user}@gw.lab.example:2200"
identities = ["~/.ssh/lab"]
[[clusters]]
name = "lab1"
host = "lab1.example"
port = 2222
jump = ""
[[clusters]]
name = "bad host"
host = "x; rm -rf ~"
[[clusters]]
name = "lab2"
host = "lab2.example"
pre_login = "a\\nb"
[[clusters]]
name = "lab3"
host = "lab3.example"
options = ["ProxyCommand"]
[[clusters]]
name = "lab4"
host = "lab4.example"
identities = ["relative/key"]
needs = [{ command = "x", install = "http://insecure" }]
[[clusters]]
name = "lab5"
host = "lab5.example"
port = true
''')
  clusters = {c["id"]: c for c in sites.site_clusters()}
  lab = clusters.get("lab/lab1", {})
  check("yours: a cluster can say otherwise than [login] (no jump host); its port; the title: its name", lab.get("jump") == ""
        and lab.get("port") == 2222 and lab.get("title") == "lab1" and not lab.get("builtin"), lab)
  check("left out: a host that is no host name, a command of two lines, an option without a value, a relative key, an "
        "install link without https, true as a port", not any(k in clusters for k in ("lab/bad host", "lab/lab2", "lab/lab3", "lab/lab4", "lab/lab5")),
        sorted(k for k in clusters if k.startswith("lab/")))
  check("errors say what", any("not a host name" in e for e in sites.login_errors({"name": "a", "host": "x y"}))
        and sites.login_errors({"name": "a", "host": "h", "port": True}) == ["port: not a number"])

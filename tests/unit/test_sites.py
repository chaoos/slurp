"""Site packs (slurp.sites): the built-in ones are valid and versioned (schema, version: a date); a pack of a newer
schema is left out, one without a schema is read as 1; yours take precedence; the rule catalogue says which pack and
version applies"""
import datetime
import tomllib

from slurp import sites
from slurp.checks import engine as c


def test_the_builtin_packs(check):
  packs = [p for p in sites.load_packs() if p["_builtin"]]
  check("the built-in packs are there", {p["name"] for p in packs} >= {"CSCS Alps", "JSC JEDI", "JSC JURECA", "JSC JUSUF", "JSC JUWELS", "LUMI", "CINECA Leonardo"},
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
        line.startswith("built in: ") and f"CSCS Alps {alps['version']}" in line.split(";")[0] and "yours (" in line and "Mine 2026.11.01" in line, line)
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
        "juelich-jedi/jedi", "lumi/lumi", "cineca-leonardo/leonardo"} <= set(clusters), sorted(clusters))
  lumi, leonardo = clusters.get("lumi/lumi", {}), clusters.get("cineca-leonardo/leonardo", {})
  check("LUMI: directly to lumi.csc.fi, with a key of its own", lumi.get("host") == "lumi.csc.fi" and not lumi.get("jump")
        and lumi.get("identities") == ["~/.ssh/id_ed25519_lumi"] and not lumi.get("pre_login"), lumi)
  check("Leonardo: a step certificate (in the agent: no key file), only when a login without it fails",
        leonardo.get("host") == "login.leonardo.cineca.it" and leonardo.get("pre_login") == "step ssh login --provisioner cineca-hpc"
        and leonardo.get("pre_login_fallback") is True and not leonardo.get("identities")
        and [n["command"] for n in leonardo.get("needs", [])] == ["step"], leonardo)
  check("…a cluster takes the site's [login]", daint.get("jump") == "{user}@ela.cscs.ch" and daint.get("pre_login") == "cscs-key sign --headless"
        and daint.get("site") == "CSCS Alps" and daint.get("builtin") and daint.get("version"), daint)
  fill = sites.login_settings(daint, "me")
  check("the form's fields, the user filled in (in the jump host too)", fill["jump"] == "me@ela.cscs.ch" and fill["user"] == "me"
        and fill["identities"] == ["~/.ssh/cscs-key"] and fill["pre_login_fallback"] is True)
  check("…no user yet: the jump host without one", sites.login_settings(daint)["jump"] == "ela.cscs.ch")
  proxy = {"name": "p", "host": "h.example", "pre_login": "sshproxy.sh -u {user}"}
  check("{user} in the command before the login: the user, quoted (one word of the shell); none yet: {user} stays",
        [sites.login_settings(proxy, u)["pre_login"] for u in ("me", "a b;rm -rf ~", "")]
        == ["sshproxy.sh -u me", "sshproxy.sh -u 'a b;rm -rf ~'", "sshproxy.sh -u {user}"])
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
  builtin = [c for c in sites.site_clusters() if c["builtin"]]
  check("required: the built-in clusters need the user", all("user" in c.get("required", []) for c in builtin))
  keys_only = {c["id"] for c in builtin if "identities" in c.get("required", [])}
  check("…the sites that log in with a key only: the key too (CSCS, JSC, LUMI, Karolina, …); not those with a "
        "certificate, a password or a code", {"cscs-alps/daint", "juelich-jureca/jureca", "lumi/lumi", "it4i-karolina/karolina"} <= keys_only
        and not keys_only & {"cineca-leonardo/leonardo", "nersc-perlmutter/perlmutter", "olcf-frontier/frontier", "mpcdf/mpcdf-raven"}, sorted(keys_only))
  check("…only fields of the form", sites.login_errors({"name": "a", "host": "h", "required": ["user", "password"]})
        == ["required: of user, host, port, jump, identities, pre_login"])
  check("errors say what", any("not a host name" in e for e in sites.login_errors({"name": "a", "host": "x y"}))
        and sites.login_errors({"name": "a", "host": "h", "port": True}) == ["port: not a number"])


def test_every_builtin_cluster(check, tmp_path, monkeypatch):
  """Every cluster of a built-in pack is offered (none left out as not right), and its host and name are its own
  pack's (no pack's match patterns catch another site's cluster)"""
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  packs = [p for p in sites.load_packs() if p["_builtin"]]
  written = sum(len(p.get("clusters", [])) for p in packs)
  offered = [c for c in sites.site_clusters(packs) if c["builtin"]]
  check("all the clusters of the built-in packs offered", len(offered) == written and written >= 30, (len(offered), written))
  wrong = []
  for entry in offered:
    for tab, destination, host in ((entry["name"], "", ""), ("x", f"me@{entry['host']}", ""), ("x", "", entry["host"].split(".")[0] if "." in entry["host"] else "")):
      if not (tab or destination or host):
        continue
      pack = sites.pack_for(tab, destination, c.Facts(env={"HOSTNAME": host} if host else {}))
      if pack and pack.get("name") != entry["site"]:
        wrong.append((entry["id"], tab, destination, host, pack.get("name")))
  check("…each its own pack's, by the tab's name, the destination, the login node's name", not wrong, wrong)
  check("…each says how to log in once (setup) and where the docs are", all(p.get("docs") for p in packs)
        and all(c.get("setup") for c in offered), [c["id"] for c in offered if not c.get("setup")])

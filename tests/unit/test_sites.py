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

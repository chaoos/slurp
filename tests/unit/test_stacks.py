"""Rules by software stack: some apply only on clusters with environment modules, some only on clusters with uenv;
module rules in part also in a uenv's modules view; the catalogue says which apply on a cluster"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts, Partition


def facts(stacks, **env):
  return Facts(partitions={"normal": Partition("normal", True, 1440, 288, 460000, 4, 4, 72, 1, False)}, accounts=["p"],
               sbatch_options=c.LONG_OPTIONS | {"uenv", "view"}, env={"HOME": "/users/me", "USER": "me", **env}, user="me",
               stacks=set(stacks))


def found(script, f, replay=None):
  findings, _ = c.check_script_text(script, "/users/me/job.sh", facts=f, replay=replay)
  return {(x.rule.id, x.evidence) for x in findings if x.rule.id in ("E01", "E04", "E05", "E06", "E07", "E08")}


def ids(script, f, replay=None):
  return {i for i, _ in found(script, f, replay)}


HEAD = "#!/bin/bash -l\n#SBATCH --time=00:10:00\n"


def test_detecting_the_stacks(check):
  f = facts([], LMOD_CMD="/usr/share/lmod/lmod/libexec/lmod")
  f.sbatch_options = set(c.LONG_OPTIONS)
  check("Lmod in the login shell: modules", c.detect_stacks(f) == {"modules"})
  f = facts([]); f.sbatch_options = set(c.LONG_OPTIONS) | {"uenv"}
  check("sbatch --uenv (the plugin's option): uenv", c.detect_stacks(f) == {"uenv"})
  f = facts([], MODULESHOME="/opt/modules"); f.sbatch_options = set(c.LONG_OPTIONS)
  check("both: the uenv command and modules", c.detect_stacks(f, uenv_command=True) == {"modules", "uenv"})
  f = facts([]); f.sbatch_options = set(c.LONG_OPTIONS)
  check("neither: none known (all rules apply)", c.detect_stacks(f) == set())
  f = facts([], LMOD_CMD="x"); f.site = {"stacks": ["uenv"]}
  check("a rule pack says it instead", c.detect_stacks(f) == {"uenv"})


def test_rules_by_stack(check):
  modules, uenv, unknown = facts(["modules"]), facts(["uenv"]), facts([])
  loads = HEAD + "module load gcc\nsrun ./sim\n"
  check("a module cluster: E01 (no version, no purge)", ids(loads, modules) == {"E01"} and len(found(loads, modules)) == 2, found(loads, modules))
  check("a uenv cluster without the modules view: no E01, but E08 (no module system)", ids(loads, uenv) == {"E08"}, found(loads, uenv))
  view = HEAD + "#SBATCH --uenv=prgenv-gnu/24.11:v1\n#SBATCH --view=modules\nmodule load gcc\nsrun ./sim\n"
  got = found(view, uenv)
  check("in the uenv's modules view: E01 in part (the version, not the purge), no E08",
        got == {("E01", "module load gcc without a version")}, got)
  view2 = view.replace("--view=modules", "--view=prgenv-gnu:modules,default")
  check("the modules view next to another one counts too", found(view2, uenv) == got, found(view2, uenv))
  check("unknown stacks: everything applies as before", ids(loads, unknown) == {"E01"})
  bare = HEAD + "#SBATCH --uenv=prgenv-gnu\nsrun ./sim\n"
  check("a module cluster: no uenv rules", not ids(bare, modules), found(bare, modules))
  got = found(bare, uenv)
  check("a uenv cluster: E06 (no version and tag), E07 (no view)", ids(bare, uenv) == {"E06", "E07"}, got)
  replay = {"uenv_list": ["prgenv-gnu/24.7:v1   gh200  daint  1111222233334444  10  2024", "prgenv-gnu/24.11:v2   gh200  daint  2222333344445555  10  2024"]}
  findings, _ = c.check_script_text(bare, "/users/me/job.sh", facts=uenv, replay=replay)
  e06 = next(x for x in findings if x.rule.id == "E06")
  check("E06 proposes the one pulled (uenv image ls)", e06.snippet == "#SBATCH --uenv=prgenv-gnu/24.11:v2", e06.snippet)
  exact = HEAD + "#SBATCH --uenv=prgenv-gnu/24.11:v1,editors/24.7:v2:/user-tools\n#SBATCH --view=default\nsrun ./sim\n"
  check("exact labels with a view: no uenv rule", not ids(exact, uenv), found(exact, uenv))
  tag = HEAD + "#SBATCH --uenv=prgenv-gnu/24.11\n#SBATCH --view=default\nsrun ./sim\n"
  check("a version without a tag: E06", any(i == "E06" and "without tag" in e for i, e in found(tag, uenv)), found(tag, uenv))


def test_the_catalogue_says_which_apply(check):
  md = c.rules_markdown(facts(["uenv"]), "daint")
  e01 = md.split("### E01 ", 1)[1].split("### ", 1)[0]
  e06 = md.split("### E06 ", 1)[1].split("### ", 1)[0]
  check("a module rule on a uenv cluster: not active there, said so", "Not active on daint: it has no modules" in e01
        and "only on clusters with environment modules" in e01, e01[:300])
  check("a uenv rule there: active", "Not active" not in e06 and "only on clusters with uenv" in e06, e06[:300])
  check("the stacks of the cluster, at the top", "daint has uenv." in md)
  check("without facts: nothing is said inactive", "Not active" not in c.rules_markdown())

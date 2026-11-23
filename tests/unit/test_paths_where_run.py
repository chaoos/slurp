"""The paths of a script are checked where each line runs: --chdir (or where sbatch runs), then the script's cd and
pushd, a launcher's --chdir (srun --chdir/-D, mpirun -wdir) for its program; after a cd to a place not known, relative
paths are not checked (until a cd to a known one)"""
from slurp.checks import engine as c
from slurp.checks.engine import Facts, PathInfo

HOME = "/users/me"


def paths(body, chdir="/scratch/me/base"):
  text = "#!/bin/bash\n" + (f"#SBATCH --chdir={chdir}\n" if chdir else "") + body + "\n"
  ctx = c.make_context(text, "/users/me/src/job.sh", facts=Facts(env={"HOME": HOME}))
  return [(k, p) for k, p, _ in c.referenced_paths(ctx)], ctx


def test_where_each_line_runs(check):
  got, _ = paths("srun ./sim")
  check("--chdir: from there", ("x", "/scratch/me/base/sim") in got, got)
  got, _ = paths("cd /scratch/me/run\nsrun ./sim\ncd sub\n./post")
  check("after a cd: from its directory (not from --chdir)", ("x", "/scratch/me/run/sim") in got and ("x", "/scratch/me/base/sim") not in got, got)
  check("…a relative cd: from the directory before", ("d", "/scratch/me/run/sub") in got and ("x", "/scratch/me/run/sub/post") in got, got)
  got, _ = paths("cd /scratch/me/run && srun ./sim")
  check("cd && program on one line", ("x", "/scratch/me/run/sim") in got, got)
  got, _ = paths("pushd /opt/run\n./a\npopd\n./b")
  check("pushd: as cd; after popd: not checked", ("x", "/opt/run/a") in got and not any(p.endswith("/b") for _, p in got), got)
  got, _ = paths("cd\n./inhome\ncd ~\n./again")
  check("cd, cd ~: home", ("x", f"{HOME}/inhome") in got and ("x", f"{HOME}/again") in got, got)
  got, _ = paths("cd $NOT_KNOWN\n./x\n/abs/y\ncd /scratch/me/z\n./w")
  check("a cd to a place not known: relative paths not checked, absolute ones still; a known cd again: checked",
        [p for _, p in got if p.endswith(("/x", "/y", "/w"))] == ["/abs/y", "/scratch/me/z/w"], got)
  got, _ = paths("cd -\n./x")
  check("cd -: not followed", not any(p.endswith("/x") for _, p in got), got)


def test_a_launchers_directory(check):
  got, _ = paths("srun --chdir=/opt/x ./tool\nsrun --chdir /opt/y ./tool\nsrun -D /opt/z ./tool\nsrun -D/opt/w ./tool")
  check("srun --chdir=, --chdir x, -D x, -Dx: its program from there",
        [p for k, p in got if k == "x"] == ["/opt/x/tool", "/opt/y/tool", "/opt/z/tool", "/opt/w/tool"], got)
  got, _ = paths("cd /scratch/me/run\nsrun --chdir=sub ./tool\n./next")
  check("…relative: from the script's directory; only for that line", ("x", "/scratch/me/run/sub/tool") in got
        and ("x", "/scratch/me/run/next") in got, got)
  got, _ = paths("mpirun -wdir /opt/m -np 4 ./app")
  check("mpirun -wdir", ("x", "/opt/m/app") in got, got)
  got, _ = paths("srun ./tool -D /elsewhere")
  check("the program's own -D: not the launcher's", ("x", "/scratch/me/base/tool") in got, got)


def test_guessed_and_submit_dir(check):
  got, ctx = paths("srun ./sim", chdir="")
  check("no --chdir, no job: from the script's directory, a guess (said)", ("x", "/users/me/src/sim") in got
        and "/users/me/src/sim" in ctx.uncertain, (got, ctx.uncertain))
  got, ctx = paths("cd /scratch/me/run\nsrun ./sim", chdir="")
  check("…after a cd to an absolute directory: no guess", ("x", "/scratch/me/run/sim") in got and not ctx.uncertain, (got, ctx.uncertain))
  got, ctx = paths('cd "$SLURM_SUBMIT_DIR"/run\nsrun ./sim', chdir="")
  check("cd $SLURM_SUBMIT_DIR: where it is submitted (the script's directory, a guess)", ("x", "/users/me/src/run/sim") in got
        and "/users/me/src/run/sim" in ctx.uncertain, (got, ctx.uncertain))


def test_no_false_error_after_cd(check):
  text = "#!/bin/bash\n#SBATCH --chdir=/scratch/me/base\ncd /scratch/me/run\nsrun ./sim\n"
  probes = {"x:/scratch/me/run/sim": PathInfo(True, False, True, True, 100, 0, "ELF 64-bit"),
            "x:/scratch/me/base/sim": PathInfo(False), "d:/scratch/me/run": PathInfo(True, True, True, True),
            "d:/scratch/me/base": PathInfo(True, True, True, True)}
  findings, _ = c.check_script_text(text, "/users/me/src/job.sh", facts=Facts(env={"HOME": HOME}), probes=probes)
  check("P03: the program after the cd is there: no error (it was checked in --chdir before)", not [f for f in findings if f.rule.id == "P03"],
        [f.message for f in findings if f.rule.id == "P03"])

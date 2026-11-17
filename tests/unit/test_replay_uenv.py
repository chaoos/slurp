"""P05 (shared libraries, ldd) looks at the program in the job's environment: inside its uenv (--uenv, --view: a stub
uenv here, which puts the uenv's library on the path as a view does), never without it (no uenv command, or it
fails: not checked rather than every library missing); in a container not at all"""
import shutil
import stat
import subprocess

import pytest

from slurp.checks import engine as c
from slurp.checks import environment
from slurp.checks.engine import Facts, Partition

STUB = """#!/bin/bash
# uenv run [--view=V] IMAGE -- CMD...: as the real one, CMD runs with the uenv's view set up
echo "$@" >> "$UENV_LOG"
if [ "$1 $2" = "image ls" ]; then
  echo "uenv                    arch   system  id                size(MB)  date"
  cat "$UENV_PULLED" 2>/dev/null; exit 0
fi
[ "$1" = run ] || exit 2; shift
view=""; case "$1" in --view=*) view=${1#--view=}; shift;; esac
image=$1; shift; [ "$1" = -- ] && shift
grep -q "^${image%%:*}" "$UENV_PULLED" 2>/dev/null || { echo "uenv: no image $image in the repository" >&2; exit 1; }
[ "$image" = broken/1.0:v1 ] && { echo "uenv: cannot mount broken/1.0:v1: the squashfs file is damaged" >&2; exit 1; }
[ "$view" = default ] && export LD_LIBRARY_PATH="$UENV_LIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$@"
"""


@pytest.fixture
def world(tmp_path):
  if not shutil.which("gcc") or not shutil.which("ldd"):
    pytest.skip("needs gcc and ldd")
  libs, bin_ = tmp_path / "uenv-libs", tmp_path / "bin"
  libs.mkdir(); bin_.mkdir()
  (tmp_path / "foo.c").write_text("int foo(void) { return 42; }\n")
  (tmp_path / "sim.c").write_text("int foo(void);\nint main(void) { return foo() - 42; }\n")
  subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(libs / "libuenvfoo.so"), str(tmp_path / "foo.c")], check=True)
  subprocess.run(["gcc", "-o", str(tmp_path / "sim"), str(tmp_path / "sim.c"), f"-L{libs}", "-luenvfoo"], check=True)
  stub = bin_ / "uenv"
  stub.write_text(STUB); stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
  (tmp_path / "pulled").write_text("prgenv/24.11:v1         gh200  daint   b5f8d5b2a7c5e3d1  4,123     2024-12-05\n"
                                   "broken/1.0:v1           gh200  daint   0123456789abcdef  100       2024-12-05\n")
  return tmp_path


def replay(world, script, uenv_on_path=True):
  facts = Facts(partitions={"normal": Partition("normal", True, 1440, 288, 460000, 4, 4, 72, 1, False)}, accounts=["p"],
                sbatch_options=c.LONG_OPTIONS | {"uenv", "view", "environment"}, env={"HOME": str(world), "USER": "me"},
                user="me")
  ctx = c.make_context(script, str(world / "job.sh"), facts=facts)
  path = (f"{world / 'bin'}:" if uenv_on_path else "") + "/usr/bin:/bin"
  env = {"PATH": path, "HOME": str(world), "UENV_LOG": str(world / "uenv.log"), "UENV_LIBS": str(world / "uenv-libs"),
         "UENV_PULLED": str(world / "pulled")}
  res = subprocess.run(["bash", "-c", environment.replay_command(ctx)], env=env, capture_output=True, text=True, timeout=60)
  ctx.replay = environment.replay_result(ctx, res)
  findings, _ = c.check_script_text(script, str(world / "job.sh"), facts=facts, replay=ctx.replay)
  return ctx.replay, [f for f in findings if f.rule.id in ("P05", "E04", "E05")]


def script(*directives):
  return "#!/bin/bash -l\n#SBATCH --time=00:10:00\n" + "".join(f"#SBATCH {d}\n" for d in directives) + "srun ./sim\n"


def test_p05_in_the_jobs_uenv(check, world):
  r, f = replay(world, script())
  check("without a uenv: the library is missing (P05 error)", r["missing"].get(str(world / "sim")) == ["libuenvfoo.so"]
        and any(x.severity == "error" for x in f), [(x.rule.id, x.evidence) for x in f])
  r, f = replay(world, script("--uenv=prgenv/24.11:v1", "--view=default"))
  log = (world / "uenv.log").read_text()
  check("--uenv, --view: the replay runs in the uenv (uenv run --view=default prgenv/24.11:v1 -- bash -lc …)",
        "run --view=default prgenv/24.11:v1 -- bash -lc" in log and r["uenv_started"], log)
  check("... with the view's libraries: nothing missing, no P05", r["libs"].get(str(world / "sim")) and not r["missing"].get(str(world / "sim"))
        and not f, ([(x.rule.id, x.evidence) for x in f], r["libs"]))
  r, f = replay(world, script("--uenv=prgenv/24.11:v1", "--view=default"), uenv_on_path=False)
  check("no uenv command on the login node: not checked (info), no missing library", not r["missing"]
        and [(x.rule.id, x.severity) for x in f] == [("P05", "info")] and "not checked" in f[0].evidence, [(x.rule.id, x.severity, x.evidence) for x in f])
  r, f = replay(world, script("--uenv=broken/1.0:v1"))
  check("a pulled uenv that does not start: not checked (warning, with uenv's error)", [(x.rule.id, x.severity) for x in f]
        == [("P05", "warning")] and "squashfs file is damaged" in f[0].evidence, [(x.rule.id, x.evidence) for x in f])
  r, f = replay(world, "#!/bin/bash\n#SBATCH --uenv=prgenv/24.11:v1\n#SBATCH --view=default\nsrun ./sim\n")
  check("a script that is no login shell: bash -c in the uenv", (world / "uenv.log").read_text().rsplit("run --view=", 1)[1].startswith("default prgenv/24.11:v1 -- bash -c "))


def test_in_a_container_the_programs_are_not_looked_at(check, world):
  r, f = replay(world, "#!/bin/bash -l\n#SBATCH --environment=pytorch\nmodule load no-such-module/1\nsrun ./sim\n")
  check("--environment: no program looked at, no P05, no E04 (its modules are in the container)",
        r["container"] == "environment" and not r["libs"] and not f, ([(x.rule.id, x.evidence) for x in f], r))


def test_environment_activations_are_replayed(check):
  text = ("#!/bin/bash -l\nsource $HOME/miniforge/etc/profile.d/conda.sh\nconda activate sim-env\nmamba activate other\n"
          "spack load hdf5\nspack env activate ./env\npython run.py\nconda install numpy\n")
  lines = [t for _, t in environment.setup_lines(c.make_context(text, "/x/job.sh").script)]
  check("conda/mamba activate and spack load / env activate are set up before ldd; installing is not run",
        lines == ["source $HOME/miniforge/etc/profile.d/conda.sh", "conda activate sim-env", "mamba activate other",
                  "spack load hdf5", "spack env activate ./env"], lines)


def test_e05_a_uenv_that_is_not_pulled(check, world):
  r, f = replay(world, script("--uenv=prgenv/25.6:v2", "--view=default"))
  check("not pulled: E05 (error) with the pull command, on the --uenv line; no P05 on top", [(x.rule.id, x.severity) for x in f]
        == [("E05", "error")] and "prgenv/25.6:v2" in f[0].evidence and f[0].snippet == "uenv image pull prgenv/25.6:v2"
        and f[0].line == 3, [(x.rule.id, x.evidence, x.line) for x in f])
  r, f = replay(world, script("--uenv=prgenv/24.11:v1", "--view=default"))
  check("pulled: no E05", not f and r["uenv_not_pulled"] == [], [(x.rule.id, x.evidence) for x in f])
  images = environment.pulled_uenvs(["uenv   arch   system  id   size(MB)  date",
                                     "prgenv-gnu/24.11:v1   gh200  daint   b5f8d5b2a7c5e3d1  4,123  2024-12-05",
                                     "editors/24.7:v2       gh200  daint   1111222233334444  200    2024-08-01"])
  for value, want in [("prgenv-gnu/24.11:v1", []), ("prgenv-gnu/24.11", []), ("prgenv-gnu", []), ("prgenv-gnu/24.11:v1@daint", []),
                      ("prgenv-gnu/24.11:v1@clariden", ["prgenv-gnu/24.11:v1@clariden"]), ("prgenv-gnu/24.11:v1%gh200", []),
                      ("b5f8d5b2a7c5e3d1", []), ("ffff5b2a7c5e3d10", ["ffff5b2a7c5e3d10"]),
                      ("prgenv-gnu/24.11:v1:/user-environment,editors/24.7:v2:/user-tools", []),
                      ("prgenv-gnu/24.11:v1,netcdf/4.9:v1:/user-tools", ["netcdf/4.9:v1"]),
                      ("/scratch/me/store.squashfs:/user-environment", []), ("prgenv-gnu/25.6:v1", ["prgenv-gnu/25.6:v1"])]:
    check(f"--uenv={value}: not pulled {want}", environment.not_pulled(value, images) == want, environment.not_pulled(value, images))
  check("the header of uenv image ls is no image", [i["name"] for i in images] == ["prgenv-gnu", "editors"]
        and images[0]["system"] == "daint" and images[0]["arch"] == "gh200" and images[0]["id"] == "b5f8d5b2a7c5e3d1", images)

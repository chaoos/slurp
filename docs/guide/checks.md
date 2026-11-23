# Checks and the rule catalogue

slurp checks batch scripts and jobs against a catalogue of rules: beginner
mistakes and classic Slurm traps, requests that make no sense, paths and
programs that do not exist, full quotas, the software environment, NUMA
placement, and what a job really used. Every finding says what was found
(with the line of the script), why it matters and how to fix it, often with
a snippet to copy, and links to its rule in the **rule catalogue** (`F2`; `/` searches it), a
subtab of its own (one per tab: a later rule link goes there and jumps to the
rule; the guides, `F1`, have theirs too). Code in the catalogue, the guides and
the help (and in Markdown files) shows as a framed, highlighted block.
The rules, by category:

| | Category | Examples |
|---|---|---|
| S | Script syntax and hygiene | Windows line endings, `#SBATCH` after the first command (ignored), misspelled options, `$VARS` in `#SBATCH`, `cd` without a check, background programs without `wait`, variables nobody sets, Slurm variables the job does not get (`$SLURM_ARRAY_TASK_ID` without `--array`, `$SLURM_CPUS_PER_TASK` without `--cpus-per-task`) or misspelled |
| R | Requests that make no sense | `--mem=4` (4 MB), `--time=2:00` (2 minutes), more CPUs/GPUs/memory than a node has, time over the partition's maximum, unknown partition or account |
| B | Beginner mistakes | several nodes but no `srun`, arrays that never use `$SLURM_ARRAY_TASK_ID`, logs without `%j`, results left in the node's `/tmp`, `srun` in a loop, the same job twice, dependencies that can never be met, heavy work on the login node |
| P | Paths, files, programs | the log directory does not exist (the job fails without a log), missing directories and inputs, programs missing or not executable, built for another CPU, shared libraries not found, GPU code for other GPUs, changed since submission, the program of many tasks in home or project instead of scratch; relative paths are checked where each line runs (`--chdir` or where sbatch runs, then the script's `cd` and `pushd`, `srun --chdir`/`-D` and `mpirun -wdir` for the program they start; after a `cd` to a place slurp cannot know, such as `cd -` or an unknown variable, relative paths are not checked until the next known `cd`) |
| Q | Quota and storage | writing where the quota (space or files) is full, little room left, output in `$HOME`, inputs close to the scratch purge, very many files in one directory, logs in home or project instead of scratch, core dumps in home (where sbatch was run, `--chdir` or a `cd` there, with core files allowed; core files already there) |
| E | Environment | modules without versions or `module purge`, `conda activate` without the hook, `pip install` in jobs, modules that cannot be loaded, site advice; uenv not pulled, without version and tag, without a view, `module load` without the modules view |
| N | NUMA placement | tasks not aligned with NUMA domains, threads and tasks not bound, GPUs not assigned to tasks, `srun` without `--cpus-per-task` (Slurm 22.05+) |
| U | Usage and outcome | much more time or memory than used, low CPU usage, why a job failed (OOM, TIMEOUT, exit codes), why it waits (pending reasons), known errors in the logs, long jobs without checkpoints |
| G | Good practice | the script sets up its own environment instead of relying on the shell it is submitted from (compared with the environment Slurm kept of the submission, `sacct --env-vars`, where the cluster stores it, and by replaying its setup after a fresh login); failures that look like successes (`echo done` after `srun`, a failed step under a COMPLETED job); array logs without both `%A` and `%a` (the tasks race on one file, or the next run overwrites them); not submitting from inside a job; task and CPU counts from Slurm, not literal; runs that overwrite each other's files; a record of what ran; no interactive commands; the script works from any directory (relative paths before it sets its directory; relative logs with a `cd` but no `--chdir`); `$0` is Slurm's copy of the script |

**In the job list** the **Checks** column shows the errors (✖) and warnings
(⚠) of each job: the scripts of queued jobs, the records of all, and the paths
they name (checked on the cluster in one round trip, after the list is shown).
`!` shows only the jobs with problems.

**In the job window** the **Checks** section lists the findings, the most
severe first; each opens to show why it matters, how to fix it and the link
to the catalogue. It checks the script as submitted (or as the accounting
stored it), the record, the paths and programs, your quota where the job
writes, and the end of its logs, and then, a few seconds later, its
environment: the script's setup lines (module loads, exports, sources, `cd`,
`conda`/`mamba activate`, `spack load`;
never its commands) are replayed on the login node in a clean environment, as
after a fresh login, and the programs are checked: found at all (G01), shared
libraries (`ldd`), GPU code (`cuobjdump`). A job with a uenv (`--uenv`,
`--view`, on Alps) gets it for the replay too: the setup and the checks run in
`uenv run` with the same uenv and view (in a login shell if the script's `#!`
line asks for one). Before that, `uenv image ls` shows whether the uenv is
pulled: a job cannot use one that is not, and E05 says so, with the
`uenv image pull` command. If the login node has no `uenv` command or the uenv does
not start, the libraries and module loads are not checked (P05 says so) rather
than all missing or unknown (P05, E04).
In a container (`--environment`, `--container-image`) the programs are not
looked at: the login node does not have what the container has. **Check** runs
all of it again.
Its other buttons:

- **Placement** (running jobs) asks the nodes where the tasks and threads run,
  draws each node by NUMA domain with its GPUs, and names the problems: shared
  cores, unbound tasks, tasks spread over domains, GPUs of another domain.
- **Fixed copy…** writes a copy of the script with the fixes that are safe to
  make automatically (line endings, ignored `#SBATCH` lines moved up, `%j` in
  log names, `SRUN_CPUS_PER_TASK`, OpenMP binding, `module purge`, `wait`, …),
  shown as a diff first. Ambiguous ones (is `--mem=64` GB or MB?) stay with
  you. Submit the copy from the script view.
- **What changed** (failed jobs) compares the script, the command line and the
  request with the last run of the same job that worked.

**A batch script opened in the file browser** (a file with `#SBATCH` lines)
opens in the **script view**: the script with its findings below, each with
*Go to line*. **Explain** (`x`) annotates every `#SBATCH` line with what
Slurm makes of it (units, per node, the paths of the logs, IGNORED after the
first command). **Edit** (`e`) makes it editable right there, with Textual's
editor: the findings follow as you type (also the paths, directories and
programs on the cluster, a moment after you pause), `Ctrl+S` saves, `Ctrl+Z` undoes, **Reload** (`Ctrl+R`, also when not editing) reads the file again (into the editor while editing, asking about unsaved changes), `Esc`
stops editing; **Check** checks the edited text, saved or not; unsaved changes are asked about when you stop, close the subtab
or quit, and Save asks before it overwrites a file changed meanwhile. Bash is
highlighted with Textual's syntax extra (`pip install "textual[syntax]"`).
**Edit outside** opens your `$VISUAL`/`$EDITOR` instead. **Test** runs `sbatch --test-only` (accepted? when would it
start?). **Submit…** names the errors found and shows the `sbatch` command, which
you can edit (more options, a dependency, …); **Array** makes it an array job
(`1-100`, `0-9:2`, `1,5,7`, `%50` for at most 50 at a time): it writes the
command's `--array`, says how many tasks that is and which ids (the first and
the last: with a step the last is not always the end, `1-100:2` ends at 99,
which it warns of), and warns when the script never uses
`$SLURM_ARRAY_TASK_ID`.

**New job script** (`n` in the job list) writes a commented script for the
usual kinds of jobs (serial, OpenMP, MPI, hybrid MPI+OpenMP, GPU, array,
dependency chain, long run with checkpoints), with the numbers of the
cluster's partitions (cores, NUMA domains, GPUs), creates `logs/` and opens it
in the script view.

**The guides** (`F1`) explain the background with this cluster's numbers:
Slurm in five minutes, writing a job script, common mistakes, arrays and
dependencies, software, filesystems and quotas, NUMA placement (a recipe
table for the node), GPUs, failed jobs. Their rule links open the catalogue.
**Health** (`C`) shows the node topology of every partition.

**Muting**: a finding's *Mute everywhere* stores the rule in
`"checks_muted"` in the config; *Mute in this script* (script view) adds
`# slurp: ignore R08` to the script, which works for any rule.

**Software stacks:** most rules apply on every cluster; some are about one
software stack only. The module rules (E01, E04) apply on clusters with
environment modules (Lmod or modules in a login shell), the uenv rules (E05 to
E08) on clusters with uenv (`sbatch --uenv`, or the `uenv` command). A module
rule also applies to a uenv's modules view (`--view=modules`), in part: E01
asks for versions there, but not for `module purge` (the modules come from the
uenv, not from your login). slurp finds the stacks with the other facts of the
cluster; while it does not know them, all rules apply. The rule catalogue says
for each rule where it applies, and greys out the ones that do not apply on
the tab's cluster.

**Site rule packs** add what Slurm does not tell: scratch purge days, parallel
filesystems, NUMA domains per node, the GPU architecture, site plugin options
of `sbatch`, the software stacks (`stacks`, if not found), rules that do not apply, and site advice. A pack is a TOML file
in `src/slurp/sites/packs/` (included: CSCS Alps, and JSC's JURECA, JUWELS, JUSUF and
JEDI) or in
`~/.config/slurp/rules/`; it applies when one of its `match` patterns matches
the tab, its SSH destination or the login node's host name. It can also name
the site's documentation (`docs`, for the guides) and its status page
(`status`, shown when the login fails). See `src/slurp/sites/__init__.py` for the keys.

Each pack has a `schema` (the version of its format: slurp leaves out a pack
of a newer schema than it reads; without one: 1) and a `version` (the date
of its content, `YYYY.MM.DD`). The rule catalogue and the overview of a tab say
which pack applies there, its version, and whether it is built in or yours;
About slurp (`?` → slurp) and `slurp --version` list all packs slurp reads.

**Without the UI**, `slurp check job.sh ...` prints the findings of batch
scripts (on the machine it runs on, e.g. a login node): exit code 1 if a script
has errors; `--all` adds the hints, `--no-probe` checks only the text,
`--rules` prints the catalogue.

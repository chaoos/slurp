<p align="center"><img src="slurp/docs/logo-wordmark.svg" alt="slurp: Slurm at a glance" width="300"></p>

# slurp

`slurp` is a Textual interface for checking Slurm jobs and inspecting the files
and resources associated with them: queued, running and finished jobs, their
output and scripts, the nodes they run on, checks of batch scripts against a
rule catalogue, and shells and drives on the cluster. Use it in a terminal or
in a browser.

![Illustrative slurp job list with status and progress columns](slurp/docs/jobs.svg)

## Quick start

```bash
git clone https://github.com/chaoos/slurp.git
cd slurp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ".[all]"   # or just "." without the optional features
slurp                            # or: python -m slurp
```

Without cloning: `pipx install "slurp[all] @ git+https://github.com/chaoos/slurp"`.
Run it on a login node with Slurm, or on your own machine with an SSH
connection to one: `slurp --ssh user@cluster-login`.

## Documentation

The manual is [slurp/README.md](slurp/README.md). In the app, the `?` buttons
explain each window and `F1` opens the guides (job scripts, arrays, filesystems,
GPUs, ...). What changed between versions is in
[slurp/CHANGELOG.md](slurp/CHANGELOG.md).

## Tests

```bash
python -m pip install -e ".[all,dev]"
python -m pytest -m "not lab"   # unit tests: seconds, nothing needed beyond pip
python -m pytest -m lab         # the lab: slurp against a fake Slurm over SSH (~10 min)
python -m pytest                # both
```

The lab tests start their own `sshd` on a free port of 127.0.0.1, with a fake
Slurm and fresh keys in pytest's temporary directory. They need OpenSSH
(`ssh`, `sshd`, `ssh-keygen`) and `sshfs` (Debian/Ubuntu: `apt install
openssh-server sshfs`); without `sshd` they are skipped. They never touch
your `~/.ssh` or slurp's settings. Each scenario writes its output, log and
screenshots to `<basetemp>/lab0/out/<scenario>/` (set the place with
`--basetemp=DIR`). Parts of the long UI scenario can be run on their own:
`ONLY=t_categories,t_sort python -m pytest -k "tui and not drive"`, or left
out with `SKIP=t_sort`. See [tests/conftest.py](tests/conftest.py) and
[tests/lab](tests/lab/__init__.py).

The same runs on GitHub Actions ([.github/workflows/tests.yml](.github/workflows/tests.yml)):
the unit tests on Python 3.11 to 3.13, the lab on 3.12.

## License

GPL-3.0, see [LICENSE](LICENSE).

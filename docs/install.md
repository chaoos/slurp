# Install and start

slurp needs Python 3.11 or newer. Run it on a login node (or another host with Slurm), or on your own machine with an
SSH connection to one: the cluster then needs nothing but Slurm, a POSIX shell and the usual Linux tools (no Python,
no copy of slurp).

## Install

Into a virtual environment, from a clone:

```bash
git clone https://github.com/chaoos/slurp.git
cd slurp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ".[all]"   # or "." without the optional features
```

Or without a clone, in an environment of its own:

```bash
pipx install "slurp[all] @ git+https://github.com/chaoos/slurp"
```

`.[all]` includes the optional features:

| Extra | What it adds | Package |
|---|---|---|
| `links` | [shells and drives on your own machine](remote/links.md), [remote web access](remote/web-access.md) | asyncssh |
| `hdf5` | the [HDF5 view](guide/files.md) | h5py |
| `web` | [`--serve` and Textual Web](remote/browser.md) | textual-serve, textual-web |

Without `h5py` the HDF5 view shows `h5dump -H` when `h5dump` is installed. System commands such as `ssh`, `squeue`,
`sacct`, `scontrol`, `srun` and `sbatch` are installed separately and must be there locally or on the SSH host.

## Start

```bash
slurp                              # or: python -m slurp
slurp --ssh user@cluster-login     # a tab for a cluster (several destinations: several tabs)
slurp check job.sh                 # check batch scripts without the app
slurp --help                       # all options
```

`slurp` runs in the background, like tmux: running it again (in another terminal, or after an SSH connection
dropped) attaches to the slurp that runs already (see [One slurp, several views](remote/web-access.md)). `--new`
starts a separate one, `--no-holder` runs it directly in the terminal. `--log FILE` logs Slurm commands and debug
events. slurp needs no configuration file: tabs, jobs and files are chosen in the app, and the
[settings](guide/settings.md) it keeps are made there too.

## Later: running and updating

The `slurp` command lives in the virtual environment: activate it first (`source .venv/bin/activate`), or run
`.venv/bin/slurp`. To update a clone: `git pull`, then `python -m pip install ".[all]"` again (not needed for an
editable install, `-e`, see [Development](development.md)). After an update, quit the slurp that runs (`q`) and start
it again, or start a separate one with `slurp --new`: a new slurp that finds an older one running says so before it
attaches.

When using tmux, enable the mouse (`set -g mouse on`) to click and scroll in slurp.

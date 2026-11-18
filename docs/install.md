# Install and start

slurp needs Python 3.11 or newer. Run it on a login node (or another host with Slurm), or on your own machine with an
SSH connection to one: the cluster then needs nothing but Slurm, a POSIX shell and the usual Linux tools (no Python,
no copy of slurp).

## Install

slurp is on PyPI as [`slurp-ui`](https://pypi.org/project/slurp-ui/) (the command is `slurp`). In an environment of
its own, with [pipx](https://pipx.pypa.io/):

```bash
pipx install "slurp-ui[all]"
```

Or into a virtual environment:

```bash
python3 -m venv ~/.venvs/slurp
~/.venvs/slurp/bin/python -m pip install "slurp-ui[all]"   # or "slurp-ui" without the optional features
~/.venvs/slurp/bin/slurp
```

The newest code, not released yet, from a clone:

```bash
git clone https://github.com/chaoos/slurp.git
cd slurp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ".[all]"
```

`[all]` includes the optional features:

| Extra | What it adds | Package |
|---|---|---|
| `links` | [shells and drives on your own machine](remote/links.md), [remote web access](remote/web-access.md) | asyncssh |
| `hdf5` | the [HDF5 view](guide/files.md) | h5py |
| `web` | [`--serve` and Textual Web](remote/browser.md) | textual-serve, textual-web |
| `transfers` | [S3 buckets, SMB shares, WebDAV folders](guide/transfers.md#s3-smb-and-webdav) and [Globus collections](guide/transfers.md#globus) as endpoints of transfers, secrets in the system keyring (S3, SMB and WebDAV also need [rclone](https://rclone.org/install/)) | keyring, globus-sdk |

Without `h5py` the HDF5 view shows `h5dump -H` when `h5dump` is installed. System commands such as `ssh`, `squeue`,
`sacct`, `scontrol`, `srun` and `sbatch` are installed separately and must be there locally or on the SSH host.

## Start

```bash
slurp                              # or: python -m slurp; + in the app adds a tab for a cluster
slurp check job.sh                 # check batch scripts without the app
slurp --help                       # all options
```

`slurp` runs in the background, like tmux: running it again (in another terminal, or after an SSH connection
dropped) attaches to the slurp that runs already (see [One slurp, several views](remote/web-access.md)). `--new`
starts a separate one, `--no-holder` runs it directly in the terminal. `--log FILE` logs Slurm commands and debug
events. slurp needs no configuration file: tabs, jobs and files are chosen in the app, and the
[settings](guide/settings.md) it keeps are made there too.

## The icon font

slurp shows small icons in front of tabs, buttons, menu items, job states and files. A terminal draws them from
slurp's icon font (8 KB, every icon one cell wide); install it once on the machine of the terminal you look
through, which is your own computer, also when slurp runs on a cluster over SSH:

```bash
slurp --install-font               # Linux, macOS: into your fonts; Windows: opens the font to install
```

Then open a new terminal window. Your terminal's font stays: the terminal takes only the icons from slurp's font.
Without slurp installed on that machine, download
[slurp-icons.ttf](https://github.com/chaoos/slurp/raw/main/src/slurp/ui/fonts/slurp-icons.ttf) and install it like any
font. A terminal with a [Nerd Font](https://www.nerdfonts.com/) shows the icons too, and a browser has them already.
Without the font the icons are boxes; **Options** → **Icons** switches them off (see [Settings](guide/settings.md#icons)).

## Later: running and updating

pipx puts `slurp` on your PATH; from a virtual environment, activate it first or run its `bin/slurp`. To update:
`pipx upgrade slurp-ui`, or `python -m pip install -U "slurp-ui[all]"` in the environment. To update a clone:
`git pull`, then `python -m pip install ".[all]"` again (not needed for an editable install, `-e`, see
[Development](development.md)). After an update, quit the slurp that runs (`q`) and start
it again, or start a separate one with `slurp --new`: a new slurp that finds an older one running says so before it
attaches.

slurp tells you itself when there is a newer version: at the start it asks PyPI (at most once a day, in the
background; offline nothing happens), says so, and the title bar shows **⬆ slurp <version>** until it quits. A click
shows what changed, how slurp is installed on this machine (pipx, uv tool, pip, or a git checkout) and the command
that updates it, to copy; then quit slurp and start it again. **Skip this version** stops the reminder until a newer
one; `"update_check": false` in the config stops the check.

When using tmux, enable the mouse (`set -g mouse on`) to click and scroll in slurp.

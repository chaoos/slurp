"""slurp: an interactive terminal UI for Slurm jobs, locally or on clusters over SSH.

Browse queued, running and finished jobs, their details, steps and files (with tail -f style following), run
commands and shells on the nodes of running jobs, submit interactive jobs, and see the state of the cluster.
Run it with the `slurp` command (see pyproject.toml) or `python -m slurp`.
"""

# the version of slurp and of its Windows helper (one number for both; CHANGELOG.md says what changed)
__version__ = "0.8.0"
RELEASE = "beta"
VERSION_TEXT = f"{__version__} ({RELEASE})" if RELEASE else __version__

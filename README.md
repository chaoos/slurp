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
python -m pip install -r requirements.txt
python3 slurp          # or: python3 -m slurp
```

Run it on a login node with Slurm, or on your own machine with an SSH
connection to one: `python3 slurp --ssh user@cluster-login`.

## Documentation

The manual is [slurp/README.md](slurp/README.md). In the app, the `?` buttons
explain each window and `F1` opens the guides (job scripts, arrays, filesystems,
GPUs, ...). What changed between versions is in
[slurp/CHANGELOG.md](slurp/CHANGELOG.md).

## License

GPL-3.0, see [LICENSE](LICENSE).

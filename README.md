<p align="center"><img src="docs/images/logo-wordmark.svg" alt="slurp: All of slurm in one slurp" width="300"></p>

# slurp

`slurp` is a Textual interface for checking Slurm jobs and inspecting the files
and resources associated with them: queued, running and finished jobs, their
output and scripts, the nodes they run on, checks of batch scripts against a
rule catalogue, and shells and drives on the cluster. Use it in a terminal or
in a browser.

**Documentation: <https://chaoos.github.io/slurp/>**

![The job list: queued, running and finished jobs, progress and estimates, an array of 150 tasks as one row](docs/images/jobs.svg)

## Quick start

slurp needs Python 3.11 or newer:

```bash
git clone https://github.com/chaoos/slurp.git
cd slurp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ".[all]"
slurp                         # on a login node, or on your own machine
                              # (+ adds a tab for a cluster over SSH)
slurp --install-font          # once, where your terminal runs: its icons
```

More in [Install and start](https://chaoos.github.io/slurp/install/). In the
app, `?` explains the window shown and `F1` opens the guides.

## Links

- [Documentation](https://chaoos.github.io/slurp/) (the pages are in [docs/](docs/))
- [Changelog](CHANGELOG.md)
- [Development](https://chaoos.github.io/slurp/development/): editable install, the tests, CI

## License

GPL-3.0, see [LICENSE](LICENSE).

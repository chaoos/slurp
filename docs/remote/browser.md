# Browser access

## Local browser or SSH tunnel

Install `textual-serve` in the same Python environment and start the local
browser server:

```bash
python -m pip install textual-serve   # (included in .[all] and .[web])
slurp --serve
```

Open <http://localhost:8000>. The server binds to `127.0.0.1` by default;
`--host` and `--port` change the bind address and port. To access it through
two SSH proxies, run the server on the Slurm host and forward the port:

```bash
ssh -J user@jump1,user@jump2 -N \
  -L 8000:127.0.0.1:8000 user@slurm-login
```

Then browse to <http://localhost:8000> on your computer. Substitute your SSH
hostnames and usernames. If local port 8000 is occupied, use a different local
port, e.g. `-L 18000:127.0.0.1:8000`, and open `http://localhost:18000`.

## Random public link with Textual Web

If the Slurm host can make outbound internet connections, Textual Web can
relay a Textual app to a random public URL without an inbound port or SSH
forward. Install `textual-web`, then create a configuration file such as
`serve.toml`:

```toml
[app.Slurm]
command = "/path/to/venv/bin/slurp"
```

Run it from the project directory:

```bash
textual-web --config serve.toml
```

Textual Web prints a random URL that can be opened from a browser on another
network. Its documentation currently describes the project as beta. Anyone
with the URL can access the running app, so treat it as a secret bearer link:
the app can run Slurm actions and launch shells as the account running it.
Use this only when that access is appropriate. A permanent account URL is
available in Textual Web, but it is an address and should not be treated as a
secret. See the [Textual Web project](https://github.com/Textualize/textual-web)
for its current setup and access behavior.

Install Textual Web separately with `python -m pip install textual-web`.
Terminal suspension is unavailable in browser mode, so opening a file in
`less` is a terminal-only action.

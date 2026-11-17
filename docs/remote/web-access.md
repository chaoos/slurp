# One slurp, several views

`slurp` does not run slurp in the terminal itself: it starts a small
*session holder* that runs slurp in the background (like tmux), and this
terminal shows it. Running `slurp` again (in another terminal, or after the
first one was closed or its SSH connection dropped) attaches to the same
slurp instead of starting another one; slurp keeps running until you quit it
(`q`). `--new` starts a separate one, `--no-holder` runs directly in the
terminal as before (ending with it).

**This slurp in your browser** (in the menu, `W`) shows this same slurp in a browser, from
anywhere: it starts a web view on `127.0.0.1` (a random port and an access
token; nothing listens before you ask) and makes a *web link* through the
relay, like the links for shells and drives (relay, password or key, logins,
expiry, Revoke, listed in the connection window). The link only forwards to
the web view: no shell, no files. On your machine:

```bash
ssh -L 8000:127.0.0.1:<port> -J <name>@ssh-j.com slurp@slurp   # (the link window has the exact command)
```

It asks for the link's password, then says that the tunnel is open and
shows the URL (`http://localhost:8000/?token=…`). Keep that window open while
you use the browser: it carries the connection (Ctrl-C or `q` there closes
it). The page needs the URL with the token every time: `http://localhost:8000`
alone is refused, and nothing a browser kept from an earlier visit opens it
(each page load gets a ticket of its own, good for its one connection).

**Through Cloudflare** (the relay *Cloudflare* in the setup panel, for web
links only) there is nothing to do on your side: slurp runs Cloudflare's
tunnel client, `cloudflared`, which gets a random public address
(`https://<words>.trycloudflare.com`, a free *quick tunnel*, no account), and
the link window shows it with the token: open it in any browser, anywhere. No
ssh, no password, no helper. The address is the key (whoever has it gets
this slurp), so keep it to yourself, prefer a short expiry and revoke the
link when done. Cloudflare ends the HTTPS, so unlike an SSH relay it can read
what passes. A quick tunnel has no uptime guarantee; when it is lost slurp
opens a new one, at a new address. slurp shows the address once it is in
DNS (a browser that asks too early would keep "no such name" for minutes).
The first time, slurp asks, then downloads `cloudflared` (about 40 MB, from
Cloudflare's releases on GitHub) into `~/.local/share/slurp/bin` (the link
window shows *downloading cloudflared* and *installing cloudflared*), unless one is on `PATH`
or the config names one (`"cloudflared": "/path/to/cloudflared"`). It uses
HTTP/2 over TCP (port 7844), as QUIC (UDP) is often blocked: on Alps
(daint) it is. The web links remember their relay apart from shells and
drives (`link_relay_web`).

The browser is another view of the same slurp (textual-serve's page, a
terminal). **One view is active at a time**: its keys and mouse go to slurp,
and the screen takes its size. The others show a lock screen: who is active
(the terminal on which host, or which browser), since when, idle for how
long, its size, whether it is still connected, and the web links. **Take
over** (Enter, or a click) asks "are you sure?" and makes this view the
active one; the other one gets the lock screen, saying who took over.
**Watch** (`w`) shows the screen live and read only instead of the lock
screen, **Detach** (`d`) closes the view (slurp goes on). When the active view
goes away (a browser tab closed, a terminal gone), another one becomes active
by itself. Quitting asks when other views are attached, and closes them all.
When a web link ends, the title bar counts down to it (⏱, left of **?**; a click
opens the link): when it takes no more logins (its time limit), or when its relay
ends the tunnel (the free pinggy relay keeps one for an hour, then a new address).
Five minutes before, slurp warns: a time limit can be extended by an hour right
there; the relay's hour cannot (a relay without one, Cloudflare or your own,
avoids it).

When no web link is left (revoked, expired), the web view stops listening.
In the connection window's Links, the session of the browser you are on is
marked **◀ this view**. Disconnecting it, or revoking its link, warns first
that this ends the very view you are using.

`--serve` ([Browser access](../remote/browser.md)) is different: every browser tab gets its own new slurp.

"""SSH destinations: the full settings of a tab as ssh options, the hosts of ~/.ssh/config, and what the check makes
of ssh's verbose output"""
import os

from slurp.cluster import sshsetup as s


def test_settings_to_ssh_options(check):
  settings = s.clean({"host": " daint.alps.cscs.ch ", "user": "me", "port": "2222", "jump": "me@ela.cscs.ch",
                      "identities": ["~/.ssh/cscs-key", " "], "options": ["ForwardAgent=yes", "ServerAliveInterval 30", ""],
                      "name": ""})
  check("clean: empty ones left out, spaces gone", settings == {"host": "daint.alps.cscs.ch", "user": "me", "port": "2222",
        "jump": "me@ela.cscs.ch", "identities": ["~/.ssh/cscs-key"], "options": ["ForwardAgent=yes", "ServerAliveInterval 30"]}, settings)
  args = s.ssh_args(settings)
  check("ssh's options: -l -p -J -i (only that key) -o", args == ["-l", "me", "-p", "2222", "-J", "me@ela.cscs.ch",
        "-i", os.path.expanduser("~/.ssh/cscs-key"), "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=yes", "-o", "ServerAliveInterval=30"], args)
  check("the tab's name: user@host:port, or its own", s.label(settings) == "me@daint.alps.cscs.ch:2222"
        and s.label({**settings, "name": "daint"}) == "daint" and s.label({"host": "h", "port": "22"}) == "h")
  check("no host: no settings", s.clean({"user": "me"}) is None and s.ssh_args(None) == [])
  errors = s.option_errors({"host": "a b", "port": "70000", "options": ["ForwardAgent", "Port=22"]})
  check("what is wrong before ssh is asked: port, option without value, spaces", len(errors) == 3
        and any("70000" in e for e in errors) and any("'ForwardAgent' is no option" in e for e in errors), errors)


def test_the_hosts_of_ssh_config(check, tmp_path):
  (tmp_path / "config").write_text("Host daint daint-alias\n  HostName daint.alps.cscs.ch\nHost *.cscs.ch !ela\n  User me\n"
                                   f"Include {tmp_path}/extra/*.conf\n# Host commented\nHost   ela\n") # (relative: from ~/.ssh, as OpenSSH)
  (tmp_path / "extra").mkdir(); (tmp_path / "extra" / "a.conf").write_text("Host jureca\n")
  hosts = s.config_hosts(str(tmp_path / "config"))
  check("Host entries without wildcards, also of included files", hosts == ["daint", "daint-alias", "jureca", "ela"]
        or set(hosts) == {"daint", "daint-alias", "jureca", "ela"}, hosts)


def run(log):
  r = s.CheckResult()
  methods, offered, accepted = s.parse_ssh_log(log, r)
  return r, methods, offered, accepted


def test_what_ssh_says(check):
  r, *_ = run("ssh: Could not resolve hostname daint.alps: Name or service not known\n")
  check("unknown host name", any("daint.alps is not known" in f.text and f.level == "error" for f in r.findings), r.findings)
  r, *_ = run("debug1: connect to address 1.2.3.4 port 22: Connection refused\nssh: connect to host 1.2.3.4 port 22: Connection refused\n")
  check("refused", any("refuses connections on port 22" in f.text for f in r.findings))
  r, *_ = run("ssh: connect to host gate port 22: Connection timed out\n")
  check("timed out", any("does not answer on port 22" in f.text for f in r.findings))
  r, *_ = run("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nOffending ED25519 key in /home/me/.ssh/known_hosts:12\nHost key verification failed.\n")
  texts = [f.text for f in r.findings]
  check("a changed host key, where the old one is", any("host key of the server has changed" in t for t in texts)
        and any("/home/me/.ssh/known_hosts, line 12" in t for t in texts), texts)
  r, *_ = run("Load key \"/home/me/.ssh/id\": bad permissions\n")
  check("a key others can read", any("chmod 600" in f.text for f in r.findings))
  r, *_ = run("Received disconnect from 1.2.3.4 port 22:2: Too many authentication failures\n")
  check("too many keys", any("IdentitiesOnly" in f.text for f in r.findings))
  r, *_ = run("channel 0: open failed: administratively prohibited: open failed\nstdio forwarding failed\n")
  check("a jump host that does not forward", any("jump host does not allow forwarding" in f.text for f in r.findings))
  r, methods, offered, accepted = run("debug1: Authentications that can continue: publickey,keyboard-interactive\n"
                                      "debug1: Offering public key: /home/me/.ssh/id_ed25519 ED25519 SHA256:x\n"
                                      "debug1: Authentications that can continue: publickey,keyboard-interactive\n"
                                      "me@host: Permission denied (publickey,keyboard-interactive).\n")
  check("the methods of the server and the keys offered", methods == ["publickey", "keyboard-interactive"]
        and offered == ["/home/me/.ssh/id_ed25519"] and accepted == [], (methods, offered))
  r, methods, offered, accepted = run("debug1: Offering public key: /k ED25519\ndebug1: Server accepts key: /k ED25519 SHA256:x\n"
                                      "debug1: read_passphrase: can't open /dev/tty: No such device or address\n")
  check("a key the server accepts (its passphrase needed)", accepted == ["/k"])


def test_check_without_a_server(check):
  r = s.check("", {"host": "127.0.0.1", "port": "1"}, timeout=10)
  check("a port where nothing listens: refused, an error", r.verdict == "error" and "cannot be reached" in r.summary
        and any("refuses connections on port 1" in f.text for f in r.findings), (r.summary, r.findings))
  r = s.check("no-such-host.invalid", None, timeout=10)
  check("an unknown name", r.verdict == "error" and "not known" in r.summary, r.summary)
  r = s.check("", {"host": "localhost", "identities": ["/nonexistent/key"], "port": "1"}, timeout=10)
  check("an identity file that does not exist: said", any("/nonexistent/key does not exist" in f.text for f in r.findings), r.findings)
  text = s.check_text(r).plain
  check("in words: the verdict first, the findings", text.startswith("✖ does not work") and "✖" in text)

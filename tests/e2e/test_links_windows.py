"""The slurp helper for Windows, as far as it can be tested here (no Windows):
A. its link URL against the checks of winhelper.ps1 (the patterns taken from the script), the setup (command, .cmd
   file), static checks of the PowerShell (functions it calls exist, balance) and of the C# launcher;
B. in the lab (own relay): the ssh steps the helper runs, with the argument strings taken from winhelper.ps1: the
   tunnel, the host key check through it, the login (the password typed when its log says ssh asks for it), the web
   view through it, a shell, a wrong password;
C. the session holder: open_url to the active browser (a text message on its websocket), a download, the banner.
"""
import asyncio
import base64
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import zlib
import urllib.parse
from slurp.links import share as sh
from linkkit import blocking, free_port, read, Term, until
from slurp.links.windows import winhelper, windrive
from slurp.links import holder
from aiohttp import web
import aiohttp

PS = winhelper.SCRIPT.read_text()
CS = winhelper.LAUNCHER.read_text()

# ---- the link checks of winhelper.ps1, in Python (the patterns are read from the script)
NEED = dict(re.findall(r"Need '(\w+)' '([^']+)'", PS))
def parse_link(url):
  m = re.match(r"^slurp:(?://)?(drive|shell|web)/?\?([^#]*?)/?$", url.strip())
  if not m: raise ValueError("not a slurp link")
  kind, q = m[1], {}
  for pair in m[2].split("&"):
    i = pair.find("=")
    if i < 1: continue
    q[pair[:i]] = urllib.parse.unquote(pair[i + 1:].replace("+", " "))
  def need(key, pattern=None):
    pattern = pattern or NEED[key]
    v = q.get(key, "")
    if not re.match(pattern.replace("(?://)", "(?://)") + ("" if pattern.endswith("$") else ""), v) or not re.fullmatch(pattern.strip("^$"), v):
      raise ValueError(f"no valid {key!r}: {v!r}")
    return v
  link = {k: need(k) for k in ("v", "id", "r", "rh", "h", "p", "fp", "a", "u", "x")}
  jumps = re.findall(r"Need 'j' '([^']+)'", PS) # (serveo, ssh-j, openssh)
  if link["r"] == "serveo":
    link["j"] = need("j", jumps[0])
    if link["h"] != link["j"].split("@")[0] or link["p"] != "22": raise ValueError("the link's host is not valid")
  elif link["r"] in ("direct", "tcp"): link["j"] = ""
  else:
    if not re.fullmatch("slurp|localhost", link["h"]): raise ValueError("the link's host is not valid")
    link["j"] = need("j", jumps[1] if link["r"] == "ssh-j" else jumps[2])
  if kind == "web":
    link["wp"], link["tok"] = need("wp"), need("tok")
  import hashlib
  if "slurp-" + hashlib.sha256(link["fp"].encode()).hexdigest()[:8] != link["a"]:
    raise ValueError("alias and fingerprint differ")
  link["kind"], link["t"] = kind, q.get("t", "")
  return link

# ---- the argument strings of winhelper.ps1
def ps_function(name):
  i = PS.index(f"function {name}")
  j = PS.index("\nfunction ", i + 1)
  return PS[i:j]
def ps_strings(body, var):
  """the double-quoted strings of '$var = "..." + "..."' (one statement, over lines)"""
  m = re.search(r"\$" + var + r" = ((?:\"[^\"]*\"\s*\+?\s*)+)", body)
  return "".join(re.findall(r"\"([^\"]*)\"", m[1]))
def subst(text, values):
  text = re.sub(r"\$\((\$[\w.]+)\)", lambda m: str(values[m[1]]), text)
  return re.sub(r"\$(\w+)", lambda m: str(values["$" + m[1]]), text)

def test_helper_script(check, links):
  """A: the link URL against the checks of winhelper.ps1, the setup, static checks of the PowerShell and C#"""
  for relay in ["ssh-j.com", "lab-relay", "direct://192.168.7.5:40999", "serveo.net", "pinggy", "sshreach://u1@fw1.sshreach.me:10248"]:
    for kind in ("drive", "shell", "web"):
      link = sh.Link(kind, f"{kind}: daint /users/x (o'brien) & more", "daint", lambda: [], relay=relay, path="/users/x",
                     read_only=(kind == "drive"), web_port=41173 if kind == "web" else 0,
                     web_token="AbC_d-1234567890xyzXYZ12" if kind == "web" else "")
      link.relay_port, link.relay_state = 40123, "ready"
      if link.direct: link.port = 40999
      if link.relay_type == "pinggy": link.public_host = "abcde-1-2-3-4.run.pinggy-free.link"
      url = winhelper.link_url(link)
      try:
        parsed = parse_link(url); err = ""
      except ValueError as e:
        parsed, err = None, str(e)
      check(f"{relay} {kind}: the link passes the helper's checks", parsed is not None, err or url[:120])
      if parsed:
        host, port = link.target()
        check(f"{relay} {kind}: its values", parsed["kind"] == kind and parsed["id"] == link.id and parsed["j"] == link.jump()
              and parsed["h"] == host and int(parsed["p"]) == port and parsed["fp"] == sh.host_fingerprint()
              and parsed["t"] == link.title and link.password not in url
              and (kind != "web" or (parsed["wp"] == "41173" and parsed["tok"] == link.web_token)), parsed)
      check(f"{relay} {kind}: helper available", winhelper.available(link) is None, winhelper.available(link))
  bad = winhelper.link_url(link).replace("fp=SHA256", "fp=SHA1")
  try: parse_link(bad); ok = False
  except ValueError: ok = True
  check("a link with a broken fingerprint is refused", ok)
  jl = sh.Link("shell", "x", "t", lambda: [], relay="ssh-j.com"); jl.relay_state = "ready"
  evil = winhelper.link_url(jl).replace("j=", "j=-oProxyCommand%3Dcalc%20", 1)
  try: parse_link(evil); ok = False
  except ValueError: ok = True
  check("a jump host that is an ssh option is refused", ok)
  relayed = sh.Link("shell", "x", "t", lambda: [], relay="ssh-j.com"); relayed.relay_state = "ready"
  try: parse_link(winhelper.link_url(relayed).replace("h=slurp", "h=evil.example")); ok = False
  except ValueError: ok = True
  check("a relayed link to another host is refused (only a direct one names a host)", ok and "$link.Host -notmatch '^(slurp|localhost)$'" in PS)
  dl = sh.Link("shell", "x", "t", lambda: [], relay="direct"); dl.port, dl.relay_state = 41000, "ready"
  import socket as _socket
  cmds = dict((t, c) for t, c, _ in sh.client_commands(dl))
  check("direct: this machine's name by default, the commands without a jump host", dl.target() == (_socket.getfqdn(), 41000)
        and all("-J" not in c and "ProxyJump" not in c for c in cmds.values()) and "-p 41000" in cmds["Linux, macOS, Windows (OpenSSH)"], cmds)
  dd = sh.Link("drive", "d", "t", lambda: [], relay="direct://fs.example.org:2200", path="/users/x"); dd.port, dd.relay_state = 2200, "ready"
  titles = [t for t, _, _ in sh.client_commands(dd)]
  check("direct drive: SSHFS-Win by hand in one step (no tunnel), sshfs without ProxyJump",
        "Windows by hand (SSHFS-Win): map the drive" in titles and not any("step 1" in t for t in titles)
        and dict((t, c) for t, c, _ in sh.client_commands(dd))["Windows by hand (SSHFS-Win): map the drive"] == "net use X: \\\\sshfs\\slurp@fs.example.org!2200", titles)
  wd = windrive.launcher(dd)
  check("direct drive: the pasted drive window gets the host and port, no jump", "$LinkRelayKind = 'direct'" in windrive.settings(dd)
        and "$LinkTargetHost = 'fs.example.org'" in windrive.settings(dd) and "$LinkJump = ''" in windrive.settings(dd))
  click = re.search(r"Tray\.Add_MouseClick\(\{(.*?)\}\)", PS, re.S)[1]
  check("tray: a click shows the helper window (and the 'show' message of slurp-open.exe)", "Show-Panel" in click and "'show') { Show-Panel }" in PS, click)
  panel = ps_function("New-Panel")
  check("helper window: the connections (state, details, route, since, logins until) and the actions",
        all(f"@('{c}'" in panel for c in ("Kind", "Link", "State", "Details", "Route", "Since", "Logins until"))
        and all(f"'{a}'" in panel for a in ("open", "reconnect", "disconnect", "remove", "logs")) and "'show'" not in panel, panel[:300])
  check("helper window: no Helper ... button (the tray's menu has the helper's settings), no window per connection",
        "Helper ..." not in PS and PS.count("New-Object Windows.Forms.Form") == 2 and "Windows.Forms.Form" in ps_function("New-Panel")
        and "Windows.Forms.Form" in ps_function("Map-Another") and "Windows.Forms.Form" not in ps_function("New-Connection"))
  details = ps_function("Show-Details")
  check("helper window: the selected connection below the list, its settings only while it is not connected",
        all(f"$p.{w}" in details for w in ("Folder", "Letter", "Pw", "Go", "State", "Steps")) and "$editable = ($c -and $phase -eq 'input')" in details
        and "$c.PwDraft" in details and "$c.FolderText" in details)
  check("what is typed stays with its connection (another row selected and back)", all(f"$p.Shown.{k} = " in panel for k in ("PwDraft", "FolderText", "LetterChoice")))
  menu = ps_function("Build-ConnMenu")
  check("a row's menu: drive: open each letter, map another, unmap one; shell: another shell; web: browser; all: reconnect, disconnect, logs, remove",
        all(f"'{a}'" in menu for a in ("openmap", "mapanother", "unmap", "another", "open", "reconnect", "disconnect", "logs", "copy", "remove"))
        and "ContextMenuStrip" in panel and "Build-ConnMenu $s" in panel and "HitTest" in panel, menu[:200])
  ma = ps_function("Map-Another")
  check("Map another folder: a small window (folder, letter), then the same tunnel and password",
        "Resolve-Folder $st.Conn.Link $st.Folder.Text" in ma and "[void]$c.Pending.Add(" in ma and "Begin-NetUse $c" in ma and "Get-FreeLetters" in ma)
  check("Open another shell: a new connection of the same link, its terminal at once", "New-Connection $c.Link" in ps_function("Another-Shell") and "Begin-Connect $n" in ps_function("Another-Shell"))
  check("Unmap one folder; the last one disconnects", "Disconnect-Keep $c" in ps_function("Unmap-One") and "/delete /y" in ps_function("Unmap-One"))
  rc = ps_function("Reconnect")
  check("reconnect: a drive maps all its folders again; a shell asks first (its terminal ends); no password: its window asks",
        "foreach ($m in $prev) { [void]$c.Pending.Add($m) }" in rc and "Ask " in rc and "-not $c.Password" in rc, rc[:200])
  dk = ps_function("Disconnect-Keep")
  check("disconnect keeps the connection in the list (Remove takes it out)", "Set-Phase $c 'input'" in dk and "Form.Close" not in dk)
  check("helper window: updated in place, not into itself", "$script:InPanel" in ps_function("Update-Panel") and "Update-Panel" in PS.split("$timer.Add_Tick", 1)[1])
  from slurp.ui.screens import links as _links
  wt = str(_links.warning_text("direct://login.example.org:2222", "shell"))
  check("setup panel: direct explains itself (reachable, every interface, the password)", "Direct, no relay" in wt and "login.example.org port 2222" in wt
        and "every" in wt and str(_links.warning_text("direct://a b", "shell")).startswith("direct://a b:"), wt)
  for bad_relay in ("direct://-oProxyCommand=x", "direct://a b", "direct://h:99999"):
    try: sh.direct_address(bad_relay); ok = False
    except ValueError: ok = True
    check(f"direct: {bad_relay} is refused", ok)
  other = sh.Link("shell", "x", "t", lambda: [], auth="key", public_key="ssh-ed25519 AAAA")
  check("key links: no helper (it logs in with the password)", winhelper.available(other) is not None)

  # ---- a drive: the folders it offers (dirs), what the folder box resolves to, the UNC path of SSHFS-Win
  DIRS_RE = re.search(r"\$entry -cmatch '([^']+)'", PS)[1]
  def parse_dirs(url, path=""):
    q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1], keep_blank_values=True))
    out = []
    for entry in q.get("dirs", "").split("\n"):
      m = re.match(DIRS_RE, entry)
      if m and len(out) < 16: out.append((m[1], m[2]))
    if path.startswith("/") and not any(n == "HOME" for n, _ in out): out.insert(0, ("HOME", path))
    return out
  d = sh.Link("drive", "drive: daint /users/x", "daint", lambda: [], relay="ssh-j.com", path="/users/x", uses=None)
  d.relay_port, d.relay_state = 40123, "ready"
  d.dirs = [("HOME", "/users/x"), ("SCRATCH", "/capstor/scratch/cscs/x"), ("PROJECT", "/capstor/store/cscs/proj p")]
  url = winhelper.link_url(d)
  check("drive: the link has its folders, the helper reads them all", parse_dirs(url, d.path) == d.dirs, (parse_dirs(url, d.path), url[-160:]))
  check("drive: the link with folders still passes the helper's checks", parse_link(url)["kind"] == "drive")
  import slurp as _slurp
  _q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
  check("version: the link carries slurp's version, in the form the helper takes", _q.get("sv") == _slurp.__version__
        and re.fullmatch(r"[0-9][0-9A-Za-z.+-]{0,30}", _q["sv"]) and "SlurpVersion = $(if ([string]$q['sv'] -cmatch" in PS)
  _setup = winhelper.setup_script()
  check("version: the setup gives the helper its version (slurp's), the tray, window and menu show it",
        f"$HelperVersion = '{_slurp.__version__}'" in _setup.split("$Helper = @'", 1)[1] and '$script:Tray.Text = "slurp helper $HelperVersion"' in PS
        and '$form.Text = "slurp helper $HelperVersionText"' in PS and '"slurp helper $($HelperVersion): its window ..."' in PS)
  check("version: a link of another slurp version: the helper says so (and works on)", "$link.SlurpVersion -ne $HelperVersion" in PS
        and "set the helper up again from slurp" in PS)
  check("version: the drive window shows it", f"$SlurpVersion = '{_slurp.VERSION_TEXT}'" in windrive.settings(d) and "slurp drive ($SlurpVersion)" in windrive.helper_script(d))
  d2 = sh.Link("drive", "d", "daint", lambda: [], relay="ssh-j.com", path="/users/x")
  d2.dirs = [("SCRATCH", "/s|x"), ("lower", "/l"), ("WORK", "relative"), ("STORE", "/ok")]
  check("drive: folders a Windows drive cannot have, or not absolute, are left out", parse_dirs(winhelper.link_url(d2), d2.path) == [("HOME", "/users/x"), ("STORE", "/ok")],
        parse_dirs(winhelper.link_url(d2), d2.path))
  # the drive's name in Explorer: user@cluster:/path (n), set after the mapping, removed with it
  d.origin = "roman@daint"
  _q = dict(urllib.parse.parse_qsl(winhelper.link_url(d).split("?", 1)[1]))
  ORIGIN_RE = re.search(r"Origin = \$\(if \(\[string\]\$q\['n'\] -cmatch '([^']+)'\)", PS)[1]
  check("drive name: the link carries user@cluster, in the form the helper takes", _q.get("n") == "roman@daint"
        and re.fullmatch(ORIGIN_RE, _q["n"]) and not re.fullmatch(ORIGIN_RE, "x;calc@y") and not re.fullmatch(ORIGIN_RE, "nobody"), _q.get("n"))
  check("drive name: no origin, no n (the helper names it slurp:/path)", "n" not in dict(urllib.parse.parse_qsl(winhelper.link_url(d2).split("?", 1)[1]))
        and '"slurp:$($m.Path)"' in ps_function("Set-DriveLabel"))
  label = ps_function("Set-DriveLabel"); key = ps_function("Get-DriveLabelKey")
  check("drive name: _LabelFromReg in the key of the UNC path under MountPoints2 (\\ as #), as Explorer renames a drive",
        "'_LabelFromReg'" in label and '"$($c.Link.Origin):$($m.Path)"' in label and "CreateSubKey((Get-DriveLabelKey $m))" in label
        and "Explorer\\MountPoints2\\' + $m.Unc.Replace('\\', '#')" in key, key)
  mapping = PS.split("{ $_ -eq 'mapping' -or", 1)[1].split("'connected' {", 1)[0]
  check("drive name: set right after a mapping, removed on unmapping", "[void]$c.Maps.Add($m)\nSet-DriveLabel $c $m" in mapping.replace("      ", "")
        or ("[void]$c.Maps.Add($m)" in mapping and mapping.index("Set-DriveLabel $c $m") > mapping.index("[void]$c.Maps.Add($m)")),
        mapping[:200])
  check("drive name: …removed with the drive", "Remove-DriveLabel $m" in ps_function("Unmap-Drive"))
  check("drive: an old link (no folders) offers its login directory", parse_dirs(winhelper.link_url(sh.Link("drive", "d", "t", lambda: [], path="/users/y")), "/users/y") == [("HOME", "/users/y")])

  def resolve(dirs, text):
    """Resolve-Folder of winhelper.ps1, in Python (the same steps)"""
    choices = [(f"${n}   {p}", p) for n, p in dirs] + [("/   (the whole file system)", "/")]
    t = text.strip()
    for label, p in choices:
      if t == label: return p
    home = dict(dirs).get("HOME", "")
    m = re.match(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$", t)
    if m:
      base = dict(dirs).get(m[1].upper())
      if not base: return None
      t = base + m[2]
    elif t.startswith("~"): t = home + t[1:]
    elif not re.match(r"^[/\\]", t):
      if not home: return None
      t = home + "/" + t
    t = re.sub(r"/{2,}", "/", t.replace("\\", "/"))
    if len(t) > 1: t = t.rstrip("/")
    t = t or "/"
    if re.search(r'[\x00-\x1f\x7f<>:"|?*]', t) or re.search(r"(^|/)\.\.?(/|$)", t): return None
    return t
  rf = ps_function("Resolve-Folder")
  check("folder box: the Python steps are the ones of Resolve-Folder", all(x in rf for x in (
    r"'^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$'", "'^~(.*)$'", r"'(^|/)\.\.?(/|$)'", "-replace '/{2,}', '/'", "TrimEnd('/')")), rf[:200])
  cases = [("$HOME   /users/x", "/users/x"), ("$SCRATCH   /capstor/scratch/cscs/x", "/capstor/scratch/cscs/x"),
           ("/   (the whole file system)", "/"), ("$SCRATCH/run1/", "/capstor/scratch/cscs/x/run1"),
           ("${PROJECT}/data", "/capstor/store/cscs/proj p/data"), ("$scratch", "/capstor/scratch/cscs/x"),
           ("~/src", "/users/x/src"), ("src/a", "/users/x/src/a"), ("/capstor//x\\y", "/capstor/x/y"), ("/", "/"),
           ("$WORK", None), ("/x/../etc", None), ("/x:y", None), ('/a"b', None)]
  got = [(t, resolve(d.dirs, t)) for t, _ in cases]
  check("folder box: what it resolves to", got == cases, [g for g, c in zip(got, cases) if g != c])
  unc = ps_function("Get-Unc")
  def get_unc(port, path):
    u = f"\\\\sshfs.r\\slurp@localhost!{port}"
    return u if path == "/" else u + path.replace("/", "\\")
  check("drive: SSHFS-Win's absolute form (\\\\sshfs.r), the path in it", r'"\\sshfs.r\$($c.Link.User)@$($c.UncHost)!$($c.Port)"' in unc and "$c.UncHost = 'localhost'" in PS and "$c.UncHost = $c.Link.Host" in PS
        and get_unc(5000, "/capstor/scratch/cscs/x") == r"\\sshfs.r\slurp@localhost!5000\capstor\scratch\cscs\x" and get_unc(5000, "/") == r"\\sshfs.r\slurp@localhost!5000", unc)
  nu = ps_function("Begin-NetUse")
  check("drive: net use gets the UNC path quoted (a folder may have spaces)", '`"$($m.Unc)`"' in nu, nu)
  step_map = PS.split("{ $_ -eq 'mapping' -or", 1)[1].split("'connected' {", 1)[0]
  fails = [m.start() for m in re.finditer(r"Fail \$c", step_map)]
  guards = [step_map.rfind("$extra", 0, i) for i in fails]
  check("drive: a further folder that fails or is slow leaves the mapped ones (every Fail is behind $extra)",
        "$extra = ($c.Maps.Count -gt 0 -and -not $c.Remap)" in step_map and len(fails) == 2 and all(g > step_map.index("$extra =") for g in guards), step_map[:400])
  check("drive: reconnecting maps every folder again", "foreach ($m in @($c.Maps)) { [void]$c.Pending.Add(" in PS)
  check("drive: no $c.Drive / $c.Target left (several drives now)", "$c.Drive" not in PS and "$c.Target" not in PS)
  from slurp.cluster import storage
  os.environ.update(SCRATCH="/tmp/scr", PROJECT="/proj/p/"); os.environ.pop("WORK", None); os.environ.pop("STORE", None)
  check("drive_dirs: $HOME (the login directory) and the variables that are set, in order", storage.drive_dirs("/home/me") == [("HOME", "/home/me"), ("SCRATCH", "/tmp/scr"), ("PROJECT", "/proj/p")],
        storage.drive_dirs("/home/me"))

  script = winhelper.setup_script()
  cmd = winhelper.setup_command()
  b64 = re.search(r"FromBase64String\('([^']+)'\)", cmd)[1]
  check("setup command: round trip", zlib.decompress(base64.b64decode(b64), -15).decode() == script, len(cmd))
  check("setup command: no $ or double quote inside (as the drive command)", '"' not in cmd.split('-Command "', 1)[1][:-1] and "$" not in cmd)
  check(f"setup command: {len(cmd)} characters (PowerShell; cmd takes {windrive.CMD_LIMIT})", 5000 < len(cmd) < 60000)
  f = winhelper.setup_file()
  text = f.decode()
  check("setup .cmd: starts as a batch file, CRLF, no BOM", text.startswith("@echo off\r\n") and "\n" not in text.replace("\r\n", "") and not f.startswith(b"\xef\xbb\xbf"))
  check("setup .cmd: the marker once, followed by the setup", text.count(winhelper.SETUP_MARK) == 1 and
        text.split(winhelper.SETUP_MARK, 1)[1].replace("\r\n", "\n").strip() == script.strip())
  check("setup: trusts this slurp", f"$Fingerprint = '{sh.host_fingerprint()}'" in script and f"$Alias = '{sh.host_alias()}'" in script)
  helper = script.split("$Helper = @'\n", 1)[1].split("\n'@\n", 1)[0]
  check("setup: the helper is embedded whole (every function)", all(f"function {n}" in helper for n in re.findall(r"^function ([\w-]+)", PS, re.M)))
  check("setup: the launcher source is embedded as is", CS.strip() in script)

  # PowerShell: what it calls exists; balance
  defined = set(re.findall(r"^function ([\w-]+)", PS, re.M))
  called = set(re.findall(r"(?<![\w$.-])([A-Z][a-z]+-[A-Z][A-Za-z]+)\b", re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "''", PS)))
  cmdlets = {"Add-Type", "New-Object", "Get-Command", "Test-Path", "Join-Path", "Get-ChildItem", "Get-Date", "Remove-Item",
             "Start-Process", "Start-Sleep", "Get-Item", "Set-Location", "Where-Object", "ForEach-Object", "Select-Object",
             "Sort-Object", "Out-String", "Get-ItemProperty", "Set-ItemProperty", "Remove-ItemProperty", "New-Item", "Out-Null",
             "Get-Content", "Add-Content", "Write-Host"}
  unknown = sorted(called - defined - cmdlets)
  check("winhelper.ps1: every function it calls is defined (or a cmdlet)", not unknown, unknown)
  unused = sorted(n for n in defined if len(re.findall(r"(?<![\w-])" + re.escape(n) + r"\b", PS)) < 2)
  check("winhelper.ps1: no function is left unused", not unused, unused)
  code = re.sub(r"'[^'\n]*'", "''", re.sub(r"^\s*#.*$", "", PS, flags=re.M))
  code = re.sub(r"\"(?:`.|[^\"`\n])*\"", '""', code)
  check("winhelper.ps1: braces and parentheses balance", code.count("{") == code.count("}") and code.count("(") == code.count(")"),
        (code.count("{"), code.count("}"), code.count("("), code.count(")")))
  check("winhelper.ps1: no assignment to an automatic variable", not re.search(r"\$(input|args|this|_|matches|error|host|home|pid)\s*=[^=]", PS, re.I),
        re.findall(r"\$(?:input|args|this|_|matches|error|host|home|pid)\s*=[^=]", PS, re.I))
  check("winhelper.ps1: no line starts with '@ (embedded in a here-string)", not re.search(r"^\s*'@", PS, re.M))
  check("winhelper.cs: braces balance", CS.count("{") == CS.count("}") and CS.count("(") == CS.count(")"))
  decls = re.findall(r"\[DllImport\(([^\]]*)\)\]\s*static extern \w+ (\w+)\(", CS)
  check("winhelper.cs: no char field in a marshalled struct, W functions with Unicode", "public char" not in CS and
        all("CharSet.Unicode" in attrs for attrs, name in decls if name.endswith("W")) and len(decls) >= 9, decls)
  check("winhelper.cs: C# 5 (no $\"\", no out var, no =>)", not re.search(r'(?<!\w)\$"', CS) and "out var" not in CS and "=>" not in CS)
  WD = windrive.SCRIPT.read_text()
  check("logs are read shared with their writer (ssh keeps them open; ReadAllText fails on Windows)",
        all("::ReadAllText(" not in t and "[IO.FileShare]::ReadWrite" in t for t in (PS, WD)),
        [n for n, t in (("winhelper.ps1", PS), ("windrive.ps1", WD)) if "::ReadAllText(" in t])
  wd = windrive.launcher(sh.Link("drive", "d", "t", lambda: [], relay="ssh-j.com"))
  check("drive window: started without a console window (not hidden)", "CreateNoWindow = $true" in wd and "WindowStyle Hidden" not in wd)
  ol = ps_function("Open-Link")
  check("shell: the terminal opens at once (no click: nothing to ask)", "if ($c.Kind -eq 'shell') { Begin-Connect $c }" in ol, ol[-600:])
  shell_in = ps_function("Shell-In")
  check("shell: logged in, it is connected (no window to hide)", "Connected $c" in shell_in and "Hide-Window" not in PS)
  step_login = PS.split("'login' {", 1)[1].split("'opening' {", 1)[0]
  shell_part = step_login.split("if ($c.Kind -eq 'shell') {", 1)[1].split("if (-not $authed", 1)[0]
  check("shell: never ended for a timeout (no Fail while it waits for the login)", "Fail" not in shell_part and "Shell-In" in shell_part and "--watch" in PS, shell_part)
  check("drive window: the console waits for the window to show", "shown.txt" in wd and "shown.txt" in windrive.SCRIPT.read_text())


async def holder_part(check):
  h = holder.Holder("/nonexistent", [])
  master, slave = os.openpty()
  h.master = master
  port = await h.web_start()
  jar = aiohttp.CookieJar(unsafe=True)
  async with aiohttp.ClientSession(cookie_jar=jar) as s:
    async with s.get(f"http://127.0.0.1:{port}/?token={h.web_token}") as r:
      page = await r.text()
    check("the page: slurp's name and favicon, the start screen with the logo (not Textual's)",
          "<title>slurp</title>" in page and 'rel="icon" href="data:image/svg+xml,' in page and "All of slurm in one slurp" in page
          and "Textual Application" not in page and "M2927.81" not in page)
    check("the page has the link banner (before textual.js)", "slurp-link-banner" in page and page.index("slurp-link-banner") < page.index("textual.js"))
    ws = await s.ws_connect(f"http://127.0.0.1:{port}/ws?width=80&height=24")
    await asyncio.sleep(0.3)
    check("the browser is the active view", h.active is not None and h.active.kind == "web", h.active and h.active.label)
    reply = await h.control({"cmd": "open_url", "url": "slurp://drive?v=1&id=abc"})
    msg = None
    async def text_message():
      while True:
        m = await ws.receive(timeout=5)
        if m.type == aiohttp.WSMsgType.TEXT: return json.loads(m.data)
    msg = await text_message()
    check("open_url: the browser gets ['open_url', {url, new_tab: false}]", reply.get("ok") and msg == ["open_url", {"url": "slurp://drive?v=1&id=abc", "new_tab": False}], (reply, msg))
    reply = await h.control({"cmd": "open_url", "url": "https://status.example.org/"})
    msg = await text_message()
    check("open_url: a web page in a new tab (slurp's page stays)", reply.get("ok") and msg == ["open_url", {"url": "https://status.example.org/", "new_tab": True}], (reply, msg))
    reply = await h.control({"cmd": "open_url", "url": "javascript:alert(1)"})
    check("open_url: other schemes are refused", not reply.get("ok"), reply)
    check("the page has the copy script for Ctrl+C (before textual.js, which opens the websocket)",
          "slurp_selection" in page and page.index("slurp_selection") < page.index("textual.js"))
    reply = await h.control({"cmd": "selection", "text": "marked\ntext"})
    msg = await text_message()
    check("selection: the browser gets ['slurp_selection', text]", reply.get("ok") and msg == ["slurp_selection", "marked\ntext"], (reply, msg))
    data = winhelper.setup_file()
    reply = await h.control({"cmd": "deliver", "name": "slurp-helper-setup.cmd", "data": base64.b64encode(data).decode()})
    msg = await text_message()
    url = msg[1]["url"] if msg and msg[0] == "open_url" else ""
    check("deliver: the browser opens /download/<key>, in a new tab (slurp's page stays)", reply.get("ok") and url.startswith("/download/")
          and msg[1].get("new_tab") is True, (reply, msg))
    async with s.get(f"http://127.0.0.1:{port}{url}") as r:
      body = await r.read(); disposition = r.headers.get("Content-Disposition", "")
    check("download: the file, as an attachment with its name", body == data and 'filename="slurp-helper-setup.cmd"' in disposition, disposition)
    async with aiohttp.ClientSession() as other:
      async with other.get(f"http://127.0.0.1:{port}{url}") as r:
        check("download: not without the token's cookie", r.status == 403, r.status)
    async with s.get(f"http://127.0.0.1:{port}/download/nope") as r:
      check("download: an unknown key is not found", r.status == 404, r.status)
    sent = []
    term = holder.View(h, "terminal", "a terminal", sent.append, lambda code: None, 80, 24)
    h.attach(term); h.activate(term)
    reply = await h.control({"cmd": "open_url", "url": "slurp://drive?v=1"})
    check("open_url: refused while a terminal is the active view", not reply.get("ok") and "not a browser" in reply.get("error", ""), reply)
    reply = await h.control({"cmd": "selection", "text": "x"})
    check("selection: not sent while a terminal is the active view", not reply.get("ok"), reply)
    await ws.close()
  await h.web_stop()
  os.close(master); os.close(slave)

async def lab_part(check, mgr, events, S, KNOWN):
  conn = os.path.join(S, "winhelper_conn"); shutil.rmtree(conn, ignore_errors=True); os.makedirs(conn)
  async def page(request):
    return web.Response(text=f"the web view (token {request.query.get('token')})")
  app = web.Application(); app.add_routes([web.get("/", page)])
  runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
  view_port = site._server.sockets[0].getsockname()[1]
  web_link = mgr.create(sh.Link("web", "this slurp (test)", "lab", lambda: [], relay="lab-relay", uses=None,
                                web_port=view_port, web_token="tok123tok123"))
  shell_link = mgr.create(sh.Link("shell", "shell (helper test)", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"],
                                  relay="lab-relay", uses=None))
  ok = await until(lambda: web_link.ready() and shell_link.ready(), 20)
  check("the links' relay tunnels are up", ok, (web_link.relay_state, shell_link.relay_state))
  tunnel_args = ps_strings(ps_function("Start-Tunnel"), "arguments")
  keyscan_args = re.search(r'Start-Proc \$Keyscan "([^"]+)"', PS)[1]
  login = ps_function("Begin-Login")
  common = ps_strings(login, "common")
  web_args, shell_args = re.findall(r'\$arguments = "([^"]+)"', login)
  check("the argument strings are found in winhelper.ps1", "-L 127.0.0.1:" in tunnel_args and "HostKeyAlias" in common and "-N" in web_args and "RequestTTY" in shell_args,
        (tunnel_args, common, web_args, shell_args))
  procs = []
  def start(args, cwd, tty=False):
    if tty:
      t = Term(["ssh", *shlex.split(args)], os.environ | {"PWD": cwd}, 100, 30)
      procs.append(t); return t
    p = subprocess.Popen(["ssh", *shlex.split(args)], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p); return p
  def log_of(name): return read(os.path.join(conn, name)) if os.path.exists(os.path.join(conn, name)) else ""

  async def connect(link, kind, password, tag, watch=None):
    parsed = parse_link(winhelper.link_url(link))
    tport = free_port()
    values = {"$c.Port": tport, "$link.Host": parsed["h"], "$link.Port": parsed["p"], "$link.Jump": parsed["j"],
              "$known": "", "$batch": "yes", "$link.Alias": parsed["a"], "$link.User": parsed["u"],
              "$prompts": 3 if kind == "shell" else 1, "$c.Host": "127.0.0.1"}
    for f in ("tunnel.log", "client.log", "known_hosts"):
      if os.path.exists(os.path.join(conn, f)): os.unlink(os.path.join(conn, f))
    tunnel = start(subst(tunnel_args, values), conn)
    ok = await until(lambda: "forwarding listening" in log_of("tunnel.log"), 15)
    check(f"{tag}: the tunnel to the relay (the log line the helper waits for)", ok and "forwarding listening" in PS, log_of("tunnel.log")[-300:])
    line = ""
    for attempt in range(3): # (as the helper: three tries)
      out = (await blocking(subprocess.run, ["ssh-keyscan", *shlex.split(subst(keyscan_args, values))], capture_output=True, text=True, timeout=20)).stdout
      line = next((l for l in out.splitlines() if "ssh-ed25519" in l), "")
      if line: break
      await asyncio.sleep(0.7)
    check(f"{tag}: the host key on the first try", attempt == 0, attempt)
    import hashlib
    parts = line.split()
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(parts[2])).digest()).decode().rstrip("=") if len(parts) > 2 else ""
    check(f"{tag}: the host key through the tunnel is the link's", fp == parsed["fp"], (fp, parsed["fp"]))
    open(os.path.join(conn, "known_hosts"), "w").write(f"{parsed['a']} {parts[1]} {parts[2]}\n")
    values["$common"] = subst(common, values)
    values["$c.WebLocal"] = wport = free_port()
    values["$link.WebPort"] = parsed.get("wp", 0)
    client = start(subst(web_args if kind == "web" else shell_args, values), conn, tty=True)
    os.chdir(conn) if False else None
    ok = await client.read_until(r"password:", 15)
    check(f"{tag}: the prompt the feeder waits for (password: on ssh's screen)", ok and '"password:"' in CS, client.out[-200:])
    await asyncio.sleep(0.25)
    if watch:
      await watch(client)
    client.send(password + "\r")
    ok = await until(lambda: re.search(r"Authenticated to|Authentication succeeded", log_of("client.log")) or client.wait(0) is not None, 15)
    authed = bool(re.search(r"Authenticated to|Authentication succeeded", log_of("client.log")))
    return tunnel, client, wport, authed

  # the Term of the client runs in the connection folder: Term has no cwd; ssh -E client.log is relative
  real_term_init = Term.__init__
  def term_init(self, argv, env, cols=100, rows=30):
    old = os.getcwd(); os.chdir(env.get("PWD", old))
    try: real_term_init(self, argv, env, cols, rows)
    finally: os.chdir(old)
  Term.__init__ = term_init

  tunnel, client, wport, authed = await connect(web_link, "web", web_link.password, "web view")
  check("web view: logged in (the log line the helper waits for)", authed, log_of("client.log")[-300:])
  def fetch():
    import urllib.request
    try: return urllib.request.urlopen(f"http://127.0.0.1:{wport}/?token=tok123tok123", timeout=5).read().decode()
    except Exception as e: return f"error {e}"
  body = ""
  for _ in range(40):
    body = await blocking(fetch)
    if body.startswith("the web view"): break
    await asyncio.sleep(0.25)
  check("web view: it answers at the helper's local port, with the token", body == "the web view (token tok123tok123)", body)
  check("web view: ssh's terminal stays clean (logs go to the file)", "debug1" not in client.out.decode(errors="replace"), client.out[-200:])
  os.kill(client.pid, 15); tunnel.terminate()

  tunnel, client, wport, authed = await connect(web_link, "web", "wrong-password", "wrong password")
  code = await blocking(client.wait, 15)
  check("wrong password: ssh ends after one try (NumberOfPasswordPrompts=1)", not authed and code not in (None, 0), code)
  check("wrong password: the log says Permission denied (the helper's hint)", "Permission denied" in log_of("client.log"), log_of("client.log")[-300:])
  tunnel.terminate()

  # slurp-open.exe --watch, in Python: the same rules on the same screen (pyte), sampled as it does (200 ms)
  import pyte
  class Watch:
    def __init__(self, client):
      self.client, self.screen = client, pyte.Screen(100, 30)
      self.stream, self.fed = pyte.ByteStream(self.screen), 0
      self.prompted, self.other, self.blank, self.last, self.done = False, None, None, None, None
    def lines(self):
      out = self.client.out
      self.stream.feed(out[self.fed:]); self.fed = len(out)
      y = self.screen.cursor.y
      return self.screen.display[max(0, y - 1)] + self.screen.display[y], self.screen.display[y]
    async def step(self):
      await self.client.read_until(r"(?!)", 0.05) # (what the pty has: on Windows, the console shows it)
      lines, line = self.lines()
      line = line.strip(); now = time.monotonic()
      asks = "password:" in line.lower(); denied = "permission denied" in lines.lower()
      if asks: self.prompted, self.other, self.blank = True, None, None
      elif self.prompted and line and not denied:
        if line != self.last or self.other is None: self.other = now
        self.blank = None
        if now - self.other > 1: self.done = line
      elif self.prompted and not line and not denied:
        self.other = None
        if self.blank is None: self.blank = now
        if now - self.blank > 5: self.done = "(blank)"
      else: self.other = self.blank = None
      self.last = line
    async def run(self, seconds):
      end = time.monotonic() + seconds
      while self.done is None and time.monotonic() < end:
        await self.step(); await asyncio.sleep(0.15)
      return self.done
  check("watch: the rules in Python are the ones of winhelper.cs", all(t in CS for t in (
    'line.IndexOf("password:"', 'lines.IndexOf("Permission denied"', "TimeSpan.FromSeconds(1)", "TimeSpan.FromSeconds(5)", "Thread.Sleep(200)")))
  watcher = {}
  async def wrong_first(client):
    w = watcher["w"] = Watch(client)
    client.send("not-the-password\r")
    done = await w.run(4)
    check("shell watch: a wrong password is not a login (ssh asks again)", done is None and w.prompted, client.out[-200:])
    await client.read_until(r"password:\s*$", 10)
    await asyncio.sleep(0.25)
  tunnel, client, wport, authed = await connect(shell_link, "shell", shell_link.password, "shell", watch=wrong_first)
  check("shell: logged in (second try: the shell asks 3 times)", authed, log_of("client.log")[-300:])
  done = await watcher["w"].run(10)
  check("shell watch: the shell's prompt is seen as the login (the window hides)", done and "$" in done, (done, client.out[-200:]))
  client.send("echo HELPER-$((6*7))\r")
  ok = await client.read_until(r"HELPER-42", 10)
  check("shell: a shell in the terminal", ok, client.out[-300:])
  check("shell: no debug output in the terminal (jump-free, logs to the file)", "debug1" not in client.out.decode(errors="replace"))
  client.send("exit\r")
  code = await blocking(client.wait, 10)
  check("shell: exit ends ssh (the helper then closes its tunnel)", code is not None, code)
  tunnel.terminate()
  for p in procs:
    try: os.kill(p.pid, 15)
    except OSError: pass
  # direct: no relay, the helper's steps straight to slurp's port
  direct = mgr.create(sh.Link("shell", "shell (direct)", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"],
                              relay="direct://127.0.0.1", uses=None))
  ok = await until(lambda: direct.ready(), 10)
  check("direct: ready at once, listening only on the address it names", ok and direct.target() == ("127.0.0.1", direct.port)
        and direct.server.sockets[0].getsockname()[0] == "127.0.0.1", (direct.relay_state, direct.relay_error))
  parsed = parse_link(winhelper.link_url(direct))
  check("direct: the helper's link (r=direct, h the host, p its port, no jump)", parsed["r"] == "direct" and parsed["h"] == "127.0.0.1"
        and int(parsed["p"]) == direct.port and parsed["j"] == "", parsed)
  values = {"$c.Port": direct.port, "$c.Host": "127.0.0.1", "$link.Alias": parsed["a"], "$link.User": parsed["u"], "$prompts": 3}
  out = (await blocking(subprocess.run, ["ssh-keyscan", *shlex.split(subst(keyscan_args, values))], capture_output=True, text=True, timeout=20)).stdout
  line = next((l for l in out.splitlines() if "ssh-ed25519" in l), "")
  import hashlib
  parts = line.split()
  fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(parts[2])).digest()).decode().rstrip("=") if len(parts) > 2 else ""
  check("direct: the host key straight from slurp is the link's", fp == parsed["fp"], (fp, out[-200:]))
  open(os.path.join(conn, "known_hosts"), "w").write(f"{parsed['a']} {parts[1]} {parts[2]}\n")
  values["$common"] = subst(common, values)
  client = start(subst(shell_args, values), conn, tty=True)
  ok = await client.read_until(r"password:", 15)
  client.send(direct.password + "\r")
  ok = ok and await client.read_until(r"\$ $", 15)
  client.send("echo DIRECT-$((6*7))\r")
  ok = ok and await client.read_until(r"DIRECT-42", 10)
  check("direct: the helper's shell logs in without a relay", ok, client.out[-300:])
  client.send("exit\r"); await blocking(client.wait, 10)
  # sshreach.me: a fixed port at its server (here the lab sshd; the client connects to it, no jump)
  import socket as _s
  so = _s.socket(); so.bind(("127.0.0.1", 0)); fixed = so.getsockname()[1]; so.close()
  reach = mgr.create(sh.Link("shell", "shell (sshreach)", "lab", lambda: ["bash", "--norc", "--noprofile", "-i"],
                             relay=f"sshreach://{getpass.getuser()}@lab-relay:{fixed}", uses=None))
  ok = await until(lambda: reach.ready(), 20)
  check("sshreach: the forward to its fixed port is up", ok and reach.target() == ("lab-relay", fixed), (reach.relay_state, reach.relay_error))
  parsed = parse_link(winhelper.link_url(reach))
  check("sshreach: the helper connects to that server and port, no jump", parsed["r"] == "tcp" and parsed["h"] == "lab-relay"
        and int(parsed["p"]) == fixed and parsed["j"] == "", parsed)
  cmd = sh.client_commands(reach)[0][1]
  client = start(" ".join(shlex.split(cmd)[1:]).replace("-o HostKeyAlias", "-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o HostKeyAlias"), conn, tty=True)
  ok = await client.read_until(r"password:", 15)
  client.send(reach.password + "\r")
  ok = ok and await client.read_until(r"\$ $", 15)
  client.send("echo REACH-$((6*7))\r")
  ok = ok and await client.read_until(r"REACH-42", 10)
  check("sshreach: the link's own command logs in at the fixed port", ok and "-J" not in cmd, (cmd, client.out[-300:]))
  client.send("exit\r"); await blocking(client.wait, 10)
  await mgr.stopped(); await runner.cleanup()



def test_holder_hands_to_browser(check, links):
  """C: the session holder hands URLs and downloads to the active browser"""
  asyncio.run(holder_part(check))


def test_helper_ssh_steps(check, links, link_manager):
  """B: the helper's ssh steps in the lab (own relay)"""
  mgr, events = link_manager
  asyncio.run(lab_part(check, mgr, events, links.S, links.KNOWN))

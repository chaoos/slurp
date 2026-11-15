"""The slurp helper for Windows: a tray program that opens links with one click (and the link's password).

slurp makes a link (shell, drive, web view) as before; its link window then also offers it as a slurp:// URL. In
slurp's web view, "Open on my computer" hands that URL to the browser, which starts the helper (it asks once whether
to); elsewhere it can be copied (Win+R, paste, Enter). The helper (winhelper.ps1) asks for the password (and the
drive letter; not for a shell, whose terminal asks for it), connects through the relay, checks slurp's host key and
then maps the drive and opens Explorer, opens the browser at the web view, or opens a terminal with the shell. What it
has open stays in the tray.

The setup is one PowerShell script (a command to paste, or a .cmd file to download from the web view). It writes
the helper into %LOCALAPPDATA%\\slurp-helper, compiles its small launcher (winhelper.cs) on the user's machine (no
binary is shipped), registers slurp:// for the user (no admin), starts it with Windows and remembers the host key
of this slurp (a link of another slurp asks first)."""

import os
import urllib.parse

from . import share, windrive

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "winhelper.ps1")
LAUNCHER = os.path.join(HERE, "winhelper.cs")
SETUP_NAME = "slurp-helper-setup.cmd"
SETUP_MARK = "#SLURP-SETUP"

def available(link):
  """None if the helper can open the link, else why not"""
  if link.auth != "password":
    return "the helper logs in with a password: this link takes a key"
  if link.kind not in ("shell", "drive", "web"):
    return f"the helper does not open {link.kind} links"
  if not link.ready():
    return "the relay is not ready yet"
  return None

def link_url(link):
  """slurp://kind?... with what the helper needs (never the password); winhelper.ps1 checks every value"""
  host, port = link.target()
  query = {"v": "1", "id": link.id, "t": link.title, "r": link.client_kind, "rh": link.relay_host, "j": link.jump(), # (direct, tcp: j empty)
           "h": host, "p": str(port), "u": share.LINK_USER, "fp": share.host_fingerprint(), "a": share.host_alias(),
           "x": str(int(link.expires or 0))}
  if link.read_only:
    query["ro"] = "1"
  if link.kind == "drive" and link.path:
    query["path"] = link.path
  if link.kind == "drive" and link.dirs: # (the folders the helper offers: NAME=/path, one per line)
    query["dirs"] = "\n".join(f"{name}={path}" for name, path in link.dirs if "\n" not in path)
  if link.kind == "web":
    query["wp"] = str(link.web_port)
    query["tok"] = link.web_token
  return f"slurp://{link.kind}?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="")

def compact(text):
  """The PowerShell script without comment lines, indentation and empty lines (it has no multi-line strings)"""
  lines = (line.strip() for line in text.splitlines())
  return "\n".join(line for line in lines if line and not line.startswith("#"))

def here_string(name, text):
  if "\n'@" in "\n" + text:
    raise ValueError(f"{name} must not have a line starting with '@")
  return f"${name} = @'\n{text}\n'@\n"

def setup_script():
  """The PowerShell script that sets the helper up (or updates it) on the user's machine"""
  with open(SCRIPT, encoding="utf-8") as f:
    helper = compact(f.read())
  with open(LAUNCHER, encoding="utf-8") as f:
    launcher = f.read()
  import base64
  from . import logo
  values = (f"$Alias = {windrive.ps_string(share.host_alias())}\n"
            f"$Fingerprint = {windrive.ps_string(share.host_fingerprint())}\n"
            f"$IconData = '{base64.b64encode(logo.ico((16, 24, 32, 48, 256))).decode()}'\n")
  return (values + here_string("Helper", helper) + here_string("Launcher", launcher) + r"""
$ErrorActionPreference = 'Stop'
$Dir = Join-Path $env:LOCALAPPDATA 'slurp-helper'
$Exe = Join-Path $Dir 'slurp-open.exe'
Write-Host "slurp helper: setting up in $Dir"
try {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  $m = $null
  if ([Threading.Mutex]::TryOpenExisting('Local\slurp-helper-tray', [ref]$m)) {
    $m.Dispose()
    Write-Host 'slurp helper: the helper that runs now quits first (what it has open closes) ...'
    New-Item -ItemType Directory -Force -Path (Join-Path $Dir 'inbox') | Out-Null
    [IO.File]::WriteAllText((Join-Path $Dir 'inbox\00000000000000000000-quit.msg'), 'quit')
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Milliseconds 250
      if (-not [Threading.Mutex]::TryOpenExisting('Local\slurp-helper-tray', [ref]$m)) { break }
      $m.Dispose()
    }
  }
  [IO.File]::WriteAllText((Join-Path $Dir 'slurp-helper.ps1'), $Helper, (New-Object Text.UTF8Encoding $true))
  $Ico = Join-Path $Dir 'slurp.ico'
  [IO.File]::WriteAllBytes($Ico, [Convert]::FromBase64String($IconData))
  Write-Host 'slurp helper: compiling its launcher (slurp-open.exe) ...'
  Remove-Item $Exe -Force -ErrorAction SilentlyContinue
  Add-Type -TypeDefinition $Launcher -Language CSharp -OutputAssembly $Exe -OutputType WindowsApplication
  $trust = Join-Path $Dir 'trusted.txt'
  $line = "$Alias $Fingerprint"
  if (-not ((Test-Path $trust) -and (@(Get-Content $trust) -contains $line))) { Add-Content -Path $trust -Value $line }
  $cls = 'HKCU:\Software\Classes\slurp'
  foreach ($k in @($cls, "$cls\DefaultIcon", "$cls\shell", "$cls\shell\open", "$cls\shell\open\command")) {
    if (-not (Test-Path $k)) { New-Item -Path $k | Out-Null }
  }
  Set-ItemProperty -Path $cls -Name '(default)' -Value 'URL:slurp link'
  Set-ItemProperty -Path $cls -Name 'URL Protocol' -Value ''
  Set-ItemProperty -Path "$cls\DefaultIcon" -Name '(default)' -Value $Ico
  Set-ItemProperty -Path "$cls\shell\open\command" -Name '(default)' -Value ('"' + $Exe + '" "%1"')
  Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'slurp helper' -Value ('"' + $Exe + '" --tray')
  try {
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'slurp helper.lnk'))
    $lnk.TargetPath = $Exe
    $lnk.IconLocation = "$Ico,0"
    $lnk.Description = 'slurp helper: opens slurp links (in the tray)'
    $lnk.Save()
  } catch { Write-Host "slurp helper: no start menu entry ($($_.Exception.Message))" }
  if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'))) {
    Write-Host 'slurp helper: ssh.exe is missing: add the Windows feature "OpenSSH Client" (Settings > Apps > Optional features)' -ForegroundColor Yellow
  }
  Start-Process $Exe
  Write-Host "slurp helper: ready. Its icon is in the tray (^ next to the clock); it starts with Windows." -ForegroundColor Green
  Write-Host "slurp helper: trusts this slurp: $Alias ($Fingerprint)"
  Write-Host 'slurp helper: in slurp, a link window: Open on my computer. Remove it again: its tray menu, Uninstall.'
} catch {
  Write-Host "slurp helper: the setup failed: $($_.Exception.Message)" -ForegroundColor Red
  if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Write-Host 'slurp helper: PowerShell is restricted on this computer (Constrained Language Mode): the helper cannot run here' -ForegroundColor Red
  }
}
""")

def setup_command():
  """The setup as a command to paste into PowerShell (too long for cmd and the Run dialog)"""
  return windrive.pack(setup_script())

def setup_file():
  """The setup as a .cmd file (bytes) to run with a double-click: cmd hands the rest of the file to PowerShell"""
  head = ("@echo off\r\n"
          "set \"SLURP_SETUP=%~f0\"\r\n"
          "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$t = [IO.File]::ReadAllText($env:SLURP_SETUP, "
          f"[Text.Encoding]::UTF8); $i = $t.IndexOf('#' + '{SETUP_MARK[1:]}'); iex $t.Substring($i)\"\r\n"
          "pause\r\n"
          "exit /b\r\n")
  return (head + SETUP_MARK + "\r\n" + setup_script().replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8")

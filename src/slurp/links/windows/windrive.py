"""The Windows drive helper of a link: one command to paste into PowerShell or cmd. It carries windrive.ps1
(compressed) with the settings of the link, writes it to a folder in %TEMP% and starts it as a hidden PowerShell
with a small window: drive letter and password, then the tunnel, the host key check, the mapping, and its state.

The command has no $ and no double quotes inside its argument, so it is the same pasted into PowerShell (which would
expand $... in a double-quoted string) and into cmd."""

import base64
import os
import time
import zlib

from ... import VERSION_TEXT
from .. import share

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "windrive.ps1")
CMD_LIMIT = 8191 # the longest line cmd takes

def ps_string(value):
  """A PowerShell string literal (single quotes: nothing is expanded)"""
  return "'" + str(value).replace("'", "''") + "'"

def settings(link):
  host, port = link.target()
  values = {
    "SlurpVersion": ps_string(VERSION_TEXT),
    "LinkTitle": ps_string(link.title),
    "LinkRelayKind": ps_string(link.client_kind),
    "LinkRelayHost": ps_string(link.relay_host),
    "LinkJump": ps_string(link.jump()),
    "LinkTargetHost": ps_string(host),
    "LinkTargetPort": str(int(port)),
    "LinkUser": ps_string(share.LINK_USER),
    "LinkFingerprint": ps_string(share.host_fingerprint()),
    "LinkExpires": str(int(link.expires or 0)),
    "LinkReadOnly": "$true" if link.read_only else "$false",
  }
  return "".join(f"${k} = {v}\n" for k, v in values.items())

def helper_script(link):
  """windrive.ps1 with the settings of the link in front"""
  with open(SCRIPT, encoding="utf-8") as f:
    body = f.read()
  # compact: without comment lines, indentation and empty lines (the script has no here-strings, where they count)
  lines = (line.strip() for line in body.splitlines())
  body = "\n".join(line for line in lines if line and not line.startswith("#"))
  return settings(link) + body + "\n"

def launcher(link):
  """What the pasted command runs: writes the helper into its own folder in %TEMP% and starts it as a PowerShell
  without a console window. Not with -File: a script file is subject to the execution policy, which Group Policy may
  set beyond -ExecutionPolicy Bypass; the helper runs as a command instead (a short -EncodedCommand that reads the
  file). Not "hidden" either (Start-Process -WindowStyle Hidden): Windows applies that state to the first window the
  process shows, which is the helper's window, so it never appeared. CREATE_NO_WINDOW (CreateNoWindow) gives the
  PowerShell an invisible console and leaves its windows alone. Errors show in a message box and in error.log; the
  console says when the window is shown (shown.txt), or why not."""
  script = helper_script(link)
  if "\n'@" in script:
    raise ValueError("windrive.ps1 must not have a line starting with '@")
  folder = f"slurp-drive-{link.id}-{int(time.time())}"
  return (
    f"$d = Join-Path $env:TEMP '{folder}'\n"
    "New-Item -ItemType Directory -Force -Path $d | Out-Null\n"
    "$f = Join-Path $d 'map-drive.ps1'\n"
    "$s = @'\n" + script + "\n'@\n"
    "[IO.File]::WriteAllText($f, $s, (New-Object Text.UTF8Encoding $true))\n"
    "$q = $d.Replace([string][char]39, [string][char]39 + [char]39)\n"
    "$b = \"`$SlurpDir = '$q'; try { iex ([IO.File]::ReadAllText(`$SlurpDir + '\\map-drive.ps1', [Text.Encoding]::UTF8)) } \" +\n"
    "  \"catch { `$m = (`$_ | Out-String); [IO.File]::WriteAllText(`$SlurpDir + '\\error.log', `$m); \" +\n"
    "  \"Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show(`$m, 'slurp drive: error') }\"\n"
    "$e = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($b))\n"
    "$si = New-Object Diagnostics.ProcessStartInfo\n"
    "$si.FileName = Join-Path $env:WINDIR 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
    "$si.Arguments = '-NoProfile -STA -ExecutionPolicy Bypass -EncodedCommand ' + $e\n"
    "$si.UseShellExecute = $false\n"
    "$si.CreateNoWindow = $true\n"
    "$si.WorkingDirectory = $d\n"
    "$p = [Diagnostics.Process]::Start($si)\n"
    "Write-Host 'slurp: starting the drive window ...'\n"
    "$log = Join-Path $d 'error.log'\n"
    "$shown = Join-Path $d 'shown.txt'\n"
    "for ($i = 0; $i -lt 40 -and -not $p.HasExited -and -not (Test-Path $log) -and -not (Test-Path $shown); $i++) { Start-Sleep -Milliseconds 500 }\n"
    "if (Test-Path $log) { Write-Host 'slurp: the drive window failed:' -ForegroundColor Red; Get-Content $log | Write-Host }\n"
    "elseif (Test-Path $shown) { Write-Host 'slurp: the drive window is open (it runs on its own; this window can be closed)' }\n"
    "elseif ($p.HasExited) { Write-Host ('slurp: the drive window ended right away (exit code ' + $p.ExitCode + ')') -ForegroundColor Red }\n"
    "else { Write-Host ('slurp: the drive window did not show within 20 s (PowerShell ' + $p.Id + ' still runs); its folder: ' + $d) -ForegroundColor Yellow }\n")

def pack(script):
  """A command to paste that runs the PowerShell script (compressed in it): no $ and no double quotes inside its
  argument, so it is the same in PowerShell and in cmd (if it is short enough for cmd)"""
  packer = zlib.compressobj(9, zlib.DEFLATED, -15) # raw deflate, as .NET's DeflateStream reads it
  data = packer.compress(script.encode("utf-8")) + packer.flush()
  b64 = base64.b64encode(data).decode()
  return ("powershell -NoProfile -ExecutionPolicy Bypass -Command \"iex (New-Object IO.StreamReader((New-Object "
          "IO.Compression.DeflateStream((New-Object IO.MemoryStream(,[Convert]::FromBase64String('" + b64 + "'))),"
          "[IO.Compression.CompressionMode]::Decompress)),[Text.Encoding]::UTF8)).ReadToEnd()\"")

def command(link):
  """The command to paste (PowerShell or cmd, not the Run dialog: too long for it)"""
  return pack(launcher(link))

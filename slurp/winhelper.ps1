# slurp helper: the tray program on Windows that opens slurp links (slurp://...) with one click and a password.
# The setup (see winhelper.py) puts it into %LOCALAPPDATA%\slurp-helper with slurp-open.exe (winhelper.cs), and
# registers slurp:// for this user (no admin). A click on a link in slurp's web view (or Win+R and the link):
# the browser starts slurp-open.exe, which hands the link to this tray (a file in its inbox) and starts it if needed.
#
# A link is a shell, a drive or the web view of one slurp. Each one is a connection, a row of the helper window:
#   the password of the link (drive and web view; and the drive letter) -> the tunnel to the relay (ssh -N -L, no
#   password) -> the host key of slurp through it (ssh-keyscan, against the fingerprint in the link, and the slurps
#   known here) -> drive: net use (SSHFS-Win) of the folder picked ($HOME, $SCRATCH, ... or typed in; more
#   folders later, on other letters, through the same tunnel) and Explorer; web view: ssh -N -L (the password typed into its
#   console by slurp-open.exe --feed) and the browser; shell: at once (nothing to ask) ssh in a terminal window,
#   which asks for the password itself (the user types it there; the helper does not ask for it); the window hides
#   is the user's (the helper sees its prompt: slurp-open.exe --watch, or ssh's log)
# The helper window (a click on the tray icon; it shows by itself when a link comes) lists every connection with its
# state (mapped folders, local port, relay or direct, since when, logins until); below the list the selected one: its
# settings while it is not connected (folder, letter, password), its state and its steps. A right click on a row: what
# that connection can do (open, map another folder, unmap one, open another shell, reconnect, disconnect, logs,
# remove). Quitting closes them all.
#
# Kept simple on purpose: no line in this file starts with '@ (the setup embeds it in a here-string), no
# multi-line strings (comment lines and indentation are stripped when it is embedded).

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

$created = $false
$TrayMutex = New-Object Threading.Mutex($true, 'Local\slurp-helper-tray', [ref]$created)
if (-not $created) { return }

Set-Location $env:USERPROFILE # (not the helper's folder: uninstalling removes it)
[Environment]::CurrentDirectory = $env:USERPROFILE
$OpenExe = Join-Path $SlurpHelperDir 'slurp-open.exe'
$Inbox = Join-Path $SlurpHelperDir 'inbox'
$ConnDir = Join-Path $SlurpHelperDir 'connections'
$TrustFile = Join-Path $SlurpHelperDir 'trusted.txt'
$HelperLog = Join-Path $SlurpHelperDir 'helper.log'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Native = $false
try { [void][Reflection.Assembly]::LoadFrom($OpenExe); $Native = $true } catch { }

function Log($text) {
  try { [IO.File]::AppendAllText($HelperLog, (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + '  ' + $text + "`r`n") } catch { }
}
try { if ((Test-Path $HelperLog) -and (Get-Item $HelperLog).Length -gt 1MB) { Remove-Item $HelperLog -Force } } catch { }
if (-not $HelperVersion) { $HelperVersion = '?'; $HelperVersionText = '?' } # (set by the setup, from slurp)
Log "tray starts: version $HelperVersionText (native helper: $Native)"

# ---- tools
function Find-Tool($name) {
  $c = Get-Command $name -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $p = Join-Path $env:WINDIR "System32\OpenSSH\$name"
  if (Test-Path $p) { return $p }
  return $null
}
function Test-Sshfs {
  $dirs = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
  $sshfs = $false; $winfsp = $false
  foreach ($d in $dirs) {
    if (Test-Path (Join-Path $d 'SSHFS-Win\bin\sshfs-win.exe')) { $sshfs = $true }
    if (Test-Path (Join-Path $d 'WinFsp\bin')) { $winfsp = $true }
  }
  if (Test-Path 'HKLM:\SOFTWARE\WOW6432Node\WinFsp') { $winfsp = $true }
  return ($sshfs -and $winfsp)
}
$Ssh = Find-Tool 'ssh.exe'
$Keyscan = Find-Tool 'ssh-keyscan.exe'

function Get-FreeLetters {
  $used = @()
  try { $used += @([IO.DriveInfo]::GetDrives() | ForEach-Object { $_.Name.Substring(0, 1).ToUpper() }) } catch { }
  $used += @(Get-ChildItem 'HKCU:\Network' -ErrorAction SilentlyContinue | ForEach-Object { $_.PSChildName.ToUpper() })
  foreach ($c in $script:Conns) { foreach ($m in @($c.Maps) + @($c.Pending)) { if ($m) { $used += $m.Letter.Substring(0, 1) } } }
  $free = @()
  foreach ($code in 68..90) { $l = [string][char]$code; if ($used -notcontains $l) { $free += $l } }
  return $free
}

function Get-FreePort {
  $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
  $l.Start(); $port = $l.LocalEndpoint.Port; $l.Stop()
  return $port
}

function Test-Port($port) {
  $t = New-Object Net.Sockets.TcpClient
  try {
    $r = $t.BeginConnect('127.0.0.1', $port, $null, $null)
    if ($r.AsyncWaitHandle.WaitOne(250)) { $t.EndConnect($r); return $true }
    return $false
  } catch { return $false } finally { $t.Close() }
}

# A process: without a window (CREATE_NO_WINDOW: it has a console, invisible), or in a console window of its own
function Start-Proc($file, $arguments, $dir, [switch]$Capture, [switch]$Visible, [switch]$Stdin) {
  $si = New-Object Diagnostics.ProcessStartInfo
  $si.FileName = $file
  $si.Arguments = $arguments
  if ($dir) { $si.WorkingDirectory = $dir }
  if ($Visible) { $si.UseShellExecute = $true } else { $si.UseShellExecute = $false; $si.CreateNoWindow = $true }
  if ($Capture) { $si.RedirectStandardOutput = $true; $si.RedirectStandardError = $true }
  if ($Stdin) { $si.RedirectStandardInput = $true }
  return [Diagnostics.Process]::Start($si)
}

function Stop-Proc($p) {
  if ($p -and -not $p.HasExited) { try { $p.Kill() } catch { } }
}

function Get-KeyFingerprint($line) {
  # "[127.0.0.1]:port ssh-ed25519 AAAA..." -> SHA256:... (as ssh shows it)
  $parts = $line.Trim() -split '\s+'
  if ($parts.Count -lt 3) { return $null }
  try { $blob = [Convert]::FromBase64String($parts[2]) } catch { return $null }
  $hash = [Security.Cryptography.SHA256]::Create().ComputeHash($blob)
  return 'SHA256:' + [Convert]::ToBase64String($hash).TrimEnd('=')
}

# The name slurp gives its host key (the same for all its links): slurp- and 8 hex digits of the fingerprint's hash
function Get-AliasOf($fp) {
  $hash = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($fp))
  return 'slurp-' + (-join ($hash[0..3] | ForEach-Object { $_.ToString('x2') }))
}

function Read-Text($path) {
  # (shared for writing and deleting: ssh has its log open for writing while it runs; ReadAllText would fail on it)
  try {
    $stream = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try { return (New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)).ReadToEnd() } finally { $stream.Dispose() }
  } catch { return '' }
}

# ---- the slurps known here (their host keys): the setup adds the one it came from, a link of another one asks
function Get-Trusted {
  $list = @()
  foreach ($line in @((Read-Text $TrustFile) -split "`r?`n")) {
    $p = $line.Trim() -split '\s+'
    if ($p.Count -ge 2 -and $p[1] -like 'SHA256:*') { $list += ,@($p[0], $p[1]) }
  }
  return ,$list
}
function Test-Trusted($fp) {
  foreach ($t in (Get-Trusted)) { if ($t[1] -ceq $fp) { return $true } }
  return $false
}
function Add-Trusted($alias, $fp) {
  [IO.File]::AppendAllText($TrustFile, "$alias $fp`r`n")
}

# ---- links: slurp://drive?..., slurp://shell?..., slurp://web?... (see winhelper.py; every value is checked)
function Parse-Link([string]$url) {
  $url = $url.Trim()
  if ($url.Length -gt 2048) { throw 'the link is too long' }
  if ($url -notmatch '^slurp:(?://)?(drive|shell|web)/?\?([^#]*?)/?$') { throw 'this is not a slurp link (slurp://drive, shell or web)' }
  $kind = $Matches[1]
  $q = @{}
  foreach ($pair in $Matches[2].Split('&')) {
    $i = $pair.IndexOf('=')
    if ($i -lt 1) { continue }
    $q[$pair.Substring(0, $i)] = [Uri]::UnescapeDataString($pair.Substring($i + 1).Replace('+', ' '))
  }
  function Need($key, $pattern) {
    $v = [string]$q[$key]
    if ($v -cnotmatch $pattern) { throw "the link has no valid '$key'" }
    return $v
  }
  if ((Need 'v' '^\d+$') -ne '1') { throw 'this link is for a newer slurp helper: set the helper up again from slurp' }
  $link = @{
    Kind = $kind
    Id = Need 'id' '^[0-9a-f]{8}$'
    RelayKind = Need 'r' '^(ssh-j|serveo|openssh|direct|tcp)$'
    RelayHost = Need 'rh' '^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$'
    Host = Need 'h' '^(slurp|localhost|[A-Za-z0-9][A-Za-z0-9.-]{0,252})$'
    Port = [int](Need 'p' '^\d{1,5}$')
    Fingerprint = Need 'fp' '^SHA256:[A-Za-z0-9+/]{43}$'
    Alias = Need 'a' '^slurp-[0-9a-f]{8}$'
    User = Need 'u' '^[A-Za-z0-9_-]{1,32}$'
    Expires = [long](Need 'x' '^\d{1,12}$')
    ReadOnly = ([string]$q['ro'] -eq '1')
    Title = (([string]$q['t']) -replace '[\x00-\x1f\x7f]', ' ')
    Path = (([string]$q['path']) -replace '[\x00-\x1f\x7f]', ' ')
    WebPort = 0
    Token = ''
    SlurpVersion = $(if ([string]$q['sv'] -cmatch '^[0-9][0-9A-Za-z.+-]{0,30}$') { [string]$q['sv'] } else { '' })
  }
  if ($link.Title.Length -gt 120) { $link.Title = $link.Title.Substring(0, 120) + '...' }
  # (a link names a host only without a jump: direct, or the public port of a relay; serveo: the link's name)
  $link.Straight = ($link.RelayKind -eq 'direct' -or $link.RelayKind -eq 'tcp')
  if ($link.RelayKind -eq 'serveo') {
    $link.Jump = Need 'j' '^[0-9a-f]{24}@[A-Za-z0-9][A-Za-z0-9.-]*$'
    if ($link.Host -cne $link.Jump.Split('@')[0] -or $link.Port -ne 22) { throw "the link's host is not valid" }
  }
  elseif (-not $link.Straight -and $link.Host -notmatch '^(slurp|localhost)$') { throw "the link's host is not valid" }
  if ($link.Straight) { $link.Jump = '' }
  elseif ($link.RelayKind -eq 'serveo') { }
  elseif ($link.RelayKind -eq 'ssh-j') { $link.Jump = Need 'j' '^[0-9a-f]{24}@[A-Za-z0-9][A-Za-z0-9.-]*$' }
  else { $link.Jump = Need 'j' '^[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?$' }
  if ($link.Port -lt 1 -or $link.Port -gt 65535) { throw "the link's port is not valid" }
  if ((Get-AliasOf $link.Fingerprint) -ne $link.Alias) { throw "the link's host key and its name do not match" }
  $link.Dirs = @()
  if ($kind -eq 'drive') {
    foreach ($entry in ([string]$q['dirs']).Split("`n")) {
      if ($entry -cmatch '^([A-Z][A-Z0-9_]{0,31})=(/[^\x00-\x1f\x7f<>:"|?*\\]{0,1000})$' -and $link.Dirs.Count -lt 16) {
        $link.Dirs += @{ Name = $Matches[1]; Path = $Matches[2] }
      }
    }
    if ($link.Path -cmatch '^/' -and -not @($link.Dirs | Where-Object { $_.Name -eq 'HOME' }).Count) {
      $link.Dirs = @(@{ Name = 'HOME'; Path = $link.Path }) + $link.Dirs
    }
  }
  if ($kind -eq 'web') {
    $link.WebPort = [int](Need 'wp' '^\d{1,5}$')
    $link.Token = Need 'tok' '^[A-Za-z0-9_-]{8,64}$'
    if ($link.WebPort -lt 1 -or $link.WebPort -gt 65535) { throw "the link's web port is not valid" }
  }
  if (-not $link.Title) { $link.Title = "$kind link $($link.Id)" }
  return $link
}

# ---- the tray icon: slurp's logo (slurp.ico, written by the setup from slurp/logo.py)
$Icon = $null
try { $Icon = New-Object Drawing.Icon((Join-Path $SlurpHelperDir 'slurp.ico'), 32, 32) } catch { }
if (-not $Icon) { $Icon = [Drawing.SystemIcons]::Application }

$script:Conns = New-Object Collections.ArrayList
$script:Quitting = $false

$KindWord = @{ drive = 'Drive'; shell = 'Shell'; web = 'Web view' }
$GoText = @{ drive = 'Map drive'; shell = 'Open terminal'; web = 'Open web view' }
$OpenText = @{ drive = 'Open in Explorer'; shell = ''; web = 'Open in browser' }

# ---- a connection: a link and its window
$UiFont = New-Object Drawing.Font('Segoe UI', 9)
$UiBold = New-Object Drawing.Font('Segoe UI', 10, [Drawing.FontStyle]::Bold)

# ---- the folders a drive can map: the ones slurp sent ($HOME, $SCRATCH, $PROJECT, ...) and the root; or typed in
function Get-FolderChoices($link) {
  $out = @()
  foreach ($d in $link.Dirs) { $out += @{ Label = "`$$($d.Name)   $($d.Path)"; Path = $d.Path } }
  $out += @{ Label = '/   (the whole file system)'; Path = '/' }
  return $out
}

# the absolute path of what the folder box says, or $null (and why, in $script:FolderError)
function Resolve-Folder($link, [string]$text) {
  $script:FolderError = ''
  $t = $text.Trim()
  foreach ($d in (Get-FolderChoices $link)) { if ($t -ceq $d.Label) { return $d.Path } }
  $homeDir = ''
  foreach ($d in $link.Dirs) { if ($d.Name -eq 'HOME') { $homeDir = $d.Path } }
  if ($t -cmatch '^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?(.*)$') {
    $name = $Matches[1]
    $rest = $Matches[2]
    $base = $null
    foreach ($d in $link.Dirs) { if ($d.Name -eq $name.ToUpper()) { $base = $d.Path } }
    if (-not $base) { $script:FolderError = "slurp did not send `$$name (it sent: $((@($link.Dirs | ForEach-Object { '$' + $_.Name })) -join ', '))"; return $null }
    $t = $base + $rest
  } elseif ($t -match '^~(.*)$') { $t = $homeDir + $Matches[1] }
  elseif ($t -notmatch '^[/\\]') {
    if (-not $homeDir) { $script:FolderError = 'type an absolute path (/...)'; return $null }
    $t = $homeDir + '/' + $t
  }
  $t = ($t -replace '\\', '/') -replace '/{2,}', '/'
  if ($t.Length -gt 1) { $t = $t.TrimEnd('/') }
  if (-not $t) { $t = '/' }
  if ($t -match '[\x00-\x1f\x7f<>:"|?*]') { $script:FolderError = 'a Windows drive cannot have < > : " | ? * in its path'; return $null }
  if ($t -match '(^|/)\.\.?(/|$)') { $script:FolderError = 'no . or .. in the path'; return $null }
  if ($t.Length -gt 1000) { $script:FolderError = 'the path is too long'; return $null }
  return $t
}

# SSHFS-Win: \\sshfs.r\user@host!port\path is the absolute path (\\sshfs\... would be relative to the login directory)
function Get-Unc($c, $path) {
  $unc = "\\sshfs.r\$($c.Link.User)@$($c.UncHost)!$($c.Port)"
  if ($path -ne '/') { $unc += $path.Replace('/', '\') }
  return $unc
}

function Get-DriveList($c) {
  return ((@($c.Maps) | ForEach-Object { "$($_.Letter) $($_.Path)" }) -join ', ')
}

function First-FreeLetter {
  $free = @(Get-FreeLetters)
  if ($free.Count) { return "$($free[0]):" }
  return ''
}

function Fill-Letters($c) {
  if ($c.Kind -ne 'drive') { return }
  $c.LetterChoice = First-FreeLetter
  $script:PanelDirty = $true
}

function Add-Control($form, $c, $x, $y, $w, $h) {
  $c.Location = New-Object Drawing.Point($x, $y)
  $c.Size = New-Object Drawing.Size($w, $h)
  $form.Controls.Add($c)
  return $c
}

# a connection is a row of the helper window, which shows its details and settings (no window of its own)
function New-Connection($link) {
  $c = @{
    Link = $link; Kind = $link.Kind; Phase = 'input'; Since = (Get-Date); LastTry = (Get-Date)
    Tunnel = $null; Keyscan = $null; NetUse = $null; Client = $null
    Port = 0; Host = '127.0.0.1'; UncHost = 'localhost'; WebLocal = 0; Maps = (New-Object Collections.ArrayList); Pending = (New-Object Collections.ArrayList); Password = ''; Tries = 0
    Remap = $false; Verifying = $false; Mapping = $false; Feeder = $null; Watcher = $null; FedAt = $null; Typing = $false; LogNoted = $false; KeyLine = ''; Ended = $false; ConnectedAt = $null
    StateText = ''; Lines = (New-Object Collections.ArrayList); PwDraft = ''; FolderText = ''; LetterChoice = ''
  }
  $c.Dir = Join-Path $ConnDir ("$($link.Kind)-$($link.Id)-" + (Get-Date).ToString('HHmmssfff'))
  New-Item -ItemType Directory -Force -Path $c.Dir | Out-Null
  if ($c.Kind -eq 'drive') {
    $c.FolderText = (Get-FolderChoices $link)[0].Label
    $c.LetterChoice = First-FreeLetter
  }
  if ($c.Kind -ne 'shell') { $c.StateText = 'Paste the password of the link (slurp shows it), then ' + $GoText[$c.Kind] + '.' }
  else { $c.StateText = 'Opening the terminal: it asks for the password of the link (slurp shows it).' }
  [void]$script:Conns.Add($c)
  Say $c "link $($link.Id) of $($link.Alias) (host key $($link.Fingerprint))"
  if (-not $Ssh) { Say $c 'ssh.exe not found: install the Windows feature "OpenSSH Client" (Settings > Apps > Optional features)' }
  Log "new $($c.Kind) link $($link.Id) of $($link.Alias)"
  return $c
}

# what the connection is: of which slurp, the route, read only, until when it takes logins
function Get-Detail($c) {
  $link = $c.Link
  $expires = ''
  if ($link.Expires -gt 0) { $expires = ' · new logins until ' + [DateTimeOffset]::FromUnixTimeSeconds($link.Expires).LocalDateTime.ToString('ddd HH:mm') }
  return "$($KindWord[$c.Kind]) of $($link.Alias) · " + $(if ($link.RelayKind -eq 'direct') { "directly to $($link.Host):$($link.Port)" } elseif ($link.RelayKind -eq 'tcp') { "relay $($link.RelayHost), at $($link.Host):$($link.Port)" } else { "relay $($link.RelayHost)" }) + $(if ($link.ReadOnly) { ' · read only' } else { '' }) + $expires
}

# the helper window, with this connection selected
function Show-Window($c) {
  Show-Panel
  Select-Conn $c
}

function Say($c, $text) {
  [void]$c.Lines.Add((Get-Date).ToString('HH:mm:ss') + '  ' + $text)
  if ($c.Lines.Count -gt 500) { $c.Lines.RemoveRange(0, 100) }
  $script:PanelDirty = $true
}

# a question in a message box, in front of the helper window when it shows (Yes, No, Cancel, OK)
function Ask($text, $buttons = 'YesNo', $icon = 'Question', $default = 'Button2') {
  $owner = $null
  if ($script:Panel -and $script:Panel.Form.Visible) { $owner = $script:Panel.Form }
  if ($owner) { return [string][Windows.Forms.MessageBox]::Show($owner, $text, 'slurp helper', $buttons, $icon, $default) }
  return [string][Windows.Forms.MessageBox]::Show($text, 'slurp helper', $buttons, $icon, $default)
}

function Clear-Password($c) {
  $c.PwDraft = ''
  $p = $script:Panel
  if ($p -and $p.Shown -eq $c) { $p.Loading = $true; $p.Pw.Text = ''; $p.Loading = $false }
}

function Notify($text, $kind = 'Info') {
  try { $script:Tray.ShowBalloonTip(6000, 'slurp helper', $text, $kind) } catch { }
}

$Busy = @('tunnel', 'verify', 'login', 'opening', 'mapping', 'reconnecting')

function Set-Phase($c, $phase, $text) {
  if ($phase -eq 'connected' -and -not $c.ConnectedAt) { $c.ConnectedAt = Get-Date }
  if ($phase -eq 'input') { $c.ConnectedAt = $null }
  $c.Phase = $phase
  $c.Since = Get-Date
  $c.StateText = $text
  $script:PanelDirty = $true
}

function Stop-All($c) {
  Stop-Proc $c.Keyscan
  Stop-Proc $c.Watcher
  Stop-Proc $c.NetUse
  Stop-Proc $c.Client
  Stop-Proc $c.Tunnel
  $c.Client = $null
  $c.Tunnel = $null
}

function Fail($c, $text) {
  Say $c "failed: $text"
  Log "$($c.Kind) $($c.Link.Id): failed: $text"
  Unmap-Drive $c
  $c.Pending.Clear()
  Stop-All $c
  Set-Phase $c 'input' "Failed: $text"
  if (-not $script:Quitting) { Show-Window $c }
}

function Unmap-Drive($c) {
  if (-not $c.Maps) { return }
  foreach ($m in @($c.Maps)) {
    try {
      $p = Start-Proc 'net.exe' "use $($m.Letter) /delete /y" $null -Capture
      $null = $p.WaitForExit(10000)
    } catch { }
    Say $c "unmapped $($m.Letter)"
  }
  $c.Maps.Clear()
}

function Disconnect($c) {
  Unmap-Drive $c
  Stop-All $c
}

# ---- the steps
function Start-Tunnel($c) {
  $link = $c.Link
  Remove-Item (Join-Path $c.Dir 'tunnel.log') -Force -ErrorAction SilentlyContinue
  $known = ''
  $batch = 'yes'
  if ($link.RelayKind -eq 'ssh-j') { $known = '-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=relay_hosts' }
  if ($link.RelayKind -eq 'serveo') {
    # (anonymous at serveo.net: keyboard-interactive without prompts, which BatchMode refuses; no keys of yours there)
    $known = '-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=relay_hosts -o PubkeyAuthentication=no -o PasswordAuthentication=no -o PreferredAuthentications=keyboard-interactive'
    $batch = 'no'
  }
  $arguments = "-N -v -E tunnel.log -o BatchMode=$batch -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 " +
               "-o ServerAliveCountMax=3 $known -L 127.0.0.1:$($c.Port):$($link.Host):$($link.Port) $($link.Jump)"
  $c.Tunnel = Start-Proc $Ssh $arguments $c.Dir
}

function Get-LogError($path) {
  $text = Read-Text $path
  $lines = @($text -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^debug' })
  if ($lines.Count -gt 0) { return ($lines | Select-Object -Last 3) -join ' / ' }
  return 'ssh ended (see Logs)'
}

function Begin-Connect($c) {
  if (-not $Ssh) { Fail $c 'ssh.exe not found: install the Windows feature "OpenSSH Client" (Settings > Apps > Optional features)'; return }
  if ($c.Kind -eq 'drive' -and -not (Test-Sshfs)) {
    $answer = Ask "A drive needs WinFsp and SSHFS-Win, which are not installed.`r`n`r`nInstall them now with winget? (Windows asks for permission; then press Map drive again.)`r`n`r`nNo: open the download page instead." 'YesNoCancel' 'Question' 'Button1'
    if ($answer -eq 'Yes') {
      Say $c 'installing WinFsp and SSHFS-Win with winget ...'
      try {
        Start-Process winget -ArgumentList 'install', '-e', '--id', 'WinFsp.WinFsp', '--accept-source-agreements', '--accept-package-agreements' -Wait
        Start-Process winget -ArgumentList 'install', '-e', '--id', 'SSHFS-Win.SSHFS-Win', '--accept-source-agreements', '--accept-package-agreements' -Wait
      } catch { Say $c "winget failed: $($_.Exception.Message)" }
      if (Test-Sshfs) { Say $c 'installed' } else { Say $c 'still not installed: see https://github.com/winfsp/sshfs-win' }
    } elseif ($answer -eq 'No') {
      Start-Process 'https://github.com/winfsp/sshfs-win#installation'
    }
    return
  }
  $want = $null
  if ($c.Kind -eq 'drive') {
    if (-not $c.LetterChoice) { $c.StateText = 'No free drive letter.'; return }
    $path = Resolve-Folder $c.Link $c.FolderText
    if (-not $path) { $c.StateText = "Folder: $script:FolderError"; Focus-Input $c 'folder'; return }
    $same = @($c.Maps | Where-Object { $_.Path -ceq $path })
    if ($same.Count) { $c.StateText = "$path is mapped already, as $($same[0].Letter)"; return }
    $want = @{ Letter = $c.LetterChoice; Path = $path; Unc = '' }
    if ($c.Phase -eq 'connected') {
      # (another folder through the same tunnel, with the same password)
      [void]$c.Pending.Add($want)
      Begin-NetUse $c
      return
    }
  }
  $c.Password = ([string]$c.PwDraft).Trim()
  if (-not $c.Password -and $c.Kind -ne 'shell') { $c.StateText = 'Paste (or type) the password of the link first.'; Focus-Input $c 'pw'; return }
  $c.Pending.Clear()
  if ($want) { [void]$c.Pending.Add($want) }
  $c.FedAt = $null
  $c.Tries = 0
  if ($c.Link.Straight) {
    # (no tunnel: straight to slurp's machine, which must be reachable from here, or to the relay's public port)
    $c.Port = $c.Link.Port
    $c.Host = $c.Link.Host
    $c.UncHost = $c.Link.Host
    Say $c "connecting $(if ($c.Link.RelayKind -eq 'direct') { 'directly ' } else { '' })to $($c.Host) port $($c.Port) ..."
    Begin-Verify $c
    return
  }
  $c.Port = Get-FreePort
  $c.Host = '127.0.0.1'
  $c.UncHost = 'localhost'
  Say $c "connecting to the relay $($c.Link.RelayHost) (local port $($c.Port)) ..."
  Start-Tunnel $c
  Set-Phase $c 'tunnel' 'Connecting to the relay ...'
}

function Begin-Verify($c) {
  $c.Tries += 1
  if (-not $Keyscan) {
    Fail $c 'ssh-keyscan.exe not found: the host key of slurp cannot be checked (it comes with the OpenSSH Client)'
    return
  }
  $c.Keyscan = Start-Proc $Keyscan "-T 10 -t ed25519 -p $($c.Port) $($c.Host)" $c.Dir -Capture
  $reconnecting = ($c.Phase -eq 'reconnecting')
  Set-Phase $c $(if ($reconnecting) { 'reconnecting' } else { 'verify' }) $(if ($reconnecting) { 'Reconnecting: checking slurp ...' } else { 'Checking that slurp answers (its host key) ...' })
  $c.Verifying = $true
}

function Begin-NetUse($c) {
  $m = $c.Pending[0]
  $m.Unc = Get-Unc $c $m.Path
  Say $c "mapping $($m.Letter) to $($m.Path) ($($m.Unc)) ..."
  $c.NetUse = Start-Proc 'net.exe' "use $($m.Letter) `"$($m.Unc)`" `"$($c.Password)`" /user:$($c.Link.User) /persistent:no" $null -Capture
  Set-Phase $c $(if ($c.Remap) { 'reconnecting' } else { 'mapping' }) $(if ($c.Remap) { 'Reconnecting: mapping again ...' } else { 'Logging in and mapping (password) ...' })
  $c.Mapping = $true
}

# web view and shell: ssh to slurp through the tunnel; the host key is the one just checked (known_hosts of this
# connection). Web view: slurp-open.exe --feed starts with ssh: it watches ssh's console for the password prompt and
# types the password then; whether the login worked does not rely on ssh's log (Windows may not let it be read while
# ssh runs): the web view's local port answers (ssh opens it only after the login); a wrong password ends ssh
# (NumberOfPasswordPrompts=1). Shell: ssh in its terminal window asks for the password itself, the user types it
# there (3 tries, as ssh's default; 5 wrong passwords lock the link); the terminal is the user's from the start (it is
# never ended for a timeout). slurp-open.exe --watch sees the shell's prompt after the password one (or ssh's log
# says so): then this window hides.
function Begin-Login($c, [switch]$Visible) {
  $link = $c.Link
  Remove-Item (Join-Path $c.Dir 'client.log') -Force -ErrorAction SilentlyContinue
  $prompts = $(if ($c.Kind -eq 'shell') { 3 } else { 1 })
  $common = "-p $($c.Port) -E client.log -o LogLevel=DEBUG1 -o HostKeyAlias=$($link.Alias) -o UserKnownHostsFile=known_hosts " +
            "-o GlobalKnownHostsFile=known_hosts -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o PubkeyAuthentication=no " +
            "-o PreferredAuthentications=password -o NumberOfPasswordPrompts=$prompts -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
  $c.FedAt = $null
  $c.Typing = $false
  $c.LogNoted = $false
  if ($c.Kind -eq 'web') {
    $c.WebLocal = Get-FreePort
    $arguments = "-N -o ExitOnForwardFailure=yes -L 127.0.0.1:$($c.WebLocal):127.0.0.1:$($link.WebPort) $common $($link.User)@$($c.Host)"
    $c.Client = Start-Proc $Ssh $arguments $c.Dir -Visible:$Visible
    Say $c "logging in, the web view comes to local port $($c.WebLocal) ..."
  } else {
    $arguments = "-o RequestTTY=yes $common $($link.User)@$($c.Host)"
    $c.Client = Start-Proc $Ssh $arguments $c.Dir -Visible
    $c.Feeder = $null
    $c.Typing = $true
    Start-Watch $c
    Say $c 'the terminal window is open: ssh asks for the password there'
    Set-Phase $c 'login' 'Give the password in the terminal (right-click pastes there).'
    return
  }
  if ($Visible) { $c.Feeder = $null; $c.Typing = $true }
  else { Start-Feed $c }
  Set-Phase $c 'login' 'Logging in (password) ...'
}

function Start-Feed($c) {
  $c.Feeder = $null
  if (-not $Native) { Say $c 'slurp-open.exe is missing: the password cannot be typed in for you'; return }
  try {
    $f = Start-Proc $OpenExe "--feed $($c.Client.Id) 45" $c.Dir -Stdin
    $f.StandardInput.WriteLine($c.Password)
    $f.StandardInput.Close()
    $c.Feeder = $f
  } catch { Say $c "the password cannot be typed in for you: $($_.Exception.Message)" }
}

function Start-Watch($c) {
  $c.Watcher = $null
  if (-not $Native) { return }
  try { $c.Watcher = Start-Proc $OpenExe "--watch $($c.Client.Id) 300" $c.Dir }
  catch { Say $c "the login cannot be watched: $($_.Exception.Message)" }
}

# (the terminal is the user's: whatever the helper cannot see, it stays open, and so does this window)
function Shell-In($c, $how) {
  Stop-Proc $c.Watcher
  $c.Watcher = $null
  Say $c "logged in ($how)"
  Connected $c 'The terminal is open, logged in (closing it ends the shell).' "Shell open: $($c.Link.Title)"
}

$WatchErrors = @{ 2 = "no console of ssh"; 5 = "no login within 5 minutes"; 6 = "its screen cannot be read" }
$FeedErrors = @{ 2 = "no console of ssh"; 3 = "its input cannot be opened"; 4 = "the keys were not taken"; 5 = "no password prompt within 45 s"; 6 = "its screen cannot be read" }

function Open-Result($c, $only) {
  if ($c.Kind -eq 'drive') { foreach ($m in $(if ($only) { @($only) } else { @($c.Maps) })) { Start-Process explorer.exe "$($m.Letter)\" } }
  elseif ($c.Kind -eq 'web' -and $c.WebLocal) { Start-Process "http://localhost:$($c.WebLocal)/?token=$($c.Link.Token)" }
}

function Connected($c, $text, $note) {
  Set-Phase $c 'connected' $text
  Say $c $text
  Log "$($c.Kind) $($c.Link.Id): $text"
  Notify $note
  Clear-Password $c
}

function Step($c) {
  $age = ((Get-Date) - $c.Since).TotalSeconds
  $link = $c.Link
  switch ($c.Phase) {
    { $_ -eq 'tunnel' -or ($_ -eq 'reconnecting' -and -not $c.Verifying -and -not $c.Mapping) } {
      if ($c.Tunnel.HasExited) {
        $err = Get-LogError (Join-Path $c.Dir 'tunnel.log')
        if ($c.Phase -eq 'reconnecting' -and $age -lt 120) {
          if (((Get-Date) - $c.LastTry).TotalSeconds -ge 5) {
            Say $c "tunnel: $err; trying again ..."
            $c.LastTry = Get-Date
            Start-Tunnel $c
          }
          return
        }
        Fail $c "the relay connection ended: $err"
        return
      }
      # (the local port listens a moment after the login at the relay: "Local forwarding listening")
      $tlog = Read-Text (Join-Path $c.Dir 'tunnel.log')
      if ($tlog -match 'forwarding listening' -or ($tlog -match 'Authenticated to' -and $age -gt 3) -or
          (-not $tlog -and $age -gt 3 -and (Test-Port $c.Port))) {
        Say $c 'connected to the relay'
        $c.Tries = 0
        Begin-Verify $c
      } elseif ($age -gt 30) {
        $last = @($tlog -split "`r?`n" | Where-Object { $_ }) | Select-Object -Last 1
        Fail $c "no answer from the relay within 30 s (the tunnel's log ends: $(if ($last) { $last } else { 'nothing readable' }))"
      }
      break
    }
    { $_ -eq 'verify' -or ($_ -eq 'reconnecting' -and $c.Verifying) } {
      if (-not $c.Keyscan.HasExited) { return }
      $c.Verifying = $false
      $out = $c.Keyscan.StandardOutput.ReadToEnd()
      $line = @($out -split "`r?`n" | Where-Object { $_ -match 'ssh-ed25519' }) | Select-Object -First 1
      if (-not $line) {
        if ($c.Tries -lt 3) { Say $c 'slurp did not answer yet, trying again ...'; Start-Sleep -Milliseconds 700; Begin-Verify $c; return }
        if ($link.RelayKind -eq 'direct') { Fail $c "slurp does not answer at $($c.Host) port $($c.Port): is this computer in a network that reaches it (VPN?), is slurp running, the link still valid?"; return }
        if ($link.RelayKind -eq 'tcp') { Fail $c "slurp does not answer at $($c.Host) port $($c.Port): is slurp running, the link still valid? (A free pinggy tunnel lasts 60 minutes, then it has a new address: open the link from slurp again.)"; return }
        Fail $c 'slurp does not answer through the relay: is slurp running, and the link still valid (not revoked or expired)?'
        return
      }
      $fp = Get-KeyFingerprint $line
      if ($fp -cne $link.Fingerprint) {
        Fail $c "WRONG HOST KEY: got $fp, the link says $($link.Fingerprint). Something between you and slurp is not slurp: do not use this link."
        return
      }
      Say $c "host key checked: $fp"
      $parts = $line.Trim() -split '\s+'
      [IO.File]::WriteAllText((Join-Path $c.Dir 'known_hosts'), "$($link.Alias) $($parts[1]) $($parts[2])`n")
      if ($c.Kind -eq 'drive') { Begin-NetUse $c } else { Begin-Login $c }
      break
    }
    'login' {
      $logFile = Join-Path $c.Dir 'client.log'
      $log = Read-Text $logFile
      if (-not $c.LogNoted -and $age -gt 3) {
        $c.LogNoted = $true
        $size = 0
        try { $size = (Get-Item $logFile -ErrorAction Stop).Length } catch { }
        Log "$($c.Kind) $($link.Id): ssh's log: $size bytes, $($log.Length) readable"
      }
      if ($c.Client.HasExited) {
        $hint = ''
        if ($log -match 'Permission denied|Too many authentication failures' -or ($c.FedAt -and -not $log)) {
          $hint = ' (wrong password, or the link takes no more logins: used, expired or revoked; 5 wrong passwords lock it)'
          Clear-Password $c
        }
        Fail $c "the login ended: $(Get-LogError $logFile)$hint"
        return
      }
      if ($c.Feeder -and $c.Feeder.HasExited) {
        $code = $c.Feeder.ExitCode
        $c.Feeder = $null
        if ($code -eq 0) { Say $c 'password given'; $c.FedAt = Get-Date }
        else {
          Say $c "the password could not be typed in for you ($($FeedErrors[[int]$code]), code $code): a window opens where ssh asks for it"
          Stop-Proc $c.Client
          Begin-Login $c -Visible
          return
        }
      }
      $authed = ($log -match 'Authenticated to|Authentication succeeded')
      if ($c.Kind -eq 'shell') {
        if ($authed) { Shell-In $c "ssh's log"; return }
        if ($c.Watcher -and $c.Watcher.HasExited) {
          $code = $c.Watcher.ExitCode
          $c.Watcher = $null
          if ($code -eq 0) { Shell-In $c 'its prompt shows'; return }
          Say $c "the helper cannot see the login in the terminal ($($WatchErrors[[int]$code]), code $code)"
          Connected $c 'The terminal is open: give the password there (closing it ends the shell).' "Shell: $($link.Title)"
          return
        }
        if ($age -gt 300 -or (-not $c.Watcher -and $age -gt 20)) {
          Connected $c 'The terminal is open (the helper could not see the login; closing it ends the shell).' "Shell: $($link.Title)"
        }
        return
      }
      if (-not $authed -and $c.Kind -eq 'web' -and $c.WebLocal -and (Test-Port $c.WebLocal)) { $authed = $true }
      if ($authed) {
        Say $c 'logged in'
        Set-Phase $c 'opening' 'Waiting for the web view ...'
        return
      }
      if ($age -gt $(if ($c.Typing) { 180 } else { 60 })) { Fail $c 'the login took too long' }
      break
    }
    'opening' {
      if ($c.Client.HasExited) { Fail $c "the tunnel to the web view ended: $(Get-LogError (Join-Path $c.Dir 'client.log'))"; return }
      if (Test-Port $c.WebLocal) {
        Connected $c "The web view is open: http://localhost:$($c.WebLocal)" "Web view open in your browser: $($link.Title)"
        Open-Result $c
      } elseif ($age -gt 20) { Fail $c 'the web view did not answer within 20 s' }
      break
    }
    { $_ -eq 'mapping' -or ($_ -eq 'reconnecting' -and $c.Mapping) } {
      $extra = ($c.Maps.Count -gt 0 -and -not $c.Remap) # (a further folder: what is mapped stays, whatever happens)
      if (-not $c.NetUse.HasExited) {
        if ($age -gt 60 -and $extra) {
          Stop-Proc $c.NetUse
          $c.Mapping = $false
          Say $c "failed: mapping $($c.Pending[0].Path) took longer than 60 s"
          Set-Phase $c 'connected' "Not mapped: $($c.Pending[0].Path) (it took longer than 60 s)"
          $c.Pending.Clear()
        } elseif ($age -gt 60) { Fail $c 'mapping took longer than 60 s' }
        return
      }
      $c.Mapping = $false
      $out = ($c.NetUse.StandardOutput.ReadToEnd() + ' ' + $c.NetUse.StandardError.ReadToEnd()).Trim()
      if ($c.NetUse.ExitCode -ne 0) {
        $hint = ''
        if ($out -match '1326|password|Kennwort|mot de passe') { $hint = ' (wrong password? 5 wrong ones lock the link)' }
        elseif ($out -match ' 85|already in use') { $hint = ' (the drive letter is in use: pick another)' }
        elseif ($out -match ' 67|network name') { $hint = ' (wrong password, the link is used up or locked, or SSHFS-Win cannot reach the tunnel)' }
        $m = $c.Pending[0]
        if ($extra) {
          $c.Pending.Clear()
          Say $c "failed: net use $($m.Letter) $($m.Path): $out$hint"
          Set-Phase $c 'connected' "Not mapped: $($m.Path) ($out$hint)"
          return
        }
        $c.Remap = $false
        Fail $c "net use $($m.Path): $out$hint"
        Clear-Password $c
        return
      }
      $m = $c.Pending[0]
      $c.Pending.RemoveAt(0)
      [void]$c.Maps.Add($m)
      Say $c "mapped $($m.Letter) $($m.Path)"
      if ($c.Pending.Count -gt 0) { Begin-NetUse $c; return }
      $again = $c.Remap
      $c.Remap = $false
      Fill-Letters $c
      Set-Phase $c 'connected' "Mapped: $(Get-DriveList $c)"
      if ($again) {
        Notify "Drives are back: $(Get-DriveList $c)"
      } else {
        Log "drive $($link.Id): mapped $($m.Letter) $($m.Path)"
        Notify "Drive $($m.Letter) mapped: $($m.Path)"
        Open-Result $c $m
      }
      break
    }
    'connected' {
      if ($c.Kind -eq 'drive') {
        if ($c.Tunnel.HasExited) {
          Say $c "the tunnel ended: $(Get-LogError (Join-Path $c.Dir 'tunnel.log')); reconnecting ..."
          $c.Pending.Clear()
          foreach ($m in @($c.Maps)) { [void]$c.Pending.Add(@{ Letter = $m.Letter; Path = $m.Path; Unc = '' }) }
          Unmap-Drive $c
          $c.Remap = $true
          $c.Verifying = $false; $c.Mapping = $false
          Start-Tunnel $c
          Set-Phase $c 'reconnecting' 'Connection lost: reconnecting ...'
          $c.LastTry = Get-Date
          Notify "Drives $(@($c.Pending | ForEach-Object { $_.Letter }) -join ', '): connection lost, reconnecting ..." 'Warning'
          if ($link.Expires -gt 0 -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $link.Expires) {
            Say $c 'note: the link takes no new logins any more (expired): extend it in slurp'
          }
        }
      } elseif ($c.Client.HasExited) {
        if ($c.Kind -eq 'shell') {
          # (a failed login ends ssh and its terminal window: keep this window, with why, to try again)
          $log = Read-Text (Join-Path $c.Dir 'client.log')
          if ($log -match 'Permission denied|Too many authentication failures' -and $log -notmatch 'Authenticated to|Authentication succeeded') {
            Fail $c "the login ended: $(Get-LogError (Join-Path $c.Dir 'client.log')) (wrong password, or the link takes no more logins: used, expired or revoked; 5 wrong passwords lock it). $($GoText[$c.Kind]) tries again."
            return
          }
          Say $c 'the shell ended'
          Log "shell $($link.Id): ended"
          Notify "Shell ended: $($link.Title)"
          Stop-All $c
          $c.Ended = $true
          Remove-Connection $c
        } else {
          Fail $c "the tunnel to the web view ended: $(Get-LogError (Join-Path $c.Dir 'client.log')). Paste the password again to reconnect."
        }
      } elseif ($c.Tunnel.HasExited) {
        Stop-Proc $c.Client
      }
    }
  }
}

function Remove-Connection($c) {
  Disconnect $c
  [void]$script:Conns.Remove($c)
  try { Remove-Item -Recurse -Force $c.Dir -ErrorAction SilentlyContinue } catch { }
  $script:PanelDirty = $true
  Update-Panel
}

# ---- links that arrive (from slurp-open.exe)
function Open-Link($url) {
  try { $link = Parse-Link $url } catch {
    Log "refused a link: $($_.Exception.Message)"
    [void][Windows.Forms.MessageBox]::Show("This slurp link cannot be used: $($_.Exception.Message)", 'slurp helper', 'OK', 'Warning')
    return
  }
  foreach ($c in $script:Conns) {
    if ($c.Link.Id -eq $link.Id -and $c.Kind -eq $link.Kind) { Show-Window $c; return }
  }
  if (-not (Test-Trusted $link.Fingerprint)) {
    $answer = [Windows.Forms.MessageBox]::Show(
      "This link is for a slurp this helper does not know yet:`r`n`r`n    $($link.Alias)`r`n    host key $($link.Fingerprint)`r`n    $($link.Title)`r`n`r`nUse it only if you just made it in your own slurp (its link window shows the same host key). A link from anywhere else may lead to someone else's computer.`r`n`r`nTrust this slurp from now on?",
      'slurp helper: a new slurp', 'YesNo', 'Warning', 'Button2')
    if ($answer -ne 'Yes') { Log "a link of the unknown $($link.Alias) was not trusted"; return }
    Add-Trusted $link.Alias $link.Fingerprint
    Log "trusted $($link.Alias) $($link.Fingerprint)"
  }
  $c = New-Connection $link
  Show-Window $c
  # (a shell has nothing to ask here: its terminal opens at once)
  if ($c.Kind -eq 'shell') { Begin-Connect $c }
  if ($link.SlurpVersion -and $link.SlurpVersion -ne $HelperVersion) {
    # (it works on, as far as it knows the link; what slurp changed since needs the helper of the same version)
    Log "link of slurp $($link.SlurpVersion), helper $HelperVersion"
    Say $c "This helper is version $HelperVersion, slurp is $($link.SlurpVersion): set the helper up again from slurp (a link window: Windows helper) to get the same version."
  }
}

function Read-Inbox {
  if (-not (Test-Path $Inbox)) { return }
  foreach ($f in @(Get-ChildItem $Inbox -Filter '*.msg' -ErrorAction SilentlyContinue | Sort-Object Name)) {
    $text = Read-Text $f.FullName
    Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
    $text = $text.Trim()
    if ($text -eq 'show') { Show-Panel }
    elseif ($text -eq 'quit') { Quit-Tray }
    elseif ($text -like 'slurp:*') { Open-Link $text }
  }
}

# ---- what the helper window does with a connection
function Reconnect($c) {
  if ($Busy -contains $c.Phase) { return }
  if ($c.Kind -ne 'shell' -and -not $c.Password) {
    $c.StateText = 'Paste the password of the link, then ' + $GoText[$c.Kind] + '.'
    Show-Window $c
    return
  }
  if ($c.Kind -eq 'shell' -and $c.Phase -eq 'connected') {
    if ((Ask "Reconnect ends the shell in its terminal window and opens a new one.`r`n`r`nGo on?") -ne 'Yes') { return }
  }
  $prev = @($c.Maps | ForEach-Object { @{ Letter = $_.Letter; Path = $_.Path; Unc = '' } })
  Say $c 'reconnecting ...'
  Log "$($c.Kind) $($c.Link.Id): reconnect"
  Disconnect $c
  $c.Pending.Clear()
  Set-Phase $c 'input' 'Reconnecting ...'
  Fill-Letters $c
  $c.PwDraft = $c.Password
  Begin-Connect $c
  # (a drive maps all its folders again, on their letters)
  if ($c.Kind -eq 'drive' -and $prev.Count -and $c.Phase -ne 'input') {
    $c.Pending.Clear()
    foreach ($m in $prev) { [void]$c.Pending.Add($m) }
  }
}

function Disconnect-Keep($c) {
  if ($c.Phase -eq 'input') { return }
  if ($c.Kind -eq 'shell' -and $c.Phase -eq 'connected') {
    if ((Ask 'This ends the shell in its terminal window. Go on?') -ne 'Yes') { return }
  }
  Say $c 'disconnected'
  Log "$($c.Kind) $($c.Link.Id): disconnected"
  Disconnect $c
  $c.Pending.Clear()
  $c.Remap = $false
  Set-Phase $c 'input' ('Disconnected. ' + $GoText[$c.Kind] + ' (or Reconnect) connects again.')
  Fill-Letters $c
}

function Get-StateWord($c) {
  switch ($c.Phase) {
    'connected' { return $(if ($c.Kind -eq 'drive') { 'mapped' } elseif ($c.Kind -eq 'shell') { 'open' } else { 'open' }) }
    'input' { return $(if ($c.StateText -like 'Failed*') { 'failed' } elseif ($c.StateText -like 'Disconnected*') { 'disconnected' } else { 'not connected' }) }
    'reconnecting' { return 'reconnecting' }
    'login' { return $(if ($c.Kind -eq 'shell') { 'password in the terminal' } else { 'logging in' }) }
    'mapping' { return 'mapping' }
    default { return 'connecting' }
  }
}

function Get-Row($c) {
  $link = $c.Link
  $route = $(if ($link.RelayKind -eq 'direct') { "direct $($link.Host):$($link.Port)" } elseif ($link.RelayKind -eq 'tcp') { "$($link.RelayHost): $($link.Host):$($link.Port)" } else { "relay $($link.RelayHost)" })
  $details = switch ($c.Kind) {
    'drive' { $(if ($c.Maps.Count) { Get-DriveList $c } elseif ($c.Pending.Count) { "($($c.Pending[0].Letter) $($c.Pending[0].Path))" } else { '' }) }
    'web' { $(if ($c.Phase -eq 'connected' -and $c.WebLocal) { "http://localhost:$($c.WebLocal)" } else { '' }) }
    default { $(if ($c.Phase -eq 'connected' -and $c.Client -and -not $c.Client.HasExited) { "terminal (ssh pid $($c.Client.Id))" } else { '' }) }
  }
  $since = $(if ($c.ConnectedAt) { $c.ConnectedAt.ToString('HH:mm') } else { '' })
  $until = $(if ($link.Expires -gt 0) { [DateTimeOffset]::FromUnixTimeSeconds($link.Expires).LocalDateTime.ToString('ddd HH:mm') } else { 'until slurp quits' })
  return @($KindWord[$c.Kind], $link.Title, (Get-StateWord $c), $details, $route, $link.Alias, $since, $until)
}

# ---- the helper window: a click on the tray icon. Every connection is a row; the one selected shows below it, with
# its settings (folder, letter and password until it is connected) and its steps; a right click on a row: its menu
$script:Panel = $null
$script:InPanel = $false
$script:PanelDirty = $false

function New-Panel {
  $p = @{ Shown = $null; ShownLines = -1; ShownPhase = ''; ShownLayout = ''; Loading = $false }
  $form = New-Object Windows.Forms.Form
  $form.Text = "slurp helper $HelperVersionText"
  $form.Font = $UiFont
  $form.AutoScaleMode = 'Dpi'
  $form.ClientSize = New-Object Drawing.Size(900, 640)
  $form.MinimumSize = New-Object Drawing.Size(720, 560)
  $form.StartPosition = 'CenterScreen'
  $form.Icon = $Icon
  $p.Form = $form

  $head = New-Object Windows.Forms.Label
  $head.Font = $UiBold
  $head.Anchor = 'Top, Left, Right'
  $p.Head = Add-Control $form $head 12 10 876 22
  $info = New-Object Windows.Forms.Label
  $info.Anchor = 'Top, Left, Right'
  $info.ForeColor = [Drawing.SystemColors]::GrayText
  $p.Info = Add-Control $form $info 12 34 876 34

  $list = New-Object Windows.Forms.ListView
  $list.View = 'Details'
  $list.FullRowSelect = $true
  $list.MultiSelect = $false
  $list.HideSelection = $false
  $list.Anchor = 'Top, Left, Right'
  foreach ($col in @(@('Kind', 70), @('Link', 210), @('State', 120), @('Details', 200), @('Route', 130), @('slurp', 110), @('Since', 50), @('Logins until', 90))) {
    [void]$list.Columns.Add($col[0], $col[1])
  }
  $p.List = Add-Control $form $list 12 72 876 170
  $menu = New-Object Windows.Forms.ContextMenuStrip
  $menu.Add_Opening({ param($s, $e) Build-ConnMenu $s; $e.Cancel = ($s.Items.Count -eq 0) })
  $list.ContextMenuStrip = $menu
  # (a right click selects the row under it first: its menu is about that one)
  $list.Add_MouseDown({ param($s, $e) if ($e.Button -eq 'Right') { $hit = $s.HitTest($e.Location); if ($hit.Item) { $hit.Item.Selected = $true; $hit.Item.Focused = $true } } })

  # the selected connection
  $title = New-Object Windows.Forms.Label
  $title.Font = $UiBold
  $title.Anchor = 'Top, Left, Right'
  $title.AutoEllipsis = $true
  $p.Title = Add-Control $form $title 12 252 876 20
  $detail = New-Object Windows.Forms.Label
  $detail.Anchor = 'Top, Left, Right'
  $detail.ForeColor = [Drawing.SystemColors]::GrayText
  $detail.AutoEllipsis = $true
  $p.Detail = Add-Control $form $detail 12 274 876 18

  $lblF = New-Object Windows.Forms.Label
  $lblF.Text = 'Folder on the cluster:'
  $p.FolderLabel = Add-Control $form $lblF 12 301 150 20
  $folder = New-Object Windows.Forms.ComboBox
  $folder.DropDownStyle = 'DropDown'
  $folder.MaxDropDownItems = 12
  $folder.Anchor = 'Top, Left, Right'
  $p.Folder = Add-Control $form $folder 170 298 520 24
  $lblL = New-Object Windows.Forms.Label
  $lblL.Text = 'Letter:'
  $lblL.Anchor = 'Top, Right'
  $p.LetterLabel = Add-Control $form $lblL 704 301 50 20
  $letter = New-Object Windows.Forms.ComboBox
  $letter.DropDownStyle = 'DropDownList'
  $letter.Anchor = 'Top, Right'
  $p.Letter = Add-Control $form $letter 758 298 70 24
  $hint = New-Object Windows.Forms.Label
  $hint.Text = 'Pick one, or type a path: /abs/path, $SCRATCH/run1, or relative to $HOME. More folders later: right click, Map another folder.'
  $hint.ForeColor = [Drawing.SystemColors]::GrayText
  $hint.Anchor = 'Top, Left, Right'
  $p.Hint = Add-Control $form $hint 170 324 718 18

  $lblPw = New-Object Windows.Forms.Label
  $lblPw.Text = 'Password of the link:'
  $p.PwLabel = Add-Control $form $lblPw 12 351 150 20
  $pw = New-Object Windows.Forms.TextBox
  $pw.UseSystemPasswordChar = $true
  $p.Pw = Add-Control $form $pw 170 348 240 24
  $paste = New-Object Windows.Forms.Button
  $paste.Text = 'Paste'
  $p.Paste = Add-Control $form $paste 418 347 70 26
  $showPw = New-Object Windows.Forms.CheckBox
  $showPw.Text = 'Show'
  $p.ShowPw = Add-Control $form $showPw 496 350 70 22
  $go = New-Object Windows.Forms.Button
  $p.Go = Add-Control $form $go 574 346 140 28

  $state = New-Object Windows.Forms.Label
  $state.Font = $UiBold
  $state.Anchor = 'Top, Left, Right'
  $state.AutoEllipsis = $true
  $p.State = Add-Control $form $state 12 384 876 22
  $steps = New-Object Windows.Forms.TextBox
  $steps.Multiline = $true
  $steps.ReadOnly = $true
  $steps.ScrollBars = 'Vertical'
  $steps.BackColor = [Drawing.SystemColors]::Window
  $steps.Font = New-Object Drawing.Font('Consolas', 9)
  $steps.Anchor = 'Top, Bottom, Left, Right'
  $p.Steps = Add-Control $form $steps 12 410 876 180

  $x = 12
  foreach ($b in @(@('Open', 'open', 90), @('Reconnect', 'reconnect', 90), @('Disconnect', 'disconnect', 90), @('Remove', 'remove', 80), @('Logs', 'logs', 60))) {
    $btn = New-Object Windows.Forms.Button
    $btn.Text = $b[0]
    $btn.Tag = $b[1]
    $btn.Anchor = 'Bottom, Left'
    $null = Add-Control $form $btn $x 600 $b[2] 30
    $btn.Add_Click({ param($s, $e) Panel-Action $s.Tag })
    $p[$b[1]] = $btn
    $x += $b[2] + 8
  }
  $close = New-Object Windows.Forms.Button
  $close.Text = 'Close'
  $close.Anchor = 'Bottom, Right'
  $null = Add-Control $form $close 806 600 82 30
  $close.Add_Click({ param($s, $e) $script:Panel.Form.Hide() })

  # (what is typed belongs to the connection shown: kept with it, also when another row is selected)
  $pw.Add_TextChanged({ param($s, $e) $p = $script:Panel; if ($p -and -not $p.Loading -and $p.Shown) { $p.Shown.PwDraft = $s.Text } })
  $folder.Add_TextChanged({ param($s, $e) $p = $script:Panel; if ($p -and -not $p.Loading -and $p.Shown) { $p.Shown.FolderText = $s.Text } })
  $letter.Add_SelectedIndexChanged({ param($s, $e) $p = $script:Panel; if ($p -and -not $p.Loading -and $p.Shown) { $p.Shown.LetterChoice = [string]$s.SelectedItem } })
  $paste.Add_Click({
    param($s, $e)
    $p = $script:Panel
    $text = ''
    try { $text = [Windows.Forms.Clipboard]::GetText() } catch { }
    if ($p.Shown) { if ($text) { $p.Pw.Text = $text.Trim(); Say $p.Shown 'password pasted' } else { Say $p.Shown 'the clipboard has no text' } }
    [void]$p.Pw.Focus()
    Update-Panel
  })
  $showPw.Add_CheckedChanged({ param($s, $e) $script:Panel.Pw.UseSystemPasswordChar = -not $s.Checked })
  $go.Add_Click({ param($s, $e) $c = $script:Panel.Shown; if ($c) { Begin-Connect $c }; Update-Panel })
  $form.AcceptButton = $go
  $list.Add_SelectedIndexChanged({ param($s, $e) Update-Panel })
  $list.Add_DoubleClick({ param($s, $e) $c = Get-Selected; if ($c -and $c.Phase -eq 'connected' -and $c.Kind -ne 'shell') { Conn-Action 'open' $c } })
  $form.Add_FormClosing({ param($s, $e) if (-not $script:Quitting -and $e.CloseReason -eq 'UserClosing') { $e.Cancel = $true; $s.Hide() } })
  $script:Panel = $p
  return $p
}

function Get-Selected {
  $p = $script:Panel
  if (-not $p -or $p.List.SelectedItems.Count -eq 0) { return $null }
  $c = $p.List.SelectedItems[0].Tag
  if ($script:Conns -contains $c) { return $c }
  return $null
}

function Select-Conn($c) {
  $p = $script:Panel
  if (-not $p) { return }
  Update-Panel
  foreach ($item in $p.List.Items) { if ($item.Tag -eq $c) { $item.Selected = $true; $item.Focused = $true; $item.EnsureVisible() } }
  Update-Panel
  Focus-Input $c ''
}

# the field to fill in next ('pw', 'folder'; '': whichever is first)
function Focus-Input($c, $which) {
  $p = $script:Panel
  if (-not $p -or $p.Shown -ne $c -or $c.Phase -ne 'input') { return }
  if ($which -eq 'folder' -and $c.Kind -eq 'drive') { [void]$p.Folder.Focus() }
  elseif ($c.Kind -ne 'shell') { [void]$p.Pw.Focus() }
  else { [void]$p.Go.Focus() }
}

function Panel-Action($action) {
  $c = Get-Selected
  if ($c) { Conn-Action $action $c }
}

# what the helper window (its buttons, a row's menu) and the tray's menu do with a connection
function Conn-Action($action, $c, $m = $null) {
  switch ($action) {
    'go' { Begin-Connect $c }
    'open' { Open-Result $c }
    'openmap' { Open-Result $c $m }
    'mapanother' { Map-Another $c }
    'unmap' { Unmap-One $c $m }
    'another' { Another-Shell $c }
    'reconnect' { Reconnect $c }
    'disconnect' { Disconnect-Keep $c }
    'logs' { Start-Process explorer.exe $c.Dir }
    'copy' {
      $text = "$($KindWord[$c.Kind]): $($c.Link.Title)`r`n$(Get-Detail $c)`r`n$($c.StateText)`r`n`r`n" + ($c.Lines -join "`r`n")
      try { [Windows.Forms.Clipboard]::SetText($text) } catch { }
    }
    'remove' { Remove-Ask $c }
    'show' { Show-Window $c }
  }
  Update-Panel
}

function Add-ConnItem($menu, $text, $action, $c, $m = $null, [switch]$Disabled) {
  $item = New-Object Windows.Forms.ToolStripMenuItem($text)
  $item.Tag = @{ Action = $action; Conn = $c; Map = $m }
  $item.Enabled = -not $Disabled
  $item.Add_Click({ param($s, $e) Conn-Action $s.Tag.Action $s.Tag.Conn $s.Tag.Map })
  [void]$menu.Items.Add($item)
}

# the menu of a row: what can be done with that connection now
function Build-ConnMenu($menu) {
  $menu.Items.Clear()
  $c = Get-Selected
  if (-not $c) { return }
  $connected = ($c.Phase -eq 'connected')
  $busy = ($Busy -contains $c.Phase)
  if ($c.Phase -eq 'input') { Add-ConnItem $menu $GoText[$c.Kind] 'go' $c }
  if ($c.Kind -eq 'drive') {
    foreach ($m in @($c.Maps)) { Add-ConnItem $menu "Open $($m.Letter) in Explorer   $($m.Path)" 'openmap' $c $m }
    Add-ConnItem $menu 'Map another folder ...' 'mapanother' $c -Disabled:(-not $connected)
    if ($c.Maps.Count -gt 1) { foreach ($m in @($c.Maps)) { Add-ConnItem $menu "Unmap $($m.Letter)   $($m.Path)" 'unmap' $c $m } }
  } elseif ($c.Kind -eq 'web') {
    Add-ConnItem $menu 'Open in browser' 'open' $c -Disabled:(-not $connected)
  } else {
    Add-ConnItem $menu 'Open another shell' 'another' $c
  }
  [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
  Add-ConnItem $menu 'Reconnect' 'reconnect' $c -Disabled:$busy
  $off = $(if ($busy) { 'Stop' } elseif ($c.Kind -eq 'drive' -and $c.Maps.Count) { "Disconnect (unmap $((@($c.Maps) | ForEach-Object { $_.Letter }) -join ', '))" } else { 'Disconnect' })
  Add-ConnItem $menu $off 'disconnect' $c -Disabled:($c.Phase -eq 'input')
  [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
  Add-ConnItem $menu 'Logs (its folder)' 'logs' $c
  Add-ConnItem $menu 'Copy the details' 'copy' $c
  Add-ConnItem $menu 'Remove ...' 'remove' $c
}

function Remove-Ask($c) {
  $what = $(if ($c.Phase -eq 'input') { 'Remove it from the list?' }
            elseif ($c.Kind -eq 'drive') { "Unmap $(Get-DriveList $c) and remove it from the list?" }
            elseif ($c.Kind -eq 'shell') { 'End the shell (its terminal window) and remove it from the list?' }
            else { 'Close the tunnel to the web view and remove it from the list?' })
  if ((Ask "$what`r`n`r`n$($KindWord[$c.Kind]): $($c.Link.Title)") -ne 'Yes') { return }
  $c.Ended = $true
  Remove-Connection $c
}

# a drive: one more folder on another letter, through the same tunnel and with the same password
function Map-Another($c) {
  if ($c.Kind -ne 'drive' -or $c.Phase -ne 'connected') { return }
  $f = New-Object Windows.Forms.Form
  $f.Text = "Map another folder: $($c.Link.Title)"
  $f.Font = $UiFont
  $f.AutoScaleMode = 'Dpi'
  $f.FormBorderStyle = 'FixedDialog'
  $f.MaximizeBox = $false
  $f.MinimizeBox = $false
  $f.ShowInTaskbar = $false
  $f.StartPosition = 'CenterParent'
  $f.Icon = $Icon
  $f.ClientSize = New-Object Drawing.Size(560, 176)
  $lblF = New-Object Windows.Forms.Label
  $lblF.Text = 'Folder on the cluster:'
  $null = Add-Control $f $lblF 12 15 150 20
  $folder = New-Object Windows.Forms.ComboBox
  $folder.DropDownStyle = 'DropDown'
  $folder.MaxDropDownItems = 12
  $mapped = @($c.Maps | ForEach-Object { $_.Path })
  $choices = @(Get-FolderChoices $c.Link)
  foreach ($d in $choices) { $null = $folder.Items.Add($d.Label) }
  $next = @($choices | Where-Object { $mapped -notcontains $_.Path }) | Select-Object -First 1
  $folder.Text = $(if ($next) { $next.Label } else { $choices[0].Label })
  $null = Add-Control $f $folder 170 12 378 24
  $hint = New-Object Windows.Forms.Label
  $hint.Text = 'Pick one, or type a path: /abs/path, $SCRATCH/run1, or relative to $HOME.'
  $hint.ForeColor = [Drawing.SystemColors]::GrayText
  $null = Add-Control $f $hint 170 38 378 18
  $lblL = New-Object Windows.Forms.Label
  $lblL.Text = 'Drive letter:'
  $null = Add-Control $f $lblL 12 69 150 20
  $letter = New-Object Windows.Forms.ComboBox
  $letter.DropDownStyle = 'DropDownList'
  foreach ($l in (Get-FreeLetters)) { $null = $letter.Items.Add("$($l):") }
  if ($letter.Items.Count -gt 0) { $letter.SelectedIndex = 0 }
  $null = Add-Control $f $letter 170 66 80 24
  $err = New-Object Windows.Forms.Label
  $err.ForeColor = [Drawing.Color]::Firebrick
  $null = Add-Control $f $err 12 100 536 30
  $ok = New-Object Windows.Forms.Button
  $ok.Text = 'Map drive'
  $null = Add-Control $f $ok 356 136 100 30
  $cancel = New-Object Windows.Forms.Button
  $cancel.Text = 'Cancel'
  $cancel.DialogResult = 'Cancel'
  $null = Add-Control $f $cancel 464 136 84 30
  $f.AcceptButton = $ok
  $f.CancelButton = $cancel
  $f.Tag = @{ Conn = $c; Folder = $folder; Letter = $letter; Error = $err; Path = $null; Drive = $null }
  $ok.Add_Click({
    param($s, $e)
    $form = $s.FindForm()
    $st = $form.Tag
    $path = Resolve-Folder $st.Conn.Link $st.Folder.Text
    if (-not $path) { $st.Error.Text = "Folder: $script:FolderError"; return }
    $same = @($st.Conn.Maps | Where-Object { $_.Path -ceq $path })
    if ($same.Count) { $st.Error.Text = "$path is mapped already, as $($same[0].Letter)"; return }
    if ($st.Letter.SelectedIndex -lt 0) { $st.Error.Text = 'No free drive letter.'; return }
    $st.Path = $path
    $st.Drive = [string]$st.Letter.SelectedItem
    $form.DialogResult = 'OK'
  })
  $owner = $script:Panel.Form
  $result = $(if ($owner -and $owner.Visible) { $f.ShowDialog($owner) } else { $f.ShowDialog() })
  $st = $f.Tag
  $f.Dispose()
  if ([string]$result -ne 'OK' -or -not $st.Path -or $c.Phase -ne 'connected') { return }
  [void]$c.Pending.Add(@{ Letter = $st.Drive; Path = $st.Path; Unc = '' })
  Begin-NetUse $c
}

# a drive: unmap one folder (the last one: the drive disconnects)
function Unmap-One($c, $m) {
  if ($c.Maps.Count -le 1) { Disconnect-Keep $c; return }
  try {
    $proc = Start-Proc 'net.exe' "use $($m.Letter) /delete /y" $null -Capture
    $null = $proc.WaitForExit(10000)
  } catch { }
  [void]$c.Maps.Remove($m)
  Say $c "unmapped $($m.Letter) $($m.Path)"
  Fill-Letters $c
  Set-Phase $c 'connected' "Mapped: $(Get-DriveList $c)"
}

# a shell: one more terminal through the same link (if the link takes more logins)
function Another-Shell($c) {
  $n = New-Connection $c.Link
  Show-Window $n
  Begin-Connect $n
}

function Update-Panel {
  $p = $script:Panel
  # (changing the list raises its selection event, which calls this again: not into this one)
  if (-not $p -or -not $p.Form.Visible -or $script:InPanel) { return }
  $script:InPanel = $true
  try { Update-PanelNow $p; $script:PanelDirty = $false } finally { $script:InPanel = $false }
}

function Update-PanelNow($p) {
  $conns = @($script:Conns)
  $open = @($conns | Where-Object { $_.Phase -eq 'connected' }).Count
  $p.Head.Text = $(if ($conns.Count) { "$($conns.Count) connection$(if ($conns.Count -ne 1) { 's' }), $open open" } else { 'Nothing open: in slurp, a link window: Open on my computer (or Copy helper link, then Win+R)' })
  $auto = $false
  try { $auto = [bool](Get-ItemProperty -Path $RunKey -Name 'slurp helper' -ErrorAction Stop) } catch { }
  $p.Info.Text = "ssh: $(if ($Ssh) { $Ssh } else { 'missing (Windows feature OpenSSH Client)' })  ·  SSHFS-Win: $(if (Test-Sshfs) { 'installed' } else { 'not installed (drives need it)' })  ·  " +
                 "start with Windows: $(if ($auto) { 'on' } else { 'off' })  ·  known slurps: $(@(Get-Trusted).Count)`r`n" +
                 "Folder: $SlurpHelperDir  ·  right click a row for what it can do; the tray icon's menu has the helper's own settings"
  $list = $p.List
  $selected = Get-Selected
  # (rows are updated in place: a rebuild would lose the selection and flicker)
  if ($list.Items.Count -ne $conns.Count -or @(for ($i = 0; $i -lt $conns.Count; $i++) { if ($list.Items[$i].Tag -ne $conns[$i]) { 1 } }).Count) {
    $list.BeginUpdate()
    $list.Items.Clear()
    foreach ($c in $conns) {
      $row = Get-Row $c
      $item = New-Object Windows.Forms.ListViewItem($row[0])
      foreach ($v in $row[1..($row.Count - 1)]) { [void]$item.SubItems.Add([string]$v) }
      $item.Tag = $c
      [void]$list.Items.Add($item)
      if ($c -eq $selected) { $item.Selected = $true }
    }
    if ($list.SelectedItems.Count -eq 0 -and $list.Items.Count -gt 0) { $list.Items[$list.Items.Count - 1].Selected = $true }
    $list.EndUpdate()
  }
  for ($i = 0; $i -lt $conns.Count; $i++) {
    $row = Get-Row $conns[$i]
    $item = $list.Items[$i]
    for ($j = 0; $j -lt $row.Count; $j++) { if ($item.SubItems[$j].Text -ne [string]$row[$j]) { $item.SubItems[$j].Text = [string]$row[$j] } }
    $state = $row[2]
    $colour = $(if ($state -eq 'failed') { [Drawing.Color]::Firebrick } elseif ($state -eq 'mapped' -or $state -eq 'open') { [Drawing.Color]::DarkGreen } else { [Drawing.SystemColors]::WindowText })
    if ($item.ForeColor -ne $colour) { $item.ForeColor = $colour }
  }
  $c = Get-Selected
  $busy = ($c -and $Busy -contains $c.Phase)
  $p.open.Enabled = [bool]($c -and $c.Phase -eq 'connected' -and $c.Kind -ne 'shell')
  $p.reconnect.Enabled = [bool]($c -and -not $busy)
  $p.disconnect.Enabled = [bool]($c -and $c.Phase -ne 'input')
  $p.disconnect.Text = $(if ($busy) { 'Stop' } else { 'Disconnect' })
  $p.remove.Enabled = [bool]$c
  $p.logs.Enabled = [bool]$c
  $p.open.Text = $(if ($c -and $c.Kind -eq 'web') { 'Browser' } else { 'Explorer' })
  Show-Details $p $c
}

# below the list: the connection selected, its settings while it is not connected, its state and steps
function Show-Details($p, $c) {
  $p.Form.UseWaitCursor = [bool]($c -and $Busy -contains $c.Phase)
  if ($p.Shown -ne $c) {
    # (another row: its own fields; what was typed for the one before stays with it)
    $p.Shown = $c
    $p.ShownLines = -1
    $p.ShownPhase = ''
    $p.Loading = $true
    try {
      $p.Pw.Text = $(if ($c) { $c.PwDraft } else { '' })
      $p.Folder.Items.Clear()
      if ($c -and $c.Kind -eq 'drive') {
        foreach ($d in (Get-FolderChoices $c.Link)) { $null = $p.Folder.Items.Add($d.Label) }
        $p.Folder.Text = $c.FolderText
      } else { $p.Folder.Text = '' }
    } finally { $p.Loading = $false }
  }
  $phase = $(if ($c) { $c.Phase } else { '' })
  if ($phase -ne $p.ShownPhase) {
    $p.ShownPhase = $phase
    if ($c -and $c.Kind -eq 'drive' -and $phase -eq 'input') {
      # (the free letters now; the one chosen stays if it is still free)
      $p.Loading = $true
      try {
        $p.Letter.Items.Clear()
        foreach ($l in (Get-FreeLetters)) { $null = $p.Letter.Items.Add("$($l):") }
        $i = $p.Letter.Items.IndexOf($c.LetterChoice)
        if ($i -lt 0 -and $p.Letter.Items.Count -gt 0) { $i = 0 }
        $p.Letter.SelectedIndex = $i
        $c.LetterChoice = [string]$p.Letter.SelectedItem
      } finally { $p.Loading = $false }
    }
  }
  $editable = ($c -and $phase -eq 'input')
  $drive = ($editable -and $c.Kind -eq 'drive')
  $pw = ($editable -and $c.Kind -ne 'shell')
  $layout = "$([bool]$c) $drive $pw $editable"
  if ($layout -ne $p.ShownLayout) {
    $p.ShownLayout = $layout
    foreach ($w in @($p.FolderLabel, $p.Folder, $p.LetterLabel, $p.Letter, $p.Hint)) { $w.Visible = $drive }
    foreach ($w in @($p.PwLabel, $p.Pw, $p.Paste, $p.ShowPw)) { $w.Visible = $pw }
    $p.Go.Visible = $editable
    $p.Title.Visible = [bool]$c
    $p.Detail.Visible = [bool]$c
    $p.Steps.Visible = [bool]$c
    # (the rows not shown take no room: the state and the steps move up)
    $y = 298
    if ($drive) { $p.FolderLabel.Top = $y + 3; $p.Folder.Top = $y; $p.LetterLabel.Top = $y + 3; $p.Letter.Top = $y; $p.Hint.Top = $y + 26; $y += 50 }
    if ($editable) { $p.PwLabel.Top = $y + 3; $p.Pw.Top = $y; $p.Paste.Top = $y - 1; $p.ShowPw.Top = $y + 2; $p.Go.Top = $y - 2; $y += 36 }
    $p.State.Top = $y
    $bottom = $p.open.Top - 10
    $p.Steps.Top = $y + 26
    $p.Steps.Height = [Math]::Max(60, $bottom - $p.Steps.Top)
  }
  if (-not $c) {
    $p.State.Text = $(if ($script:Conns.Count) { 'Select a connection.' } else { '' })
    return
  }
  $p.Go.Text = $GoText[$c.Kind]
  $p.Title.Text = "$($KindWord[$c.Kind]): $($c.Link.Title)"
  $p.Detail.Text = Get-Detail $c
  if ($p.State.Text -ne $c.StateText) { $p.State.Text = $c.StateText }
  $p.State.ForeColor = $(if ($c.StateText -like 'Failed*') { [Drawing.Color]::Firebrick } else { [Drawing.SystemColors]::ControlText })
  # (the steps: new lines appended, the box keeps its scrolling otherwise)
  if ($p.ShownLines -lt 0 -or $p.ShownLines -gt $c.Lines.Count) {
    $p.Steps.Text = (($c.Lines -join "`r`n") + $(if ($c.Lines.Count) { "`r`n" } else { '' }))
    $p.Steps.SelectionStart = $p.Steps.Text.Length
    $p.Steps.ScrollToCaret()
  } elseif ($p.ShownLines -lt $c.Lines.Count) {
    $p.Steps.AppendText((($c.Lines.GetRange($p.ShownLines, $c.Lines.Count - $p.ShownLines)) -join "`r`n") + "`r`n")
  }
  $p.ShownLines = $c.Lines.Count
}

function Show-Panel {
  $p = $script:Panel
  if (-not $p) { $p = New-Panel }
  if (-not $p.Form.Visible) { $p.Form.Show() }
  if ($p.Form.WindowState -eq 'Minimized') { $p.Form.WindowState = 'Normal' }
  if ($Native) { try { [SlurpOpen]::Bring($p.Form.Handle) } catch { } }
  $p.Form.TopMost = $true
  $p.Form.Activate()
  $p.Form.TopMost = $false
  Update-Panel
}

# ---- the tray and its menu
function Add-Item($menu, $text, $action, $tag, [switch]$Disabled, [switch]$Checked) {
  $item = New-Object Windows.Forms.ToolStripMenuItem($text)
  $item.Tag = @{ Action = $action; Conn = $tag }
  $item.Enabled = -not $Disabled
  $item.Checked = [bool]$Checked
  $item.Add_Click({ param($s, $e) Run-Item $s.Tag })
  [void]$menu.Items.Add($item)
  return $item
}

function Describe($c) {
  $text = "$($KindWord[$c.Kind]): $($c.Link.Title)"
  if ($text.Length -gt 60) { $text = $text.Substring(0, 57) + '...' }
  $state = switch ($c.Phase) {
    'connected' { $(if ($c.Kind -eq 'drive') { "$((@($c.Maps) | ForEach-Object { $_.Letter }) -join ' ') mapped" } else { 'open' }) }
    'input' { 'not connected' }
    'reconnecting' { 'reconnecting' }
    default { 'connecting' }
  }
  return "$text  ($state)"
}

function Build-Menu {
  $menu = $script:Menu
  $menu.Items.Clear()
  $head = Add-Item $menu "slurp helper $($HelperVersion): its window ..." 'panel' $null
  $head.Font = New-Object Drawing.Font($head.Font, [Drawing.FontStyle]::Bold)
  if ($script:Conns.Count -eq 0) {
    $null = Add-Item $menu 'nothing open: open a link in slurp' 'none' $null -Disabled
  }
  foreach ($c in @($script:Conns)) {
    $sub = New-Object Windows.Forms.ToolStripMenuItem((Describe $c))
    if ($c.Phase -eq 'connected' -and $c.Kind -ne 'shell') {
      $null = Add-Item $sub.DropDown $OpenText[$c.Kind] 'open' $c
    }
    $null = Add-Item $sub.DropDown 'Show in the helper window' 'show' $c
    $null = Add-Item $sub.DropDown $(if ($c.Kind -eq 'drive' -and $c.Maps.Count) { "Unmap $((@($c.Maps) | ForEach-Object { $_.Letter }) -join ', ')" } else { 'Disconnect' }) 'disconnect' $c
    [void]$menu.Items.Add($sub)
  }
  [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
  $auto = $false
  try { $auto = [bool](Get-ItemProperty -Path $RunKey -Name 'slurp helper' -ErrorAction Stop) } catch { }
  $null = Add-Item $menu 'Start with Windows' 'autostart' $null -Checked:$auto
  $null = Add-Item $menu 'Known slurps ...' 'known' $null
  $null = Add-Item $menu 'Open the helper folder (logs)' 'folder' $null
  $null = Add-Item $menu 'Uninstall ...' 'uninstall' $null
  [void]$menu.Items.Add((New-Object Windows.Forms.ToolStripSeparator))
  $null = Add-Item $menu $(if ($script:Conns.Count) { 'Quit (closes everything above)' } else { 'Quit' }) 'quit' $null
}

function Run-Item($tag) {
  $c = $tag.Conn
  switch ($tag.Action) {
    'open' { Conn-Action 'open' $c }
    'show' { Conn-Action 'show' $c }
    'panel' { Show-Panel }
    'disconnect' { Conn-Action 'disconnect' $c }
    'autostart' {
      try {
        $on = $false
        try { $on = [bool](Get-ItemProperty -Path $RunKey -Name 'slurp helper' -ErrorAction Stop) } catch { }
        if ($on) { Remove-ItemProperty -Path $RunKey -Name 'slurp helper' -ErrorAction SilentlyContinue }
        else { Set-ItemProperty -Path $RunKey -Name 'slurp helper' -Value "`"$OpenExe`" --tray" }
      } catch { Notify "could not change it: $($_.Exception.Message)" 'Warning' }
    }
    'known' {
      $list = Get-Trusted
      $text = $(if ($list.Count) { ($list | ForEach-Object { "$($_[0])   $($_[1])" }) -join "`r`n" } else { 'none' })
      $answer = [Windows.Forms.MessageBox]::Show("The slurps whose links this helper opens without asking:`r`n`r`n$text`r`n`r`nForget them all? (A link then asks again.)", 'slurp helper', 'YesNo', 'Information', 'Button2')
      if ($answer -eq 'Yes') { Remove-Item $TrustFile -Force -ErrorAction SilentlyContinue }
    }
    'folder' { Start-Process explorer.exe $SlurpHelperDir }
    'uninstall' { Uninstall-Helper }
    'quit' { Quit-Tray }
  }
}

function Quit-Tray {
  if ($script:Quitting) { return }
  $script:Quitting = $true
  Log 'tray quits'
  $timer.Stop()
  foreach ($c in @($script:Conns)) { try { $c.Ended = $true; Disconnect $c } catch { } }
  if ($script:Panel) { try { $script:Panel.Form.Close(); $script:Panel.Form.Dispose() } catch { } }
  $script:Tray.Visible = $false
  $script:Tray.Dispose()
  [Windows.Forms.Application]::Exit()
}

function Uninstall-Helper {
  $answer = [Windows.Forms.MessageBox]::Show("Remove the slurp helper from this computer?`r`n`r`nIt closes what is open, removes slurp:// links, the start with Windows, the start menu entry and its folder:`r`n$SlurpHelperDir",
    'slurp helper', 'YesNo', 'Question', 'Button2')
  if ($answer -ne 'Yes') { return }
  Remove-Item -Path 'HKCU:\Software\Classes\slurp' -Recurse -Force -ErrorAction SilentlyContinue
  Remove-ItemProperty -Path $RunKey -Name 'slurp helper' -ErrorAction SilentlyContinue
  Remove-Item (Join-Path ([Environment]::GetFolderPath('Programs')) 'slurp helper.lnk') -Force -ErrorAction SilentlyContinue
  Quit-Tray
  # the folder goes once this PowerShell has ended (slurp-open.exe is loaded in it)
  $dir = $SlurpHelperDir.TrimEnd('\')
  $null = Start-Proc 'cmd.exe' "/c ping -n 4 127.0.0.1 >nul & rmdir /s /q `"$dir`"" $env:TEMP
}

$script:Menu = New-Object Windows.Forms.ContextMenuStrip
$script:Menu.Add_Opening({ param($s, $e) Build-Menu; $e.Cancel = $false })
Build-Menu
$script:Tray = New-Object Windows.Forms.NotifyIcon
$script:Tray.Icon = $Icon
$script:Tray.Text = "slurp helper $HelperVersion"
$script:Tray.ContextMenuStrip = $script:Menu
$script:Tray.Visible = $true
$script:Tray.Add_MouseClick({
  param($s, $e)
  if ($e.Button -eq 'Left') { Show-Panel }
})

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 400
$script:InTick = $false
$script:Ticks = 0
$timer.Add_Tick({
  # (a message box in a tick runs its own message loop, where the timer ticks again: not into this one)
  if ($script:InTick) { return }
  $script:InTick = $true
  try {
    try { Read-Inbox } catch { Log "inbox: $($_.Exception.Message)" }
    foreach ($c in @($script:Conns)) {
      try { Step $c } catch { try { Fail $c $_.Exception.Message } catch { Log "step: $($_.Exception.Message)" } }
    }
    $script:Ticks += 1
    if ($script:PanelDirty -or $script:Ticks % 3 -eq 0) { try { Update-Panel } catch { Log "panel: $($_.Exception.Message)" } }
    $n = @($script:Conns | Where-Object { $_.Phase -eq 'connected' }).Count
    $text = $(if ($n) { "slurp helper $($HelperVersion): $n open" } else { "slurp helper $HelperVersion" })
    if ($script:Tray.Text -ne $text) { $script:Tray.Text = $text }
  } finally { $script:InTick = $false }
})
$timer.Start()
$FirstStart = Join-Path $SlurpHelperDir 'started'
if (-not (Test-Path $FirstStart)) {
  Notify 'The slurp helper runs (this icon). Links you open in slurp come here.'
  try { [IO.File]::WriteAllText($FirstStart, (Get-Date).ToString('s')) } catch { }
}
[Windows.Forms.Application]::Run()
$TrayMutex.ReleaseMutex()

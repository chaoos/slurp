# slurp helper: the tray program on Windows that opens slurp links (slurp://...) with one click and a password.
# The setup (see winhelper.py) puts it into %LOCALAPPDATA%\slurp-helper with slurp-open.exe (winhelper.cs), and
# registers slurp:// for this user (no admin). A click on a link in slurp's web view (or Win+R and the link):
# the browser starts slurp-open.exe, which hands the link to this tray (a file in its inbox) and starts it if needed.
#
# A link is a shell, a drive or the web view of one slurp. For each one the tray shows a window:
#   the password of the link (drive and web view; and the drive letter) -> the tunnel to the relay (ssh -N -L, no
#   password) -> the host key of slurp through it (ssh-keyscan, against the fingerprint in the link, and the slurps
#   known here) -> drive: net use (SSHFS-Win) of the folder picked ($HOME, $SCRATCH, ... or typed in; more
#   folders later, on other letters, through the same tunnel) and Explorer; web view: ssh -N -L (the password typed into its
#   console by slurp-open.exe --feed) and the browser; shell: at once (nothing to ask) ssh in a terminal window,
#   which asks for the password itself (the user types it there; the helper does not ask for it); the window hides
#   when the shell's prompt shows (slurp-open.exe --watch, or ssh's log)
# Windows of links that are open hide in the tray (its menu: open, show, disconnect); quitting closes them all.
# A click on the tray icon shows the helper's window: every connection with its state (mapped folders, local port,
# relay or direct, since when, logins until), its last steps, and Open, Show window, Reconnect, Disconnect, Remove.
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
Log "tray starts (native helper: $Native)"

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
$script:Hidden = @{} # the first time a window hides into the tray it says so

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

function Fill-Letters($c) {
  if (-not $c.Letter) { return }
  $c.Letter.Items.Clear()
  foreach ($l in (Get-FreeLetters)) { $null = $c.Letter.Items.Add("$($l):") }
  if ($c.Letter.Items.Count -gt 0) { $c.Letter.SelectedIndex = 0 }
}

function Add-Control($form, $c, $x, $y, $w, $h) {
  $c.Location = New-Object Drawing.Point($x, $y)
  $c.Size = New-Object Drawing.Size($w, $h)
  $form.Controls.Add($c)
  return $c
}

function New-Connection($link) {
  $c = @{
    Link = $link; Kind = $link.Kind; Phase = 'input'; Since = (Get-Date); LastTry = (Get-Date)
    Tunnel = $null; Keyscan = $null; NetUse = $null; Client = $null
    Port = 0; Host = '127.0.0.1'; UncHost = 'localhost'; WebLocal = 0; Maps = (New-Object Collections.ArrayList); Pending = (New-Object Collections.ArrayList); Password = ''; Tries = 0
    Remap = $false; Verifying = $false; Mapping = $false; Feeder = $null; Watcher = $null; FedAt = $null; Typing = $false; LogNoted = $false; KeyLine = ''; Visible = $false; Ended = $false; ConnectedAt = $null
  }
  $c.Dir = Join-Path $ConnDir ("$($link.Kind)-$($link.Id)-" + (Get-Date).ToString('HHmmss'))
  New-Item -ItemType Directory -Force -Path $c.Dir | Out-Null

  $form = New-Object Windows.Forms.Form
  $form.Text = "slurp $($KindWord[$c.Kind].ToLower()): $($link.Title)"
  $form.Font = $UiFont
  $form.AutoScaleMode = 'Dpi'
  $form.ClientSize = New-Object Drawing.Size(580, 440)
  $form.FormBorderStyle = 'FixedDialog'
  $form.MaximizeBox = $false
  $form.StartPosition = 'CenterScreen'
  $form.Icon = $Icon
  $form.Tag = $c
  $c.Form = $form

  $info = New-Object Windows.Forms.Label
  $expires = ''
  if ($link.Expires -gt 0) {
    $expires = ' · new logins until ' + [DateTimeOffset]::FromUnixTimeSeconds($link.Expires).LocalDateTime.ToString('ddd HH:mm')
  }
  $detail = "$($KindWord[$c.Kind]) of $($link.Alias) · " + $(if ($link.RelayKind -eq 'direct') { "directly to $($link.Host):$($link.Port)" } elseif ($link.RelayKind -eq 'tcp') { "relay $($link.RelayHost), at $($link.Host):$($link.Port)" } else { "relay $($link.RelayHost)" }) + $(if ($link.ReadOnly) { ' · read only' } else { '' }) + $expires
  $info.Text = "$($link.Title)`r`n$detail"
  $null = Add-Control $form $info 12 10 556 36

  $y = 56
  $c.Letter = $null
  $c.Folder = $null
  if ($c.Kind -eq 'drive') {
    $lblF = New-Object Windows.Forms.Label
    $lblF.Text = 'Folder on the cluster:'
    $null = Add-Control $form $lblF 12 ($y + 3) 150 20
    $folder = New-Object Windows.Forms.ComboBox
    $folder.DropDownStyle = 'DropDown'
    $folder.MaxDropDownItems = 12
    foreach ($d in (Get-FolderChoices $link)) { $null = $folder.Items.Add($d.Label) }
    $folder.SelectedIndex = 0
    $c.Folder = Add-Control $form $folder 170 $y 398 24
    $y += 22
    $hint = New-Object Windows.Forms.Label
    $hint.Text = 'Pick one, or type a path: /abs/path, $SCRATCH/run1, or relative to $HOME. Map more later, on other letters.'
    $hint.ForeColor = [Drawing.SystemColors]::GrayText
    $null = Add-Control $form $hint 170 ($y + 2) 398 18
    $y += 26
    $lbl = New-Object Windows.Forms.Label
    $lbl.Text = 'Drive letter:'
    $null = Add-Control $form $lbl 12 ($y + 3) 150 20
    $letter = New-Object Windows.Forms.ComboBox
    $letter.DropDownStyle = 'DropDownList'
    foreach ($l in (Get-FreeLetters)) { $null = $letter.Items.Add("$($l):") }
    if ($letter.Items.Count -gt 0) { $letter.SelectedIndex = 0 }
    $c.Letter = Add-Control $form $letter 170 $y 80 24
    $y += 34
  }
  # (a shell has no password field: ssh asks for the password in the terminal window)
  $askPw = ($c.Kind -ne 'shell')
  $lblPw = New-Object Windows.Forms.Label
  $lblPw.Text = 'Password of the link:'
  $lblPw.Visible = $askPw
  $null = Add-Control $form $lblPw 12 ($y + 3) 150 20
  $pw = New-Object Windows.Forms.TextBox
  $pw.UseSystemPasswordChar = $true
  $pw.Visible = $askPw
  $c.Pw = Add-Control $form $pw 170 $y 220 24
  $paste = New-Object Windows.Forms.Button
  $paste.Text = 'Paste'
  $paste.Tag = $c
  $paste.Visible = $askPw
  $c.Paste = Add-Control $form $paste 398 ($y - 1) 70 26
  $show = New-Object Windows.Forms.CheckBox
  $show.Text = 'Show'
  $show.Tag = $c
  $show.Visible = $askPw
  $c.Show = Add-Control $form $show 476 ($y + 2) 80 22
  if ($askPw) { $y += 36 }

  $state = New-Object Windows.Forms.Label
  $state.Font = $UiBold
  if ($askPw) { $state.Text = 'Paste the password of the link (slurp shows it), then ' + $GoText[$c.Kind] + '.' }
  else { $state.Text = 'Opening the terminal: it asks for the password of the link (slurp shows it).' }
  $c.State = Add-Control $form $state 12 $y 556 22
  $y += 26
  $steps = New-Object Windows.Forms.TextBox
  $steps.Multiline = $true
  $steps.ReadOnly = $true
  $steps.ScrollBars = 'Vertical'
  $steps.BackColor = [Drawing.SystemColors]::Window
  $c.Steps = Add-Control $form $steps 12 $y 556 (390 - $y - 12)

  $go = New-Object Windows.Forms.Button
  $go.Text = $GoText[$c.Kind]
  $go.Tag = $c
  $c.Go = Add-Control $form $go 12 398 116 30
  $open = New-Object Windows.Forms.Button
  $open.Text = $OpenText[$c.Kind]
  $open.Tag = $c
  $open.Enabled = $false
  $open.Visible = ($c.Kind -ne 'shell')
  $c.Open = Add-Control $form $open 136 398 124 30
  $logs = New-Object Windows.Forms.Button
  $logs.Text = 'Logs'
  $logs.Tag = $c
  $c.Logs = Add-Control $form $logs 268 398 70 30
  $hide = New-Object Windows.Forms.Button
  $hide.Text = 'Hide'
  $hide.Tag = $c
  $c.Hide = Add-Control $form $hide 346 398 80 30
  $close = New-Object Windows.Forms.Button
  $close.Text = 'Close'
  $close.Tag = $c
  $c.Close = Add-Control $form $close 434 398 134 30
  $form.AcceptButton = $go

  $go.Add_Click({ param($s, $e) Begin-Connect $s.Tag })
  $paste.Add_Click({
    param($s, $e)
    $c = $s.Tag
    $text = ''
    try { $text = [Windows.Forms.Clipboard]::GetText() } catch { }
    if ($text) { $c.Pw.Text = $text.Trim(); Say $c 'password pasted' } else { Say $c 'the clipboard has no text' }
    [void]$c.Pw.Focus()
  })
  $show.Add_CheckedChanged({ param($s, $e) $s.Tag.Pw.UseSystemPasswordChar = -not $s.Checked })
  $open.Add_Click({ param($s, $e) Open-Result $s.Tag })
  $logs.Add_Click({ param($s, $e) Start-Process explorer.exe $s.Tag.Dir })
  $hide.Add_Click({ param($s, $e) Hide-Window $s.Tag })
  $close.Add_Click({ param($s, $e) On-CloseButton $s.Tag })
  $form.Add_FormClosing({ param($s, $e) On-Closing $s.Tag $e })
  $form.Add_FormClosed({ param($s, $e) Remove-Connection $s.Tag })
  # (a shell has nothing to ask here: its terminal opens at once)
  $form.Add_Shown({ param($s, $e) $c = $s.Tag; Bring-Window $c; if ($c.Pw.Visible) { [void]$c.Pw.Focus() } else { [void]$c.Go.Focus() }
    if ($c.Kind -eq 'shell' -and $c.Phase -eq 'input') { Begin-Connect $c } })

  [void]$script:Conns.Add($c)
  Say $c "link $($link.Id) of $($link.Alias) (host key $($link.Fingerprint))"
  if (-not $Ssh) { Say $c 'ssh.exe not found: install the Windows feature "OpenSSH Client" (Settings > Apps > Optional features)' }
  Log "new $($c.Kind) link $($link.Id) of $($link.Alias)"
  return $c
}

function Bring-Window($c) {
  $f = $c.Form
  $c.Visible = $true
  if (-not $f.Visible) { $f.Show() }
  if ($f.WindowState -eq 'Minimized') { $f.WindowState = 'Normal' }
  if ($Native) { try { [SlurpOpen]::Bring($f.Handle) } catch { } }
  $f.TopMost = $true
  $f.Activate()
  $f.TopMost = $false
}

function Show-Window($c) {
  Bring-Window $c
}

function Hide-Window($c) {
  $c.Visible = $false
  $c.Form.Hide()
  if (-not $script:Hidden[$c.Kind] -and $c.Phase -ne 'input') {
    $script:Hidden[$c.Kind] = $true
    Notify "$($KindWord[$c.Kind]) stays open: the slurp icon in the tray (^ at the clock) shows it again, and disconnects it."
  }
}

function Say($c, $text) {
  $c.Steps.AppendText((Get-Date).ToString('HH:mm:ss') + '  ' + $text + "`r`n")
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
  $c.State.Text = $text
  $busy = $Busy -contains $phase
  $editable = ($phase -eq 'input')
  $c.Go.Enabled = $editable
  $c.Pw.Enabled = $editable
  $c.Paste.Enabled = $editable
  $more = ($editable -or ($phase -eq 'connected' -and $c.Kind -eq 'drive'))
  if ($c.Letter) { $c.Letter.Enabled = $more; $c.Folder.Enabled = $more }
  if ($c.Kind -eq 'drive') {
    $c.Go.Enabled = $more
    $c.Go.Text = $(if ($phase -eq 'connected') { 'Map another' } else { $GoText['drive'] })
  }
  $c.Open.Enabled = ($phase -eq 'connected')
  if ($phase -eq 'connected') { $c.Close.Text = $(if ($c.Kind -eq 'drive') { 'Unmap and close' } else { 'Disconnect' }) }
  elseif ($busy) { $c.Close.Text = 'Stop' }
  else { $c.Close.Text = 'Close' }
  $c.Form.UseWaitCursor = $busy
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
  if (-not $c.Visible -and -not $script:Quitting) { Show-Window $c }
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
    $answer = [Windows.Forms.MessageBox]::Show($c.Form,
      "A drive needs WinFsp and SSHFS-Win, which are not installed.`r`n`r`nInstall them now with winget? (Windows asks for permission; then press Map drive again.)`r`n`r`nNo: open the download page instead.",
      'slurp helper', 'YesNoCancel', 'Question')
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
    if ($c.Letter.SelectedIndex -lt 0) { $c.State.Text = 'No free drive letter.'; return }
    $path = Resolve-Folder $c.Link $c.Folder.Text
    if (-not $path) { $c.State.Text = "Folder: $script:FolderError"; [void]$c.Folder.Focus(); return }
    $same = @($c.Maps | Where-Object { $_.Path -ceq $path })
    if ($same.Count) { $c.State.Text = "$path is mapped already, as $($same[0].Letter)"; return }
    $want = @{ Letter = [string]$c.Letter.SelectedItem; Path = $path; Unc = '' }
    if ($c.Phase -eq 'connected') {
      # (another folder through the same tunnel, with the same password)
      [void]$c.Pending.Add($want)
      Begin-NetUse $c
      return
    }
  }
  $c.Password = $c.Pw.Text.Trim()
  if (-not $c.Password -and $c.Kind -ne 'shell') { $c.State.Text = 'Paste (or type) the password of the link first.'; [void]$c.Pw.Focus(); return }
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
    Set-Phase $c 'login' 'Give the password in the terminal (right-click pastes). This window hides when you are in.'
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
  if ($c.Visible) { Hide-Window $c }
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
  $c.Pw.Text = ''
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
          $c.Pw.Text = ''
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
        if ($c.Visible) { Hide-Window $c }
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
        $c.Pw.Text = ''
        return
      }
      $m = $c.Pending[0]
      $c.Pending.RemoveAt(0)
      [void]$c.Maps.Add($m)
      Say $c "mapped $($m.Letter) $($m.Path)"
      if ($c.Pending.Count -gt 0) { Begin-NetUse $c; return }
      $again = $c.Remap
      $first = ($c.Maps.Count -eq 1)
      $c.Remap = $false
      Fill-Letters $c
      Set-Phase $c 'connected' "Mapped: $(Get-DriveList $c)"
      if ($again) {
        Notify "Drives are back: $(Get-DriveList $c)"
      } else {
        Log "drive $($link.Id): mapped $($m.Letter) $($m.Path)"
        Notify "Drive $($m.Letter) mapped: $($m.Path)"
        Open-Result $c $m
        if ($first -and $c.Visible) { Hide-Window $c }
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
          $c.Form.Close()
        } else {
          Fail $c "the tunnel to the web view ended: $(Get-LogError (Join-Path $c.Dir 'client.log')). Paste the password again to reconnect."
        }
      } elseif ($c.Tunnel.HasExited) {
        Stop-Proc $c.Client
      }
    }
  }
}

function On-CloseButton($c) {
  if ($c.Phase -eq 'input' -or $c.Phase -eq 'connected') { $c.Form.Close(); return }
  Say $c 'stopped'
  Disconnect $c
  Set-Phase $c 'input' 'Stopped.'
}

function On-Closing($c, $e) {
  if ($script:Quitting -or $c.Ended) { Disconnect $c; return }
  if ($c.Phase -eq 'connected') {
    $what = $(if ($c.Kind -eq 'drive') { "Unmap $(Get-DriveList $c) and close?" } elseif ($c.Kind -eq 'shell') { 'End the shell (its terminal window) and close?' } else { 'Close the tunnel to the web view?' })
    if ($e.CloseReason -eq 'UserClosing') {
      $answer = [Windows.Forms.MessageBox]::Show($c.Form, "$what`r`n`r`nNo: keep it, in the tray.", 'slurp helper', 'YesNoCancel', 'Question')
      if ($answer -eq 'No') { $e.Cancel = $true; Hide-Window $c; return }
      if ($answer -ne 'Yes') { $e.Cancel = $true; return }
    }
  } elseif ($Busy -contains $c.Phase) {
    Say $c 'stopped'
  }
  Disconnect $c
}

function Remove-Connection($c) {
  Disconnect $c
  [void]$script:Conns.Remove($c)
  try { Remove-Item -Recurse -Force $c.Dir -ErrorAction SilentlyContinue } catch { }
  try { $c.Form.Dispose() } catch { }
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
    Show-Window $c
    $c.State.Text = 'Paste the password of the link, then ' + $GoText[$c.Kind] + '.'
    return
  }
  if ($c.Kind -eq 'shell' -and $c.Phase -eq 'connected') {
    $answer = [Windows.Forms.MessageBox]::Show($script:Panel.Form, "Reconnect ends the shell in its terminal window and opens a new one.`r`n`r`nGo on?", 'slurp helper', 'YesNo', 'Question', 'Button2')
    if ($answer -ne 'Yes') { return }
  }
  $prev = @($c.Maps | ForEach-Object { @{ Letter = $_.Letter; Path = $_.Path; Unc = '' } })
  Say $c 'reconnecting (helper window) ...'
  Log "$($c.Kind) $($c.Link.Id): reconnect"
  Disconnect $c
  $c.Pending.Clear()
  Set-Phase $c 'input' 'Reconnecting ...'
  Fill-Letters $c
  $c.Pw.Text = $c.Password
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
    $answer = [Windows.Forms.MessageBox]::Show($script:Panel.Form, 'This ends the shell in its terminal window. Go on?', 'slurp helper', 'YesNo', 'Question', 'Button2')
    if ($answer -ne 'Yes') { return }
  }
  Say $c 'disconnected (helper window)'
  Log "$($c.Kind) $($c.Link.Id): disconnected"
  Disconnect $c
  $c.Pending.Clear()
  $c.Remap = $false
  Set-Phase $c 'input' ('Disconnected. ' + $GoText[$c.Kind] + ' (or Reconnect in the helper window) connects again.')
  Fill-Letters $c
}

function Get-StateWord($c) {
  switch ($c.Phase) {
    'connected' { return $(if ($c.Kind -eq 'drive') { 'mapped' } elseif ($c.Kind -eq 'shell') { 'open' } else { 'open' }) }
    'input' { return $(if ($c.State.Text -like 'Failed*') { 'failed' } elseif ($c.State.Text -like 'Disconnected*') { 'disconnected' } else { 'not connected' }) }
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

# ---- the helper window: a click on the tray icon
$script:Panel = $null
$script:InPanel = $false

function New-Panel {
  $p = @{ Rows = @() }
  $form = New-Object Windows.Forms.Form
  $form.Text = 'slurp helper'
  $form.Font = $UiFont
  $form.AutoScaleMode = 'Dpi'
  $form.ClientSize = New-Object Drawing.Size(860, 480)
  $form.MinimumSize = New-Object Drawing.Size(640, 380)
  $form.StartPosition = 'CenterScreen'
  $form.Icon = $Icon
  $p.Form = $form

  $head = New-Object Windows.Forms.Label
  $head.Font = $UiBold
  $head.Anchor = 'Top, Left, Right'
  $p.Head = Add-Control $form $head 12 10 836 22
  $info = New-Object Windows.Forms.Label
  $info.Anchor = 'Top, Left, Right'
  $info.ForeColor = [Drawing.SystemColors]::GrayText
  $p.Info = Add-Control $form $info 12 34 836 34

  $list = New-Object Windows.Forms.ListView
  $list.View = 'Details'
  $list.FullRowSelect = $true
  $list.MultiSelect = $false
  $list.HideSelection = $false
  $list.Anchor = 'Top, Bottom, Left, Right'
  foreach ($col in @(@('Kind', 70), @('Link', 200), @('State', 110), @('Details', 190), @('Route', 130), @('slurp', 110), @('Since', 50), @('Logins until', 90))) {
    [void]$list.Columns.Add($col[0], $col[1])
  }
  $p.List = Add-Control $form $list 12 72 836 230

  $steps = New-Object Windows.Forms.TextBox
  $steps.Multiline = $true
  $steps.ReadOnly = $true
  $steps.ScrollBars = 'Vertical'
  $steps.Font = New-Object Drawing.Font('Consolas', 9)
  $steps.Anchor = 'Bottom, Left, Right'
  $p.Steps = Add-Control $form $steps 12 310 836 110

  $x = 12
  foreach ($b in @(@('Open', 'open', 80), @('Show window', 'show', 100), @('Reconnect', 'reconnect', 90), @('Disconnect', 'disconnect', 90), @('Remove', 'remove', 80), @('Logs', 'logs', 60))) {
    $btn = New-Object Windows.Forms.Button
    $btn.Text = $b[0]
    $btn.Tag = $b[1]
    $btn.Anchor = 'Bottom, Left'
    $null = Add-Control $form $btn $x 432 $b[2] 30
    $btn.Add_Click({ param($s, $e) Panel-Action $s.Tag })
    $p[$b[1]] = $btn
    $x += $b[2] + 8
  }
  $more = New-Object Windows.Forms.Button
  $more.Text = 'Helper ...'
  $more.Anchor = 'Bottom, Right'
  $null = Add-Control $form $more 676 432 82 30
  $more.Add_Click({ param($s, $e) $script:Menu.Show($s, (New-Object Drawing.Point(0, $s.Height))) })
  $close = New-Object Windows.Forms.Button
  $close.Text = 'Close'
  $close.Anchor = 'Bottom, Right'
  $null = Add-Control $form $close 766 432 82 30
  $close.Add_Click({ param($s, $e) $script:Panel.Form.Hide() })

  $list.Add_SelectedIndexChanged({ param($s, $e) Update-Panel })
  $list.Add_DoubleClick({ param($s, $e) Panel-Action 'show' })
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

function Panel-Action($action) {
  $c = Get-Selected
  if (-not $c) { return }
  switch ($action) {
    'open' { Open-Result $c }
    'show' { Show-Window $c }
    'reconnect' { Reconnect $c }
    'disconnect' { Disconnect-Keep $c }
    'remove' {
      $answer = [Windows.Forms.MessageBox]::Show($script:Panel.Form, "Disconnect and remove it from the list?`r`n`r`n$($KindWord[$c.Kind]): $($c.Link.Title)", 'slurp helper', 'YesNo', 'Question', 'Button2')
      if ($answer -eq 'Yes') { $c.Ended = $true; $c.Form.Close() }
    }
    'logs' { Start-Process explorer.exe $c.Dir }
  }
  Update-Panel
}

function Update-Panel {
  $p = $script:Panel
  # (changing the list raises its selection event, which calls this again: not into this one)
  if (-not $p -or -not $p.Form.Visible -or $script:InPanel) { return }
  $script:InPanel = $true
  try { Update-PanelNow $p } finally { $script:InPanel = $false }
}

function Update-PanelNow($p) {
  $conns = @($script:Conns)
  $open = @($conns | Where-Object { $_.Phase -eq 'connected' }).Count
  $p.Head.Text = $(if ($conns.Count) { "$($conns.Count) connection$(if ($conns.Count -ne 1) { 's' }), $open open" } else { 'Nothing open: in slurp, a link window: Open on my computer (or Copy helper link, then Win+R)' })
  $auto = $false
  try { $auto = [bool](Get-ItemProperty -Path $RunKey -Name 'slurp helper' -ErrorAction Stop) } catch { }
  $p.Info.Text = "ssh: $(if ($Ssh) { $Ssh } else { 'missing (Windows feature OpenSSH Client)' })  ·  SSHFS-Win: $(if (Test-Sshfs) { 'installed' } else { 'not installed (drives need it)' })  ·  " +
                 "start with Windows: $(if ($auto) { 'on' } else { 'off' })  ·  known slurps: $(@(Get-Trusted).Count)`r`n" +
                 "Folder: $SlurpHelperDir  ·  a double click on a row shows its window; the tray icon's menu has the rest"
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
    if ($list.SelectedItems.Count -eq 0 -and $list.Items.Count -gt 0) { $list.Items[0].Selected = $true }
    $list.EndUpdate()
  } else {
    for ($i = 0; $i -lt $conns.Count; $i++) {
      $row = Get-Row $conns[$i]
      $item = $list.Items[$i]
      for ($j = 0; $j -lt $row.Count; $j++) { if ($item.SubItems[$j].Text -ne [string]$row[$j]) { $item.SubItems[$j].Text = [string]$row[$j] } }
      $state = $row[2]
      $colour = $(if ($state -eq 'failed') { [Drawing.Color]::Firebrick } elseif ($state -eq 'mapped' -or $state -eq 'open') { [Drawing.Color]::DarkGreen } else { [Drawing.SystemColors]::WindowText })
      if ($item.ForeColor -ne $colour) { $item.ForeColor = $colour }
    }
  }
  $c = Get-Selected
  $busy = ($c -and $Busy -contains $c.Phase)
  $p.open.Enabled = [bool]($c -and $c.Phase -eq 'connected' -and $c.Kind -ne 'shell')
  $p.show.Enabled = [bool]$c
  $p.reconnect.Enabled = [bool]($c -and -not $busy)
  $p.disconnect.Enabled = [bool]($c -and $c.Phase -ne 'input')
  $p.remove.Enabled = [bool]$c
  $p.logs.Enabled = [bool]$c
  $p.open.Text = $(if ($c -and $c.Kind -eq 'web') { 'Browser' } else { 'Explorer' })
  $text = $(if ($c) { $c.State.Text + "`r`n" + ((@($c.Steps.Lines | Where-Object { $_ }) | Select-Object -Last 6) -join "`r`n") } else { '' })
  if ($p.Steps.Text -ne $text) { $p.Steps.Text = $text; $p.Steps.SelectionStart = $p.Steps.Text.Length; $p.Steps.ScrollToCaret() }
}

function Show-Panel {
  $p = $script:Panel
  if (-not $p) { $p = New-Panel }
  if (-not $p.Form.Visible) { $p.Form.Show() }
  if ($p.Form.WindowState -eq 'Minimized') { $p.Form.WindowState = 'Normal' }
  if ($Native) { try { [SlurpOpen]::Bring($p.Form.Handle) } catch { } }
  $p.Form.Activate()
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
  $head = Add-Item $menu 'slurp helper: its window ...' 'panel' $null
  $head.Font = New-Object Drawing.Font($head.Font, [Drawing.FontStyle]::Bold)
  if ($script:Conns.Count -eq 0) {
    $null = Add-Item $menu 'nothing open: open a link in slurp' 'none' $null -Disabled
  }
  foreach ($c in @($script:Conns)) {
    $sub = New-Object Windows.Forms.ToolStripMenuItem((Describe $c))
    if ($c.Phase -eq 'connected' -and $c.Kind -ne 'shell') {
      $null = Add-Item $sub.DropDown $OpenText[$c.Kind] 'open' $c
    }
    $null = Add-Item $sub.DropDown 'Show its window' 'show' $c
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
    'open' { Open-Result $c }
    'show' { Show-Window $c }
    'panel' { Show-Panel }
    'disconnect' { $c.Ended = $true; $c.Form.Close() }
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
  foreach ($c in @($script:Conns)) { try { $c.Ended = $true; $c.Form.Close() } catch { } }
  foreach ($c in @($script:Conns)) { try { Disconnect $c } catch { } }
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
$script:Tray.Text = 'slurp helper'
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
    if ($script:Ticks % 3 -eq 0) { try { Update-Panel } catch { Log "panel: $($_.Exception.Message)" } }
    $n = @($script:Conns | Where-Object { $_.Phase -eq 'connected' }).Count
    $text = $(if ($n) { "slurp helper: $n open" } else { 'slurp helper' })
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

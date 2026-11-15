# slurp helper: the tray program on Windows that opens slurp links (slurp://...) with one click and a password.
# The setup (see winhelper.py) puts it into %LOCALAPPDATA%\slurp-helper with slurp-open.exe (winhelper.cs), and
# registers slurp:// for this user (no admin). A click on a link in slurp's web view (or Win+R and the link):
# the browser starts slurp-open.exe, which hands the link to this tray (a file in its inbox) and starts it if needed.
#
# A link is a shell, a drive or the web view of one slurp. For each one the tray shows a window:
#   the password of the link (and the drive letter) -> the tunnel to the relay (ssh -N -L, no password) -> the
#   host key of slurp through it (ssh-keyscan, against the fingerprint in the link, and the slurps known here) ->
#   drive: net use (SSHFS-Win) and Explorer; web view: ssh -N -L (the password typed into its console by
#   slurp-open.exe --feed) and the browser; shell: ssh in a terminal window (the same way)
# Windows of links that are open hide in the tray (its menu: open, show, disconnect); quitting closes them all.
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
  foreach ($c in $script:Conns) { if ($c.Drive) { $used += $c.Drive.Substring(0, 1) } }
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
  try { return [IO.File]::ReadAllText($path) } catch { return '' }
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
    RelayKind = Need 'r' '^(ssh-j|openssh)$'
    RelayHost = Need 'rh' '^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$'
    Host = Need 'h' '^(slurp|localhost)$'
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
  if ($link.RelayKind -eq 'ssh-j') { $link.Jump = Need 'j' '^[0-9a-f]{24}@[A-Za-z0-9][A-Za-z0-9.-]*$' }
  else { $link.Jump = Need 'j' '^[A-Za-z0-9][A-Za-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?$' }
  if ($link.Port -lt 1 -or $link.Port -gt 65535) { throw "the link's port is not valid" }
  if ((Get-AliasOf $link.Fingerprint) -ne $link.Alias) { throw "the link's host key and its name do not match" }
  if ($kind -eq 'web') {
    $link.WebPort = [int](Need 'wp' '^\d{1,5}$')
    $link.Token = Need 'tok' '^[A-Za-z0-9_-]{8,64}$'
    if ($link.WebPort -lt 1 -or $link.WebPort -gt 65535) { throw "the link's web port is not valid" }
  }
  if (-not $link.Title) { $link.Title = "$kind link $($link.Id)" }
  return $link
}

# ---- the tray icon: a blue dot with an s
function New-TrayIcon {
  $bmp = New-Object Drawing.Bitmap(32, 32)
  $g = [Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'
  $g.TextRenderingHint = 'AntiAliasGridFit'
  $g.Clear([Drawing.Color]::Transparent)
  $brush = New-Object Drawing.SolidBrush([Drawing.Color]::FromArgb(255, 32, 104, 196))
  $g.FillEllipse($brush, 1, 1, 30, 30)
  $font = New-Object Drawing.Font('Segoe UI', 20, [Drawing.FontStyle]::Bold, [Drawing.GraphicsUnit]::Pixel)
  $format = New-Object Drawing.StringFormat
  $format.Alignment = 'Center'
  $format.LineAlignment = 'Center'
  $g.DrawString('s', $font, [Drawing.Brushes]::White, (New-Object Drawing.RectangleF(0, -2, 32, 32)), $format)
  $g.Dispose()
  return [Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$Icon = New-TrayIcon

$script:Conns = New-Object Collections.ArrayList
$script:Quitting = $false
$script:Hidden = @{} # the first time a window hides into the tray it says so

$KindWord = @{ drive = 'Drive'; shell = 'Shell'; web = 'Web view' }
$GoText = @{ drive = 'Map drive'; shell = 'Open terminal'; web = 'Open web view' }
$OpenText = @{ drive = 'Open in Explorer'; shell = ''; web = 'Open in browser' }

# ---- a connection: a link and its window
$UiFont = New-Object Drawing.Font('Segoe UI', 9)
$UiBold = New-Object Drawing.Font('Segoe UI', 10, [Drawing.FontStyle]::Bold)

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
    Port = 0; WebLocal = 0; Drive = ''; Target = ''; Password = ''; Tries = 0
    Remap = $false; Verifying = $false; Mapping = $false; Fed = $false; KeyLine = ''; Visible = $false; Ended = $false
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
  $detail = "$($KindWord[$c.Kind]) of $($link.Alias) · relay $($link.RelayHost)" + $(if ($link.ReadOnly) { ' · read only' } else { '' }) + $expires
  $info.Text = "$($link.Title)`r`n$detail"
  $null = Add-Control $form $info 12 10 556 36

  $y = 56
  $c.Letter = $null
  if ($c.Kind -eq 'drive') {
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
  $lblPw = New-Object Windows.Forms.Label
  $lblPw.Text = 'Password of the link:'
  $null = Add-Control $form $lblPw 12 ($y + 3) 150 20
  $pw = New-Object Windows.Forms.TextBox
  $pw.UseSystemPasswordChar = $true
  $c.Pw = Add-Control $form $pw 170 $y 220 24
  $paste = New-Object Windows.Forms.Button
  $paste.Text = 'Paste'
  $paste.Tag = $c
  $c.Paste = Add-Control $form $paste 398 ($y - 1) 70 26
  $show = New-Object Windows.Forms.CheckBox
  $show.Text = 'Show'
  $show.Tag = $c
  $c.Show = Add-Control $form $show 476 ($y + 2) 80 22
  $y += 36

  $state = New-Object Windows.Forms.Label
  $state.Font = $UiBold
  $state.Text = 'Paste the password of the link (slurp shows it), then ' + $GoText[$c.Kind] + '.'
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
  $form.Add_Shown({ param($s, $e) $c = $s.Tag; Bring-Window $c; [void]$c.Pw.Focus() })

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
  $c.Phase = $phase
  $c.Since = Get-Date
  $c.State.Text = $text
  $busy = $Busy -contains $phase
  $editable = ($phase -eq 'input')
  $c.Go.Enabled = $editable
  $c.Pw.Enabled = $editable
  $c.Paste.Enabled = $editable
  if ($c.Letter) { $c.Letter.Enabled = $editable }
  $c.Open.Enabled = ($phase -eq 'connected')
  if ($phase -eq 'connected') { $c.Close.Text = $(if ($c.Kind -eq 'drive') { 'Unmap and close' } else { 'Disconnect' }) }
  elseif ($busy) { $c.Close.Text = 'Stop' }
  else { $c.Close.Text = 'Close' }
  $c.Form.UseWaitCursor = $busy
}

function Stop-All($c) {
  Stop-Proc $c.Keyscan
  Stop-Proc $c.NetUse
  Stop-Proc $c.Client
  Stop-Proc $c.Tunnel
  $c.Client = $null
  $c.Tunnel = $null
}

function Fail($c, $text) {
  Say $c "failed: $text"
  Log "$($c.Kind) $($c.Link.Id): failed: $text"
  if ($c.Drive) { Unmap-Drive $c }
  Stop-All $c
  Set-Phase $c 'input' "Failed: $text"
  if (-not $c.Visible -and -not $script:Quitting) { Show-Window $c }
}

function Unmap-Drive($c) {
  if (-not $c.Drive) { return }
  try {
    $p = Start-Proc 'net.exe' "use $($c.Drive) /delete /y" $null -Capture
    $null = $p.WaitForExit(10000)
  } catch { }
  Say $c "unmapped $($c.Drive)"
  $c.Drive = ''
}

function Disconnect($c) {
  if ($c.Drive) { Unmap-Drive $c }
  Stop-All $c
}

# ---- the steps
function Start-Tunnel($c) {
  $link = $c.Link
  Remove-Item (Join-Path $c.Dir 'tunnel.log') -Force -ErrorAction SilentlyContinue
  $known = ''
  if ($link.RelayKind -eq 'ssh-j') { $known = '-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=relay_hosts' }
  $arguments = "-N -v -E tunnel.log -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 " +
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
  if ($c.Kind -eq 'drive' -and $c.Letter.SelectedIndex -lt 0) { Fail $c 'no free drive letter'; return }
  $c.Password = $c.Pw.Text.Trim()
  if (-not $c.Password) { $c.State.Text = 'Paste (or type) the password of the link first.'; [void]$c.Pw.Focus(); return }
  if ($c.Kind -eq 'drive') { $c.Target = [string]$c.Letter.SelectedItem }
  $c.Drive = ''
  $c.Fed = $false
  $c.Tries = 0
  $c.Port = Get-FreePort
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
  $c.Keyscan = Start-Proc $Keyscan "-T 10 -t ed25519 -p $($c.Port) 127.0.0.1" $c.Dir -Capture
  $reconnecting = ($c.Phase -eq 'reconnecting')
  Set-Phase $c $(if ($reconnecting) { 'reconnecting' } else { 'verify' }) $(if ($reconnecting) { 'Reconnecting: checking slurp ...' } else { 'Checking that slurp answers (its host key) ...' })
  $c.Verifying = $true
}

function Begin-NetUse($c) {
  $unc = "\\sshfs\$($c.Link.User)@localhost!$($c.Port)"
  Say $c "mapping $($c.Target) to $unc ..."
  $c.NetUse = Start-Proc 'net.exe' "use $($c.Target) $unc `"$($c.Password)`" /user:$($c.Link.User) /persistent:no" $null -Capture
  Set-Phase $c $(if ($c.Remap) { 'reconnecting' } else { 'mapping' }) $(if ($c.Remap) { 'Reconnecting: mapping again ...' } else { 'Logging in and mapping (password) ...' })
  $c.Mapping = $true
}

# web view and shell: ssh to slurp through the tunnel; the host key is the one just checked (known_hosts of this
# connection), the password is typed into ssh's console when ssh asks for it (its log says so)
function Begin-Login($c, [switch]$Visible) {
  $link = $c.Link
  Remove-Item (Join-Path $c.Dir 'client.log') -Force -ErrorAction SilentlyContinue
  $common = "-p $($c.Port) -E client.log -o LogLevel=DEBUG1 -o HostKeyAlias=$($link.Alias) -o UserKnownHostsFile=known_hosts " +
            "-o GlobalKnownHostsFile=known_hosts -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o PubkeyAuthentication=no " +
            "-o PreferredAuthentications=password -o NumberOfPasswordPrompts=1 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
  $c.Fed = $false
  if ($c.Kind -eq 'web') {
    $c.WebLocal = Get-FreePort
    $arguments = "-N -o ExitOnForwardFailure=yes -L 127.0.0.1:$($c.WebLocal):127.0.0.1:$($link.WebPort) $common $($link.User)@127.0.0.1"
    $c.Client = Start-Proc $Ssh $arguments $c.Dir -Visible:$Visible
    Say $c "logging in, the web view comes to local port $($c.WebLocal) ..."
  } else {
    $arguments = "-o RequestTTY=yes $common $($link.User)@127.0.0.1"
    $c.Client = Start-Proc $Ssh $arguments $c.Dir -Visible
    Say $c 'logging in, in a terminal window ...'
  }
  Set-Phase $c 'login' 'Logging in (password) ...'
}

function Send-Password($c) {
  if (-not $Native) { return 9 }
  try {
    $f = Start-Proc $OpenExe "--feed $($c.Client.Id)" $c.Dir -Stdin
    $f.StandardInput.WriteLine($c.Password)
    $f.StandardInput.Close()
    if (-not $f.WaitForExit(5000)) { Stop-Proc $f; return 8 }
    return $f.ExitCode
  } catch { return 7 }
}

function Open-Result($c) {
  if ($c.Kind -eq 'drive' -and $c.Drive) { Start-Process explorer.exe "$($c.Drive)\" }
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
      if ($tlog -match 'forwarding listening' -or ($tlog -match 'Authenticated to' -and $age -gt 3)) {
        Say $c 'connected to the relay'
        $c.Tries = 0
        Begin-Verify $c
      } elseif ($age -gt 30) {
        Fail $c 'no answer from the relay within 30 s'
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
      $log = Read-Text (Join-Path $c.Dir 'client.log')
      if ($c.Client.HasExited) {
        $hint = ''
        if ($log -match 'Permission denied|Too many authentication failures') {
          $hint = ' (wrong password, or the link takes no more logins: used, expired or revoked; 5 wrong passwords lock it)'
          $c.Pw.Text = ''
        }
        Fail $c "the login ended: $(Get-LogError (Join-Path $c.Dir 'client.log'))$hint"
        return
      }
      if (-not $c.Fed -and $log -match 'Next authentication method: password') {
        $c.Fed = $true
        Start-Sleep -Milliseconds 250
        $code = Send-Password $c
        if ($code -eq 0) { Say $c 'password given' }
        elseif ($c.Kind -eq 'shell') {
          Say $c "the password could not be typed in for you (code $code): type or paste it in the terminal window"
          $c.State.Text = 'Type the password in the terminal window (right-click pastes) ...'
        } else {
          Say $c "the password could not be typed in for you (code $code): a window opens where ssh asks for it"
          Stop-Proc $c.Client
          Begin-Login $c -Visible
          $c.Fed = $true
        }
        return
      }
      if ($log -match 'Authenticated to|Authentication succeeded') {
        Say $c 'logged in'
        if ($c.Kind -eq 'shell') {
          Connected $c 'The terminal is open (closing it ends the shell).' "Shell open: $($link.Title)"
          if ($c.Visible) { Hide-Window $c }
        } else {
          Set-Phase $c 'opening' 'Waiting for the web view ...'
        }
        return
      }
      if ($age -gt 60) { Fail $c 'the login took longer than 60 s' }
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
      if (-not $c.NetUse.HasExited) {
        if ($age -gt 60) { Fail $c 'mapping took longer than 60 s' }
        return
      }
      $c.Mapping = $false
      $out = ($c.NetUse.StandardOutput.ReadToEnd() + ' ' + $c.NetUse.StandardError.ReadToEnd()).Trim()
      if ($c.NetUse.ExitCode -ne 0) {
        $hint = ''
        if ($out -match '1326|password|Kennwort|mot de passe') { $hint = ' (wrong password? 5 wrong ones lock the link)' }
        elseif ($out -match ' 85|already in use') { $hint = ' (the drive letter is in use: pick another)' }
        elseif ($out -match ' 67|network name') { $hint = ' (wrong password, the link is used up or locked, or SSHFS-Win cannot reach the tunnel)' }
        $c.Remap = $false
        Fail $c "net use: $out$hint"
        $c.Pw.Text = ''
        return
      }
      $c.Drive = $c.Target
      $first = -not $c.Remap
      $c.Remap = $false
      Say $c "mapped $($c.Drive)"
      Set-Phase $c 'connected' "Mapped as $($c.Drive)"
      if ($first) {
        Log "drive $($link.Id): mapped $($c.Drive)"
        Notify "Drive $($c.Drive) mapped: $($link.Title)"
        Open-Result $c
        if ($c.Visible) { Hide-Window $c }
      } else {
        Notify "Drive $($c.Drive) is back"
      }
      break
    }
    'connected' {
      if ($c.Kind -eq 'drive') {
        if ($c.Tunnel.HasExited) {
          Say $c "the tunnel ended: $(Get-LogError (Join-Path $c.Dir 'tunnel.log')); reconnecting ..."
          Unmap-Drive $c
          $c.Remap = $true
          $c.Verifying = $false; $c.Mapping = $false
          Start-Tunnel $c
          Set-Phase $c 'reconnecting' 'Connection lost: reconnecting ...'
          $c.LastTry = Get-Date
          Notify "Drive $($c.Target): connection lost, reconnecting ..." 'Warning'
          if ($link.Expires -gt 0 -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $link.Expires) {
            Say $c 'note: the link takes no new logins any more (expired): extend it in slurp'
          }
        }
      } elseif ($c.Client.HasExited) {
        if ($c.Kind -eq 'shell') {
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
    $what = $(if ($c.Kind -eq 'drive') { "Unmap $($c.Drive) and close?" } elseif ($c.Kind -eq 'shell') { 'End the shell (its terminal window) and close?' } else { 'Close the tunnel to the web view?' })
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
    if ($text -eq 'show') { Show-All }
    elseif ($text -eq 'quit') { Quit-Tray }
    elseif ($text -like 'slurp:*') { Open-Link $text }
  }
}

function Show-All {
  if ($script:Conns.Count -eq 0) {
    Notify 'The slurp helper runs. Open a link in slurp (a link window: Open on my computer) and it comes here.'
    return
  }
  foreach ($c in @($script:Conns)) { Show-Window $c }
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
    'connected' { $(if ($c.Kind -eq 'drive') { "$($c.Drive) mapped" } else { 'open' }) }
    'input' { 'not connected' }
    'reconnecting' { 'reconnecting' }
    default { 'connecting' }
  }
  return "$text  ($state)"
}

function Build-Menu {
  $menu = $script:Menu
  $menu.Items.Clear()
  $head = Add-Item $menu 'slurp helper' 'none' $null -Disabled
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
    $null = Add-Item $sub.DropDown $(if ($c.Kind -eq 'drive' -and $c.Drive) { "Unmap $($c.Drive)" } else { 'Disconnect' }) 'disconnect' $c
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
  if ($e.Button -eq 'Left') { Show-All }
})

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 400
$script:InTick = $false
$timer.Add_Tick({
  # (a message box in a tick runs its own message loop, where the timer ticks again: not into this one)
  if ($script:InTick) { return }
  $script:InTick = $true
  try {
    try { Read-Inbox } catch { Log "inbox: $($_.Exception.Message)" }
    foreach ($c in @($script:Conns)) {
      try { Step $c } catch { try { Fail $c $_.Exception.Message } catch { Log "step: $($_.Exception.Message)" } }
    }
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

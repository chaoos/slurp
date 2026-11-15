# slurp: maps the drive of a link on Windows (SSHFS-Win), with a small status window.
# slurp puts the settings of the link in front of this script (the $Link... variables, and $SlurpDir: its folder)
# and hands it over as one
# command to paste into PowerShell or cmd. It runs on its own (a hidden PowerShell with a window); closing the
# window unmaps the drive and ends the tunnel.
#
# Steps: tunnel (ssh -N -L to the relay, no password) -> host key check (ssh-keyscan through the tunnel, against
# the fingerprint slurp shows) -> net use with the password -> mapped (the tunnel is watched and restored).

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

$Work = $SlurpDir
try { $Work = (New-Object -ComObject Scripting.FileSystemObject).GetFolder($SlurpDir).ShortPath } catch { }
$TunnelLog = Join-Path $Work 'tunnel.log'
$RelayHosts = Join-Path $Work 'relay_hosts'

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
  $free = @()
  foreach ($code in 68..90) { $l = [string][char]$code; if ($used -notcontains $l) { $free += $l } }
  return $free
}

function Get-FreePort {
  $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
  $l.Start(); $port = $l.LocalEndpoint.Port; $l.Stop()
  return $port
}

function Start-Hidden($file, $arguments, [switch]$Capture) {
  $si = New-Object Diagnostics.ProcessStartInfo
  $si.FileName = $file
  $si.Arguments = $arguments
  $si.UseShellExecute = $false
  $si.CreateNoWindow = $true
  if ($Capture) { $si.RedirectStandardOutput = $true; $si.RedirectStandardError = $true }
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

# ---- the window
$font = New-Object Drawing.Font('Segoe UI', 9)
$bold = New-Object Drawing.Font('Segoe UI', 10, [Drawing.FontStyle]::Bold)
$form = New-Object Windows.Forms.Form
$form.Text = "slurp drive: $LinkTitle"
$form.Font = $font
$form.AutoScaleMode = 'Dpi'
$form.ClientSize = New-Object Drawing.Size(560, 420)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true

function Add-Control($c, $x, $y, $w, $h) {
  $c.Location = New-Object Drawing.Point($x, $y)
  $c.Size = New-Object Drawing.Size($w, $h)
  $form.Controls.Add($c)
  return $c
}

$info = New-Object Windows.Forms.Label
$expiresText = ''
if ($LinkExpires -gt 0) {
  $expiresText = ' · new logins until ' + [DateTimeOffset]::FromUnixTimeSeconds($LinkExpires).LocalDateTime.ToString('HH:mm')
}
$info.Text = "$LinkTitle`r`nRelay: $LinkRelayHost" + $(if ($LinkReadOnly) { ' · read only' } else { '' }) + $expiresText
$null = Add-Control $info 12 10 536 36

$lblLetter = New-Object Windows.Forms.Label
$lblLetter.Text = 'Drive letter:'
$null = Add-Control $lblLetter 12 56 150 20
$letter = New-Object Windows.Forms.ComboBox
$letter.DropDownStyle = 'DropDownList'
foreach ($l in (Get-FreeLetters)) { $null = $letter.Items.Add("$($l):") }
if ($letter.Items.Count -gt 0) { $letter.SelectedIndex = 0 }
$null = Add-Control $letter 170 53 80 24

$lblPw = New-Object Windows.Forms.Label
$lblPw.Text = 'Password of the link:'
$null = Add-Control $lblPw 12 90 150 20
$pw = New-Object Windows.Forms.TextBox
$pw.UseSystemPasswordChar = $true
$null = Add-Control $pw 170 87 210 24
$paste = New-Object Windows.Forms.Button
$paste.Text = 'Paste'
$null = Add-Control $paste 388 86 70 26
$show = New-Object Windows.Forms.CheckBox
$show.Text = 'Show'
$null = Add-Control $show 466 89 80 22

$state = New-Object Windows.Forms.Label
$state.Font = $bold
$state.Text = 'Enter the password of the link and map the drive.'
$null = Add-Control $state 12 126 536 22

$steps = New-Object Windows.Forms.TextBox
$steps.Multiline = $true
$steps.ReadOnly = $true
$steps.ScrollBars = 'Vertical'
$steps.BackColor = [Drawing.SystemColors]::Window
$null = Add-Control $steps 12 152 536 210

$map = New-Object Windows.Forms.Button
$map.Text = 'Map drive'
$null = Add-Control $map 12 378 110 30
$open = New-Object Windows.Forms.Button
$open.Text = 'Open in Explorer'
$open.Enabled = $false
$null = Add-Control $open 130 378 130 30
$logButton = New-Object Windows.Forms.Button
$logButton.Text = 'Tunnel log'
$null = Add-Control $logButton 268 378 100 30
$close = New-Object Windows.Forms.Button
$close.Text = 'Cancel'
$null = Add-Control $close 438 378 110 30
$form.AcceptButton = $map

# ---- the steps
$script:phase = 'input'   # input, tunnel, verify, mapping, mapped, reconnecting, closing
$script:tunnel = $null
$script:keyscan = $null
$script:netuse = $null
$script:port = 0
$script:since = Get-Date
$script:tries = 0
$script:drive = ''
$script:password = ''
$script:remap = $false
$script:lastTry = Get-Date
$script:verifying = $false
$script:mapping = $false
$script:confirmed = $false
$script:target = ''

function Say($text) {
  $steps.AppendText((Get-Date).ToString('HH:mm:ss') + '  ' + $text + "`r`n")
}

function Set-Phase($phase, $text) {
  $script:phase = $phase
  $script:since = Get-Date
  $state.Text = $text
  $busy = @('tunnel', 'verify', 'mapping', 'reconnecting') -contains $phase
  $map.Enabled = ($phase -eq 'input')
  $letter.Enabled = ($phase -eq 'input')
  $pw.Enabled = ($phase -eq 'input')
  $paste.Enabled = ($phase -eq 'input')
  $open.Enabled = ($phase -eq 'mapped')
  if ($phase -eq 'mapped') { $close.Text = 'Unmap and close' }
  elseif ($busy) { $close.Text = 'Stop' }
  else { $close.Text = 'Close' }
  $form.UseWaitCursor = $busy
}

function Stop-All {
  Stop-Proc $script:keyscan
  Stop-Proc $script:netuse
  Stop-Proc $script:tunnel
  $script:tunnel = $null
}

function Fail($text) {
  Say "failed: $text"
  if ($script:drive) { Unmap-Drive }
  Stop-All
  Set-Phase 'input' "Failed: $text"
}

function Unmap-Drive {
  if (-not $script:drive) { return }
  try {
    $p = Start-Hidden 'net.exe' "use $($script:drive) /delete /y" -Capture
    $null = $p.WaitForExit(10000)
  } catch { }
  Say "unmapped $($script:drive)"
  $script:drive = ''
}

function Start-Tunnel {
  if (Test-Path $TunnelLog) { Remove-Item $TunnelLog -Force -ErrorAction SilentlyContinue }
  if ($LinkRelayKind -eq 'ssh-j') {
    $known = "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$RelayHosts"
  } else {
    $known = ''
  }
  $arguments = "-N -v -E `"$TunnelLog`" -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 " +
               "-o ServerAliveCountMax=3 $known -L 127.0.0.1:$($script:port):$($LinkTargetHost):$LinkTargetPort $LinkJump"
  $script:tunnel = Start-Hidden $Ssh $arguments
}

function Read-TunnelLog {
  # (shared for writing and deleting: ssh has its log open for writing while it runs; ReadAllText would fail on it)
  try {
    $stream = New-Object IO.FileStream($TunnelLog, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try { return (New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)).ReadToEnd() } finally { $stream.Dispose() }
  } catch { return '' }
}

function Tunnel-Error {
  $text = Read-TunnelLog
  $lines = @($text -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^debug' })
  if ($lines.Count -gt 0) { return ($lines | Select-Object -Last 3) -join ' / ' }
  return 'ssh ended (see Tunnel log)'
}

function Begin-Map {
  if (-not $Ssh) { Fail 'ssh.exe not found: install the Windows feature "OpenSSH Client" (Settings > Apps > Optional features)'; return }
  if (-not (Test-Sshfs)) {
    $answer = [Windows.Forms.MessageBox]::Show($form,
      "Mapping needs WinFsp and SSHFS-Win, which are not installed.`r`n`r`nInstall them now with winget? (Windows asks for permission; then press Map drive again.)`r`n`r`nNo: open the download page instead.",
      'slurp drive', 'YesNoCancel', 'Question')
    if ($answer -eq 'Yes') {
      Say 'installing WinFsp and SSHFS-Win with winget ...'
      try {
        Start-Process winget -ArgumentList 'install', '-e', '--id', 'WinFsp.WinFsp', '--accept-source-agreements', '--accept-package-agreements' -Wait
        Start-Process winget -ArgumentList 'install', '-e', '--id', 'SSHFS-Win.SSHFS-Win', '--accept-source-agreements', '--accept-package-agreements' -Wait
      } catch { Say "winget failed: $($_.Exception.Message)" }
      if (Test-Sshfs) { Say 'installed' } else { Say 'still not installed: see https://github.com/winfsp/sshfs-win' }
    } elseif ($answer -eq 'No') {
      Start-Process 'https://github.com/winfsp/sshfs-win#installation'
    }
    return
  }
  if ($letter.SelectedIndex -lt 0) { Fail 'no free drive letter'; return }
  $script:password = $pw.Text.Trim()
  if (-not $script:password) { $state.Text = 'Enter (or paste) the password of the link first.'; [void]$pw.Focus(); return }
  $script:drive = ''
  $script:target = [string]$letter.SelectedItem
  if ($LinkRelayKind -eq 'direct') {
    # (no relay: straight to slurp's machine)
    $script:port = $LinkTargetPort
    $script:sshhost = $LinkTargetHost
    $script:unchost = $LinkTargetHost
    Say "connecting directly to $LinkTargetHost port $LinkTargetPort ..."
    $script:tries = 0
    Begin-Verify
    return
  }
  $script:port = Get-FreePort
  $script:sshhost = '127.0.0.1'
  $script:unchost = 'localhost'
  Say "connecting to the relay $LinkRelayHost (local port $($script:port)) ..."
  Start-Tunnel
  Set-Phase 'tunnel' 'Connecting to the relay ...'
}

function Begin-Verify {
  $script:tries += 1
  if (-not $Keyscan) {
    Say 'ssh-keyscan.exe not found: the host key is not checked'
    Begin-NetUse
    return
  }
  $script:keyscan = Start-Hidden $Keyscan "-T 10 -t ed25519 -p $($script:port) $($script:sshhost)" -Capture
  Set-Phase $(if ($script:phase -eq 'reconnecting') { 'reconnecting' } else { 'verify' }) $(if ($script:phase -eq 'reconnecting') { 'Reconnecting: checking slurp ...' } else { 'Checking that slurp answers (its host key) ...' })
  $script:verifying = $true
}

function Begin-NetUse {
  $unc = "\\sshfs\$LinkUser@$($script:unchost)!$($script:port)"
  Say "mapping $($script:target) to $unc ..."
  $script:netuse = Start-Hidden 'net.exe' "use $($script:target) $unc `"$($script:password)`" /user:$LinkUser /persistent:no" -Capture
  Set-Phase $(if ($script:remap) { 'reconnecting' } else { 'mapping' }) $(if ($script:remap) { 'Reconnecting: mapping again ...' } else { 'Logging in and mapping (password) ...' })
  $script:mapping = $true
}

function On-Tick {
  $age = ((Get-Date) - $script:since).TotalSeconds
  switch ($script:phase) {
    { $_ -eq 'tunnel' -or ($_ -eq 'reconnecting' -and -not $script:verifying -and -not $script:mapping) } {
      if ($script:tunnel.HasExited) {
        $err = Tunnel-Error
        if ($script:phase -eq 'reconnecting' -and $age -lt 120) {
          if (((Get-Date) - $script:lastTry).TotalSeconds -ge 5) {
            Say "tunnel: $err; trying again ..."
            $script:lastTry = Get-Date
            Start-Tunnel
          }
          return
        }
        Fail "the relay connection ended: $err"
        return
      }
      # (the local port listens a moment after the login at the relay: "Local forwarding listening")
      $tlog = Read-TunnelLog
      if ($tlog -match 'forwarding listening' -or ($tlog -match 'Authenticated to' -and $age -gt 3)) {
        Say 'connected to the relay'
        $script:tries = 0
        Begin-Verify
      } elseif ($age -gt 30) {
        Fail 'no answer from the relay within 30 s'
      }
      break
    }
    { $_ -eq 'verify' -or ($_ -eq 'reconnecting' -and $script:verifying) } {
      if (-not $script:keyscan.HasExited) { return }
      $script:verifying = $false
      $out = $script:keyscan.StandardOutput.ReadToEnd()
      $line = @($out -split "`r?`n" | Where-Object { $_ -match 'ssh-ed25519' }) | Select-Object -First 1
      if (-not $line) {
        if ($script:tries -lt 3) { Say 'slurp did not answer yet, trying again ...'; Start-Sleep -Milliseconds 700; Begin-Verify; return }
        Fail 'slurp does not answer through the relay: is slurp running, and the link still valid (not revoked or expired)?'
        return
      }
      $fp = Get-KeyFingerprint $line
      if ($fp -ne $LinkFingerprint) {
        Fail "WRONG HOST KEY: got $fp, slurp shows $LinkFingerprint. Something between you and slurp is not slurp: do not use this link."
        return
      }
      Say "host key checked: $fp"
      Begin-NetUse
      break
    }
    { $_ -eq 'mapping' -or ($_ -eq 'reconnecting' -and $script:mapping) } {
      if (-not $script:netuse.HasExited) {
        if ($age -gt 60) { Fail 'mapping took longer than 60 s' }
        return
      }
      $script:mapping = $false
      $out = ($script:netuse.StandardOutput.ReadToEnd() + ' ' + $script:netuse.StandardError.ReadToEnd()).Trim()
      if ($script:netuse.ExitCode -ne 0) {
        $hint = ''
        if ($out -match '1326|password|Kennwort|mot de passe') { $hint = ' (wrong password? 5 wrong ones lock the link)' }
        elseif ($out -match ' 85|already in use') { $hint = ' (the drive letter is in use: pick another)' }
        elseif ($out -match ' 67|network name') { $hint = ' (wrong password, the link is used up or locked, or SSHFS-Win cannot reach the tunnel)' }
        $script:remap = $false
        Fail "net use: $out$hint"
        $pw.Text = ''
        return
      }
      $script:drive = $script:target
      $script:remap = $false
      Say "mapped $($script:drive)"
      Set-Phase 'mapped' "Mapped as $($script:drive)  (closing this window unmaps it)"
      break
    }
    'mapped' {
      if ($script:tunnel.HasExited) {
        Say "the tunnel ended: $(Tunnel-Error); reconnecting ..."
        Unmap-Drive
        $script:remap = $true
        $script:verifying = $false; $script:mapping = $false
        Start-Tunnel
        Set-Phase 'reconnecting' 'Connection lost: reconnecting ...'
        $script:lastTry = Get-Date
        if ($LinkExpires -gt 0 -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $LinkExpires) {
          Say 'note: the link takes no new logins any more (expired): extend it in slurp'
        }
      }
    }
  }
}

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 400
$timer.Add_Tick({ try { On-Tick } catch { Fail $_.Exception.Message } })

$paste.Add_Click({
  $text = [Windows.Forms.Clipboard]::GetText()
  if ($text) { $pw.Text = $text.Trim(); Say 'password pasted' } else { Say 'the clipboard has no text' }
  [void]$pw.Focus()
})
$show.Add_CheckedChanged({ $pw.UseSystemPasswordChar = -not $show.Checked })
$map.Add_Click({ Begin-Map })
$open.Add_Click({ if ($script:drive) { Start-Process explorer.exe "$($script:drive)\" } })
$logButton.Add_Click({ if (Test-Path $TunnelLog) { Start-Process notepad.exe $TunnelLog } else { Say 'no tunnel log yet' } })
$close.Add_Click({
  if ($script:phase -eq 'input') { $form.Close(); return }
  if ($script:phase -eq 'mapped') { $form.Close(); return }
  Say 'stopped'
  Unmap-Drive
  Stop-All
  Set-Phase 'input' 'Stopped.'
})
$form.Add_FormClosing({
  param($sender, $e)
  if ($script:phase -eq 'mapped' -and -not $script:confirmed) {
    $answer = [Windows.Forms.MessageBox]::Show($form, "Unmap $($script:drive) and close?", 'slurp drive', 'OKCancel', 'Question')
    if ($answer -ne 'OK') { $e.Cancel = $true; return }
    $script:confirmed = $true
  }
  $timer.Stop()
  Unmap-Drive
  Stop-All
})
$form.Add_Shown({
  try { [IO.File]::WriteAllText((Join-Path $SlurpDir 'shown.txt'), (Get-Date).ToString('s')) } catch { }
  $form.WindowState = 'Normal'
  $form.BringToFront()
  $form.Activate()
  $form.TopMost = $false
  [void]$pw.Focus()
  Say "ready: $($letter.Items.Count) free drive letters; slurp's host key $LinkFingerprint"
  if (-not $Ssh) { Say 'ssh.exe not found' }
})

$timer.Start()
[void]$form.ShowDialog()
try { Remove-Item -Recurse -Force $SlurpDir -ErrorAction SilentlyContinue } catch { }

# Removes what the cc-viewer setup script installed on this Windows PC, so Claude on that server can no longer reach it.
# Run in PowerShell opened with "Run as administrator":
#   [Net.ServicePointManager]::SecurityProtocol='Tls12'; irm <cc-viewer address>/pc-setup/uninstall.ps1 | iex
# OpenSSH itself stays installed (other programs may use it).
# The "devices" page of cc-viewer runs this remotely when a PC is deleted there: in a separate process and after a short wait,
# because it stops the very connection it arrives through; it then sets $UserProfileDir and $Delay before this text.
# ASCII only on purpose: Windows consoles may mangle other text.
& {
$ErrorActionPreference = 'Continue'
$prof = $env:USERPROFILE
if ($UserProfileDir) { $prof = $UserProfileDir }
if ($Delay) { Start-Sleep -Seconds $Delay }
$Utf8 = New-Object Text.UTF8Encoding $false

Get-ScheduledTask | Where-Object { $_.TaskName -like 'ccviewer-tunnel*' } | ForEach-Object {
    Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*ccviewer*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# the keys of Claude on the server (their comment is cc_viewer-gw-<id>)
foreach ($f in "$env:ProgramData\ssh\administrators_authorized_keys", "$prof\.ssh\authorized_keys") {
    if (Test-Path -LiteralPath $f) {
        $lines = @(Get-Content -LiteralPath $f)
        $keep = @($lines | Where-Object { $_.Trim() -and $_ -notmatch ' cc_viewer-gw-[0-9a-z]+\s*$' })
        if ($keep.Count -ne @($lines | Where-Object { $_.Trim() }).Count) {
            [IO.File]::WriteAllText($f, (($keep -join "`n") + "`n"), $Utf8)
        }
    }
}
# ~\.ssh\config: our block (any version; an older one could glue its start marker onto the user's last line)
$sc = "$prof\.ssh\config"
if (Test-Path -LiteralPath $sc) {
    $orig = [IO.File]::ReadAllText($sc)
    $rest = [regex]::Replace($orig, '(?ms)^([^\r\n]*?)# >>> ccviewer[ \t]*\r?\n.*?^# <<< ccviewer[ \t]*(\r?\n|\z)',
        { param($m) if ($m.Groups[1].Value.Trim()) { $m.Groups[1].Value + "`n" } else { '' } })
    if ($rest -ne $orig) { [IO.File]::WriteAllText($sc, $rest, $Utf8) }
}
foreach ($p in @("$env:ProgramData\ccviewer") + @(Get-ChildItem $env:ProgramData -Filter 'ccviewer-tunnel-*' -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })) {
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }
}
Get-ChildItem "$prof\.ssh" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'ccviewer_tunnel_*' -or $_.Name -in 'ccviewer_relay', 'ccviewer_known_hosts' } | Remove-Item -Force -ErrorAction SilentlyContinue
if ($PSCommandPath -and $PSCommandPath -like "$env:TEMP*") { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue }
Write-Host 'Removed: this PC is no longer connected to cc-viewer.'
}

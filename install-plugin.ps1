# Install / uninstall the USTC token plugin into a DSH profile.
#
#   powershell -ExecutionPolicy Bypass -File install-plugin.ps1
#   powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Uninstall
#
# The profile entry must arrive through an `insert:` patch: a bare `- id: <new>`
# is treated as an override target and silently dropped when that id is unknown.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 decodes .ps1 as ANSI
# when there is no BOM, so non-ASCII text here breaks the parser.
param(
    [string]$Profile = 'desktop',
    [string]$ProfileRoot = (Join-Path $env:USERPROFILE '.dsh\profiles'),
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'plugin'
$profileDir = Join-Path $ProfileRoot $Profile
$dst = Join-Path $profileDir 'node_modules\dsh-plugin-ustc-token'
$patch = Join-Path $profileDir 'cordis.patch.yml'
$marker = 'ustc-token-badge|dsh-plugin-ustc-token'

if (-not (Test-Path -LiteralPath $profileDir)) {
    throw "DSH profile not found: $profileDir  (start DSH once so it creates the profile, or pass -Profile <name>)"
}

# Split the patch into top-level blocks (each starts at a `- ` line) so we can
# drop only our own entry and leave every other block byte-identical.
function Read-Blocks {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $blocks = New-Object System.Collections.Generic.List[object]
    $cur = New-Object System.Collections.Generic.List[string]
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match '^- ') {
            if ($cur.Count -gt 0) { $blocks.Add($cur.ToArray()) }
            $cur = New-Object System.Collections.Generic.List[string]
            $cur.Add($line)
        } elseif ($cur.Count -gt 0) {
            $cur.Add($line)
        } elseif ($line.Trim() -ne '') {
            $cur.Add($line)
        }
    }
    if ($cur.Count -gt 0) { $blocks.Add($cur.ToArray()) }
    return $blocks
}

function Write-Blocks {
    param([object[]]$Blocks, [string]$Path, [switch]$Append)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($b in $Blocks) { foreach ($l in $b) { $out.Add($l) } }
    if ($Append) {
        while ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -eq '') { $out.RemoveAt($out.Count - 1) }
        $out.Add('')
        $out.Add('- insert:')
        $out.Add('    - id: ustc-token-badge')
        $out.Add('      name: dsh-plugin-ustc-token')
    }
    Set-Content -LiteralPath $Path -Value $out -Encoding utf8
}

if ($Uninstall) {
    Remove-Item -LiteralPath $dst -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $patch) {
        $keep = @(Read-Blocks -Path $patch | Where-Object { ($_ -join "`n") -notmatch $marker })
        Write-Blocks -Blocks $keep -Path $patch
    }
    Write-Host "[ok] removed plugin dir and the ustc-token entry from cordis.patch.yml"
    Write-Host "[ok] refresh the DSH window (Ctrl+R) and the badge disappears"
    exit 0
}

foreach ($f in 'package.json', 'lib\index.js', 'lib\client.js') {
    $p = Join-Path $src $f
    if (-not (Test-Path -LiteralPath $p)) { throw "incomplete repo, missing $p" }
}

New-Item -ItemType Directory -Force -Path (Join-Path $dst 'lib') | Out-Null
Copy-Item (Join-Path $src 'package.json') $dst -Force
Copy-Item (Join-Path $src 'lib\index.js') (Join-Path $dst 'lib') -Force
Copy-Item (Join-Path $src 'lib\client.js') (Join-Path $dst 'lib') -Force
Write-Host "[ok] copied to $dst"

$keep = @(Read-Blocks -Path $patch | Where-Object { ($_ -join "`n") -notmatch $marker })
Write-Blocks -Blocks $keep -Path $patch -Append
Write-Host "[ok] profile entry ensured as an insert patch: $patch"
Write-Host "[ok] the profile patch hot-loads; refresh the window (Ctrl+R) to see the badge"

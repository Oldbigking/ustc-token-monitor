# Install the USTC token plugin into the DSH desktop profile.
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'plugin'
$dst = Join-Path $env:USERPROFILE '.dsh\profiles\desktop\node_modules\dsh-plugin-ustc-token'

New-Item -ItemType Directory -Force -Path (Join-Path $dst 'lib') | Out-Null
Copy-Item (Join-Path $src 'package.json') $dst -Force
Copy-Item (Join-Path $src 'lib\index.js') (Join-Path $dst 'lib') -Force
Copy-Item (Join-Path $src 'lib\client.js') (Join-Path $dst 'lib') -Force
Write-Host "[ok] copied to $dst"

# A new entry must arrive through an `insert:` patch. A bare `- id: <new>` is
# treated as an override target and silently dropped when that id is unknown.
$patch = Join-Path $env:USERPROFILE '.dsh\profiles\desktop\cordis.patch.yml'
$lines = Get-Content $patch
$body = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^- id: ustc-token-') { $i++; continue }
    if ($lines[$i] -match '^\s+name: dsh-plugin-ustc-token') { continue }
    if ($lines[$i] -match '^- insert:') { $i += 3; continue }
    if ($lines[$i] -match '^# touch ') { continue }
    $body.Add($lines[$i])
}
while ($body.Count -gt 0 -and $body[$body.Count - 1].Trim() -eq '') { $body.RemoveAt($body.Count - 1) }
$body.Add('')
$body.Add('- insert:')
$body.Add('    - id: ustc-token-badge')
$body.Add('      name: dsh-plugin-ustc-token')
Set-Content -Path $patch -Value $body -Encoding utf8
Write-Host "[ok] profile entry ensured as an insert patch: $patch"
Write-Host "[ok] restart DSH to load the plugin"

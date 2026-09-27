param(
    [string]$RepositoryPath = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$Host.UI.RawUI.WindowTitle = "NachoBot Desktop Pet Logs"

$sources = @(
    [pscustomobject]@{
        Name = "CORE"
        Color = "Cyan"
        Path = Join-Path $RepositoryPath "NachoBot\logs\desktop-pet-core.err.log"
    },
    [pscustomobject]@{
        Name = "VOX"
        Color = "Magenta"
        Path = Join-Path $RepositoryPath "NachoBot-Local-Host-Adapter\runtime\logs\vox-server.err.log"
    },
    [pscustomobject]@{
        Name = "TTS"
        Color = "DarkMagenta"
        Path = Join-Path $RepositoryPath "NachoBot-Local-Host-Adapter\runtime\logs\multimodal.err.log"
    },
    [pscustomobject]@{
        Name = "CHAT"
        Color = "Green"
        Path = Join-Path $RepositoryPath "NachoBot-Local-Host-Adapter\runtime\desktop-pet-local-host.err.log"
    },
    [pscustomobject]@{
        Name = "LIVE2D"
        Color = "Yellow"
        Path = Join-Path $RepositoryPath "NachoBot-Live2D-Adapter\logs\live2d.log"
    }
)

$positions = @{}
Clear-Host
Write-Host "NachoBot Desktop Pet - Live Logs" -ForegroundColor White
Write-Host "Closing this window does not stop the desktop pet." -ForegroundColor DarkGray
Write-Host "Sources: CORE / VOX / TTS / CHAT / LIVE2D" -ForegroundColor DarkGray
Write-Host ""

foreach ($source in $sources) {
    if (Test-Path -LiteralPath $source.Path -PathType Leaf) {
        Write-Host "[$($source.Name)] recent log" -ForegroundColor $source.Color
        Get-Content -LiteralPath $source.Path -Tail 8 -Encoding utf8 |
            ForEach-Object { Write-Host "[$($source.Name)] $_" -ForegroundColor $source.Color }
        $positions[$source.Path] = (Get-Item -LiteralPath $source.Path).Length
    }
    else {
        $positions[$source.Path] = 0L
    }
}

while ($true) {
    foreach ($source in $sources) {
            if (-not (Test-Path -LiteralPath $source.Path -PathType Leaf)) {
                continue
            }
            $length = (Get-Item -LiteralPath $source.Path).Length
            $position = [long]$positions[$source.Path]
            if ($length -lt $position) {
                $position = 0L
            }
            if ($length -eq $position) {
                continue
            }

            $stream = $null
            $reader = $null
            try {
                $stream = [System.IO.File]::Open(
                    $source.Path,
                    [System.IO.FileMode]::Open,
                    [System.IO.FileAccess]::Read,
                    [System.IO.FileShare]::ReadWrite
                )
                [void]$stream.Seek($position, [System.IO.SeekOrigin]::Begin)
                $reader = [System.IO.StreamReader]::new(
                    $stream,
                    [System.Text.UTF8Encoding]::new($false),
                    $true
                )
                $newText = $reader.ReadToEnd()
                $positions[$source.Path] = $stream.Position
                foreach ($line in ($newText -split "`r?`n")) {
                    if ($line) {
                        Write-Host "[$($source.Name)] $line" -ForegroundColor $source.Color
                    }
                }
            }
            catch {
                Write-Host "[$($source.Name)] log is temporarily unavailable." -ForegroundColor DarkYellow
            }
            finally {
                if ($null -ne $reader) {
                    $reader.Dispose()
                }
                elseif ($null -ne $stream) {
                    $stream.Dispose()
                }
            }
    }
    Start-Sleep -Milliseconds 300
}

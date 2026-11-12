# powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wynxo/Kova/main/uninstall.ps1 | iex"
function Uninstall-Kova {
    param([string[]]$UninstallArgs)
    $ErrorActionPreference = 'Stop'
    $Purge = $false
    $Keep = $false
    foreach ($Arg in $UninstallArgs) {
        switch ($Arg) {
            { $_ -in '--purge', '-Purge' } { $Purge = $true }
            { $_ -in '--keep-data', '-KeepData' } { $Keep = $true }
            { $_ -in '--help', '-h', '-Help' } {
                Write-Host 'Usage: .\uninstall.ps1 [--purge | --keep-data]'
                return
            }
            default { throw "Unknown option: $Arg" }
        }
    }
    if ($Purge -and $Keep) { throw 'Choose --purge or --keep-data, not both.' }
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    if ($Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or $Identity.IsSystem) {
        throw 'Run as your normal user, not in an Administrator terminal.'
    }
    if (-not $env:USERPROFILE) { throw 'USERPROFILE must be your user home directory.' }
    $Bin = Join-Path $env:USERPROFILE '.local\bin'
    $env:UV_TOOL_BIN_DIR = $Bin
    $env:UV_TOOL_DIR = Join-Path $env:USERPROFILE '.local\share\uv\tools'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $env:USERPROFILE '.local\share\uv\python'
    $env:UV_CACHE_DIR = Join-Path $env:USERPROFILE '.cache\uv'
    $Data = if ($env:KOVA_HOME) { $env:KOVA_HOME } else { Join-Path $env:USERPROFILE '.kova' }
    $Data = [IO.Path]::GetFullPath($Data)
    function Assert-SafeData {
        $Root = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'
        if (-not $Data.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) { throw "Refusing to delete $Data" }
        # Reject junctions/symlinks, including ones in a custom data path's parents.
        $Item = Get-Item -LiteralPath $Data -Force -ErrorAction SilentlyContinue
        while ($Item -and $Item.FullName.TrimEnd('\') -ne $Root.TrimEnd('\')) {
            if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to delete linked path $Data" }
            $Item = $Item.Parent
        }
    }
    if ($Purge) { Assert-SafeData }
    Write-Host 'Stopping the Kova server (if running) ...'
    $PidFile = Join-Path $Data 'server.pid'
    if (Test-Path -LiteralPath $PidFile) {
        $ServerPid = 0
        if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$ServerPid) -or $ServerPid -le 1) {
            throw 'Invalid server PID; cannot safely stop Kova.'
        }
        $Server = Get-Process -Id $ServerPid -ErrorAction SilentlyContinue
        if ($Server) { Stop-Process -Id $ServerPid -ErrorAction Stop }
    }
    $Uv = Join-Path $Bin 'uv.exe'
    if (-not (Test-Path -LiteralPath $Uv)) {
        $Found = Get-Command uv -ErrorAction SilentlyContinue
        $Uv = if ($Found) { $Found.Source } else { $null }
    }
    if ($Uv) {
        Write-Host 'Removing the kova command: uv tool uninstall kova-ai'
        & $Uv tool uninstall kova-ai
        if ($LASTEXITCODE -ne 0) { throw 'uv uninstall failed; your data was kept.' }
    } else {
        Write-Host 'uv was not found; no uv tool can be removed.'
    }
    if (Test-Path -LiteralPath $Data) {
        if (-not $Purge -and -not $Keep -and -not [Console]::IsInputRedirected) {
            $Answer = Read-Host "Delete your Kova data in $Data (chats, memory, model settings, token)? [y/N]"
            $Purge = $Answer -match '^(y|yes)$'
        }
        if ($Purge) {
            Assert-SafeData
            Remove-Item -LiteralPath $Data -Recurse -Force
            Write-Host "Deleted $Data."
        } else {
            Write-Host "Keeping your data in $Data. Use --purge to delete it."
        }
    }
    Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Kova' -ErrorAction SilentlyContinue
    $SavedScript = Join-Path $env:USERPROFILE '.local\share\kova\uninstall.ps1'
    Remove-Item -LiteralPath $SavedScript -ErrorAction SilentlyContinue
    Write-Host 'External memory folders and files are left alone.'
    Write-Host 'Done. Kova is removed.'
}
Uninstall-Kova -UninstallArgs $args

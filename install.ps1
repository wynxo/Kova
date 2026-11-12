# powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wynxo/Kova/main/install.ps1 | iex"
# Define the entire installer before executing anything, including when piped to iex.
function Install-Kova {
    param([string[]]$InstallArgs)
    $ErrorActionPreference = 'Stop'
    $Ref = if ($env:KOVA_REF) { $env:KOVA_REF } else { 'main' }
    $Dev = $false
    for ($i = 0; $i -lt $InstallArgs.Count; $i++) {
        switch ($InstallArgs[$i]) {
            { $_ -in '--ref', '-Ref' } {
                $i++
                if ($i -ge $InstallArgs.Count) { throw '--ref needs a value.' }
                $Ref = $InstallArgs[$i]
            }
            { $_ -in '--dev', '-Dev' } { $Dev = $true }
            { $_ -in '--help', '-h', '-Help' } {
                Write-Host 'Usage: .\install.ps1 [--ref REF] [--dev]'
                return
            }
            default { throw "Unknown option: $($InstallArgs[$i])" }
        }
    }
    if ($Ref -notmatch '^[A-Za-z0-9_][A-Za-z0-9_./-]*$') { throw 'Invalid branch, tag, or commit SHA.' }
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
    $Uv = Join-Path $Bin 'uv.exe'
    if (-not (Test-Path -LiteralPath $Uv)) {
        $Found = Get-Command uv -ErrorAction SilentlyContinue
        if ($Found) { $Uv = $Found.Source } else {
            Write-Host 'Installing uv from https://astral.sh/uv/install.ps1 ...'
            $PreviousInstall = $env:UV_UNMANAGED_INSTALL
            try {
                $env:UV_UNMANAGED_INSTALL = $Bin
                Invoke-Expression (Invoke-RestMethod 'https://astral.sh/uv/install.ps1')
            } finally {
                $env:UV_UNMANAGED_INSTALL = $PreviousInstall
            }
            if (-not (Test-Path -LiteralPath $Uv)) { throw 'uv was not found after installation.' }
        }
    }
    Write-Host "Using uv at $Uv."
    if ($env:KOVA_SOURCE) {
        $Source = $env:KOVA_SOURCE
    } elseif ($Dev) {
        $Source = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    } elseif (Get-Command git -ErrorAction SilentlyContinue) {
        $Source = "kova-ai @ git+https://github.com/wynxo/Kova@$Ref"
    } else {
        Write-Host 'git was not found; using the GitHub source archive.'
        $Source = "kova-ai @ https://github.com/wynxo/Kova/archive/$Ref.tar.gz"
    }
    # Future PyPI switch: replace Source above with 'kova-ai==VERSION'.
    Write-Host "Installing/updating Kova: $Source"
    $UvArgs = @('tool', 'install', '--force', '--refresh', '--python', '3.12')
    if ($Dev) {
        if (-not (Test-Path -LiteralPath (Join-Path $Source 'pyproject.toml'))) {
            throw '--dev needs a checkout (or KOVA_SOURCE).'
        }
        $UvArgs += '--editable'
    }
    & $Uv @UvArgs $Source
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed ($LASTEXITCODE)." }

    # Persist only a user PATH entry; never modify system PATH or shell profiles.
    $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($UserPath -split ';').TrimEnd('\') -notcontains $Bin.TrimEnd('\')) {
        Write-Host "Adding $Bin to your user PATH."
        [Environment]::SetEnvironmentVariable('Path', "$Bin;$UserPath", 'User')
    }
    $env:Path = "$Bin;$env:Path"

    $Support = Join-Path $env:USERPROFILE '.local\share\kova'
    New-Item -ItemType Directory -Path $Support -Force | Out-Null
    $Uninstaller = Join-Path $Support 'uninstall.ps1'
    $LocalUninstaller = if ($env:KOVA_SOURCE) { Join-Path $env:KOVA_SOURCE 'uninstall.ps1' } elseif ($Dev) {
        Join-Path $Source 'uninstall.ps1'
    } else { $null }
    if ($LocalUninstaller -and (Test-Path -LiteralPath $LocalUninstaller)) {
        Copy-Item -LiteralPath $LocalUninstaller -Destination $Uninstaller -Force
    } else {
        Write-Host 'Saving the standalone uninstaller for Windows Settings ...'
        Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/wynxo/Kova/$Ref/uninstall.ps1" -OutFile $Uninstaller
    }
    $Kova = Join-Path $Bin 'kova.exe'
    $Version = ((& $Kova --version) -join '').Replace('Kova ', '')
    if ($LASTEXITCODE -ne 0) { throw 'Installed kova could not run.' }
    $Key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Kova'
    New-Item -Path $Key -Force | Out-Null
    $UninstallCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Uninstaller`" --keep-data"
    $Values = @{
        DisplayName = 'Kova'; DisplayVersion = $Version; Publisher = 'wynxo'
        InstallLocation = (Join-Path $env:UV_TOOL_DIR 'kova-ai')
        UninstallString = $UninstallCommand; QuietUninstallString = $UninstallCommand
        URLInfoAbout = 'https://github.com/wynxo/Kova'
    }
    foreach ($Name in $Values.Keys) { New-ItemProperty -Path $Key -Name $Name -Value $Values[$Name] -PropertyType String -Force | Out-Null }
    foreach ($Name in 'NoModify', 'NoRepair') { New-ItemProperty -Path $Key -Name $Name -Value 1 -PropertyType DWord -Force | Out-Null }
    Write-Host 'Registered Kova in Settings > Apps (current user only).'
    Write-Host 'Done. Run: kova setup'
    Write-Host 'Then: kova chat'
}
Install-Kova -InstallArgs $args

# powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wynxo/Kova/main/install.ps1 | iex"
# Define the entire installer before executing anything, including when piped to iex.
function Install-Kova {
    param([string[]]$InstallArgs)
    $ErrorActionPreference = 'Stop'
    $Ref = if ($env:KOVA_REF) { $env:KOVA_REF } else { 'main' }
    $Dev = $false
    $RegisterDesktop = $true
    for ($i = 0; $i -lt $InstallArgs.Count; $i++) {
        switch ($InstallArgs[$i]) {
            { $_ -in '--ref', '-Ref' } {
                $i++
                if ($i -ge $InstallArgs.Count) { throw '--ref needs a value.' }
                $Ref = $InstallArgs[$i]
            }
            { $_ -in '--dev', '-Dev' } { $Dev = $true }
            { $_ -in '--no-desktop', '-NoDesktop' } { $RegisterDesktop = $false }
            { $_ -in '--help', '-h', '-Help' } {
                Write-Host 'Usage: .\install.ps1 [--ref REF] [--dev] [--no-desktop]'
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
    $UvArgs = @('tool', 'install', '--force', '--refresh', '--upgrade-package', 'kova-ai', '--python', '3.12')
    if ($Dev) {
        if (-not (Test-Path -LiteralPath (Join-Path $Source 'pyproject.toml'))) {
            throw '--dev needs a checkout (or KOVA_SOURCE).'
        }
        $UvArgs += '--editable'
    }
    & $Uv @UvArgs $Source
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed ($LASTEXITCODE)." }

    # The desktop app is optional: if it cannot be prepared, finish the CLI install anyway.
    if ($RegisterDesktop) { try {
        if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
            throw 'The desktop app needs Node.js 22+ (including npm). Install it from https://nodejs.org and rerun this installer to add Kova to the Start menu.'
        }
        & node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'
        if ($LASTEXITCODE -ne 0) { throw 'Install Node.js 22+ from https://nodejs.org and rerun this installer.' }
        # Keep the desktop source and prepare Electron before creating the shortcut.
        $Data = if ($env:KOVA_HOME) { $env:KOVA_HOME } else { Join-Path $env:USERPROFILE '.kova' }
        $DesktopDest = Join-Path $Data 'desktop'
        $App = Join-Path $Bin 'kova.exe'
        $ToolPython = Join-Path $env:UV_TOOL_DIR 'kova-ai\Scripts\python.exe'
        $PackagedDesktop = $false
        if ((Test-Path -LiteralPath $App) -and (Test-Path -LiteralPath $ToolPython)) {
            $PackagedDesktop = [bool](& $ToolPython -c 'from kova import app; print(int(hasattr(app, ''refresh_desktop'')))' | Where-Object { $_ -eq '1' })
        }
        if ($PackagedDesktop) {
            & $App app --prepare
            if ($LASTEXITCODE -ne 0) { throw 'Preparing the desktop app failed. Run kova app to retry.' }
            $DesktopSource = $DesktopDest
        } else {
            # Compatibility with refs that predate packaged desktop assets.
            $DesktopSource = $null
            if ($env:KOVA_SOURCE -and (Test-Path -LiteralPath (Join-Path $env:KOVA_SOURCE 'src\kova\desktop\package.json'))) {
                $DesktopSource = Join-Path $env:KOVA_SOURCE 'src\kova\desktop'
            } elseif ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'src\kova\desktop\package.json'))) {
                $DesktopSource = Join-Path $PSScriptRoot 'src\kova\desktop'
            } elseif ($Dev -and (Test-Path -LiteralPath (Join-Path $Source 'src\kova\desktop\package.json'))) {
                $DesktopSource = Join-Path $Source 'src\kova\desktop'
            } else {
                $Zip = Join-Path ([IO.Path]::GetTempPath()) ('kova-' + [guid]::NewGuid().ToString('N') + '.zip')
                $Tree = Join-Path ([IO.Path]::GetTempPath()) ('kova-' + [guid]::NewGuid().ToString('N'))
                Write-Host "Downloading the desktop app from https://github.com/wynxo/Kova/archive/$Ref.zip ..."
                try {
                    Invoke-WebRequest -UseBasicParsing "https://github.com/wynxo/Kova/archive/$Ref.zip" -OutFile $Zip
                    Expand-Archive -LiteralPath $Zip -DestinationPath $Tree -Force
                    $DesktopSource = Get-ChildItem -LiteralPath $Tree -Directory |
                        ForEach-Object { Join-Path $_.FullName 'desktop' } |
                        Where-Object { Test-Path -LiteralPath (Join-Path $_ 'package.json') } |
                        Select-Object -First 1
                } catch {
                    Write-Host "Note: the desktop app files could not be downloaded ($($_.Exception.Message))."
                } finally {
                    Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
                }
            }
        }
        if ($DesktopSource) {
            if (-not $PackagedDesktop) {
                New-Item -ItemType Directory -Path $DesktopDest -Force | Out-Null
                Get-ChildItem -LiteralPath $DesktopSource -Force |
                    Where-Object { $_.Name -notin 'node_modules', 'dist' } |
                    ForEach-Object {
                        $Target = Join-Path $DesktopDest $_.Name
                        Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction SilentlyContinue
                        Copy-Item -LiteralPath $_.FullName -Destination $Target -Recurse -Force
                    }
                Push-Location -LiteralPath $DesktopDest
                try {
                    Write-Host 'Preparing Electron (this may take a few minutes) ...'
                    & npm ci --include=dev
                    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed. Check your connection to the npm registry and rerun this installer.' }
                } finally {
                    Pop-Location
                    if ($Tree) { Remove-Item -LiteralPath $Tree -Recurse -Force -ErrorAction SilentlyContinue }
                }
            }
            $Electron = Join-Path $DesktopDest 'node_modules\electron\dist\electron.exe'
            if (-not (Test-Path -LiteralPath $Electron)) { throw 'Electron is missing after npm ci. Rerun with Electron downloads enabled.' }
            # Direct Electron target: no console window and no Node/npm needed on click.
            $Programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
            New-Item -ItemType Directory -Path $Programs -Force | Out-Null
            $Shell = New-Object -ComObject WScript.Shell
            $Shortcut = $Shell.CreateShortcut((Join-Path $Programs 'Kova.lnk'))
            $Shortcut.TargetPath = $Electron
            $Shortcut.Arguments = "`"$DesktopDest`" `"--kova-home=$Data`""
            $Shortcut.WorkingDirectory = $DesktopDest
            $Shortcut.Description = 'Private local AI assistant'
            $Shortcut.Save()
            Write-Host 'Registered Kova in your Start menu.'
            Write-Host "Desktop app files: $DesktopDest"
        } else {
            if ($Tree) { Remove-Item -LiteralPath $Tree -Recurse -Force -ErrorAction SilentlyContinue }
            throw 'Desktop files could not be installed. Check your connection and rerun this installer to add it.'
        }
    } catch {
        Write-Host "Note: $($_.Exception.Message) Kova itself is installed; open it in your browser with kova web."
        $RegisterDesktop = $false
    } }

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
    $LocalUninstaller = if ($env:KOVA_SOURCE) { Join-Path $env:KOVA_SOURCE 'scripts\uninstall.ps1' } elseif ($Dev) {
        Join-Path $Source 'scripts\uninstall.ps1'
    } else { $null }
    if ($LocalUninstaller -and (Test-Path -LiteralPath $LocalUninstaller)) {
        Copy-Item -LiteralPath $LocalUninstaller -Destination $Uninstaller -Force
    } else {
        Write-Host 'Saving the standalone uninstaller for Windows Settings ...'
        try {
            Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/wynxo/Kova/$Ref/scripts/uninstall.ps1" -OutFile $Uninstaller
        } catch {
            Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/wynxo/Kova/$Ref/uninstall.ps1" -OutFile $Uninstaller
        }
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
    if ($RegisterDesktop) { Write-Host 'Done. Open Kova from the Start menu and choose Add provider.' }
    else { Write-Host 'Done (without the desktop app). Run: kova setup' }
    Write-Host 'Terminal chat: kova setup, then kova'
    Write-Host 'Desktop app: kova app    Web UI in your browser: kova web'
}
Install-Kova -InstallArgs $args

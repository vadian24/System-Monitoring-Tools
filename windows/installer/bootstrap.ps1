$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = 'Windows System Monitor Setup'
$runtimeVersion = '3.14.10'
$pythonUrl = 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.exe'
$pythonSha256 = '92480239264744fe0386a1bd8275786fefa126e3129e56a68e22b52c73329965'
$stage = $null
$ownsMutex = $false
$mutex = $null

function Test-MonitorPython([string]$candidate) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf) -or
        $candidate -match '\\Microsoft\\WindowsApps\\') { return $false }
    $probe = "import ensurepip,platform,struct,sys,venv; sys.exit(0 if platform.python_implementation() == 'CPython' and (3,12) <= sys.version_info[:2] <= (3,14) and struct.calcsize('P') == 8 else 1)"
    try {
        & $candidate -I -c $probe 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-MonitorPython {
    $candidates = @((Join-Path $runtime 'python.exe'))
    foreach ($branch in @('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore')) {
        foreach ($key in @(Get-ChildItem -LiteralPath $branch -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            $entry = Get-Item -LiteralPath ($key.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
            if (-not $entry) { continue }
            $explicit = $entry.GetValue('ExecutablePath')
            $folder = $entry.GetValue('')
            if ($explicit) { $candidates += $explicit }
            if ($folder) { $candidates += (Join-Path $folder 'python.exe') }
        }
    }
    foreach ($command in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $candidates += $command.Source
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-MonitorPython $candidate) { return $candidate }
    }
    return $null
}

$exitCode = 0
try {
    $mutex = [System.Threading.Mutex]::new($false, 'Local\SystemMonitorWindowsSetup')
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Another System Monitor setup is already running.' }
    if (-not $env:LOCALAPPDATA) { throw 'Windows did not provide a local application folder for this account.' }
    $root = Join-Path $env:LOCALAPPDATA 'SystemMonitorWindows'
    $runtime = Join-Path $root ('Runtime-' + $runtimeVersion)
    Write-Host 'Windows System Monitor Setup' -ForegroundColor Cyan
    Write-Host 'This installs the dashboard for your Windows account.'
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('SystemMonitorSetup-' + [guid]::NewGuid().ToString('N'))
    New-Item -Path $stage -ItemType Directory | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($env:SYSTEM_MONITOR_SETUP_PAYLOAD, $stage)

    $python = Find-MonitorPython
    if (-not $python) {
        Write-Host "Downloading Python $runtimeVersion from python.org..."
        $download = Join-Path $stage 'python-amd64.exe'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $download -TimeoutSec 120
        $actual = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
        if ($actual -ne $pythonSha256) { throw 'The Python download failed the SHA-256 check. Setup stopped.' }
        Write-Host "Installing Python $runtimeVersion for this account..."
        $arguments = @('/quiet', '/norestart', 'InstallAllUsers=0', 'Include_launcher=0',
                     'Include_pip=1', 'Include_test=0', 'Include_doc=0', 'Include_tcltk=0',
                     'Include_tools=0', 'AssociateFiles=0', 'Shortcuts=0',
                     'PrependPath=0', ('TargetDir="' + $runtime + '"'))
        $process = Start-Process -FilePath $download -ArgumentList $arguments -PassThru -Wait
        if ($process.ExitCode -eq 3010) {
            throw 'Python setup requested a restart. Restart Windows and run this setup again.'
        }
        $python = Join-Path $runtime 'python.exe'
        if ($process.ExitCode -ne 0 -or -not (Test-MonitorPython $python)) {
            throw "Python setup did not complete successfully (exit code $($process.ExitCode))."
        }
    } else {
        Write-Host "Using compatible Python at $python"
    }

    Write-Host 'Installing the dashboard and Start menu shortcut...'
    $installer = Join-Path $stage 'System-Monitor-Windows\install_windows.py'
    & $python -I $installer
    if ($LASTEXITCODE -ne 0) { throw "Dashboard installation failed (exit code $LASTEXITCODE)." }
    Write-Host 'Setup complete. Open Windows System Monitor from Start.' -ForegroundColor Cyan
} catch {
    $exitCode = 1
    Write-Host ''
    Write-Host ('Setup could not finish: ' + $_.Exception.Message) -ForegroundColor Red
} finally {
    if ($stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
if ($exitCode) {
    [void](Read-Host 'Press Enter to close setup')
}
exit $exitCode

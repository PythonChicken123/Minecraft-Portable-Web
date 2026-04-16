param(
    [string]$Username,
    [string]$ServerIp,
    [string]$JvmOpts
)

$ErrorActionPreference = "Stop"

# Set compatibility layer to avoid UAC prompts
$env:__COMPAT_LAYER = "RUNASINVOKER"

# Fallback to positional arguments if needed
if (-not $JvmOpts -and $args.Count -gt 0) {
    $JvmOpts = $args[0]
}

$verbose = $env:LAUNCHER_VERBOSE -in @("true", "1", "yes", "on")
if ($verbose) {
    Write-Host "DEBUG: Username = '$Username'"
    Write-Host "DEBUG: ServerIp = '$ServerIp'"
    Write-Host "DEBUG: JvmOpts = '$JvmOpts'"
}
if (-not $JvmOpts) {
    Write-Host "ERROR: JvmOpts is empty. Cannot proceed."
    exit 1
}

# Determine script location and base directories
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir
$baseDir = Join-Path $env:LOCALAPPDATA "PortableMC"
$binDir = Join-Path $baseDir "portablemc_bin"
$exePath = Join-Path $binDir "portablemc.exe"

# --- Ensure the base directory exists ---
if (-not (Test-Path $baseDir)) {
    New-Item -ItemType Directory -Path $baseDir -Force | Out-Null
}

# --- Download portablemc.exe if missing (with optional SSL fallback) ---
if (-not (Test-Path $exePath)) {
    Write-Host "portablemc.exe not found. Downloading to $binDir..."
    $pmcVersion = if ($env:PORTABLEMC_VERSION) { $env:PORTABLEMC_VERSION } else { "5.0.2" }
    $releaseBase = if ($env:PORTABLEMC_RELEASE_BASE) { $env:PORTABLEMC_RELEASE_BASE } else { "https://github.com/mindstorm38/portablemc/releases/download/v$pmcVersion" }
    $url = "$releaseBase/portablemc-$pmcVersion-windows-x86_64-msvc.zip"
    $zipPath = Join-Path $baseDir "portablemc.zip"

    # Check if insecure SSL is allowed
    $allowInsecure = $env:ALLOW_INSECURE_SSL -in @("true", "1", "yes", "on")

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Host "First download attempt failed: $_"
        if ($allowInsecure) {
            Write-Host "Retrying with SSL verification disabled..."
            try {
                [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}
                Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
            } catch {
                Write-Host "Download failed even with SSL disabled. Exiting."
                exit 1
            }
        } else {
            Write-Host "SSL verification failed and insecure SSL is disabled. Exiting."
            exit 1
        }
    }

    Write-Host "Extracting..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $binDir)
    Remove-Item $zipPath

    # Flatten subdirectories
    Get-ChildItem $binDir -Directory | ForEach-Object {
        Get-ChildItem $_.FullName -File | Move-Item -Destination $binDir -Force
        Remove-Item $_.FullName -Recurse
    }
}

# --- Process JVM options and launch ---
$portablemcArgs = @("--main-dir", ".", "start", "--join-server", $ServerIp)
$jvmArgList = @()
if ($JvmOpts) {
    $jvmArgList = ($JvmOpts -split '\s+') | Where-Object { $_ -and $_.Trim() -ne "" }
}
foreach ($jvmArg in $jvmArgList) {
    # Use --jvm-arg=<value> so values like -Xmx3G are not parsed as options.
    $portablemcArgs += "--jvm-arg=$jvmArg"
}
$portablemcArgs += @("fabric:", "-u", $Username)

Write-Host "Working directory: $baseDir"
$env:__COMPAT_LAYER = "RUNASINVOKER"
Push-Location $baseDir
try {
    # Try native portablemc.exe first; if blocked or returns non-zero, fall back to Python module.
    if (Test-Path $exePath) {
        try {
            Write-Host "Launching native binary: $exePath $($portablemcArgs -join ' ')"
            & $exePath @portablemcArgs
            $nativeExit = $LASTEXITCODE
            Write-Host "Native exit code: $nativeExit"
            if ($nativeExit -eq 0) {
                exit 0
            }
            Write-Host "Native launcher returned non-zero exit code ($nativeExit). Trying Python fallback..."
        } catch {
            Write-Host "Native portablemc.exe launch failed (likely policy block): $($_.Exception.Message)"
            Write-Host "Trying Python module fallback..."
        }
    }

    $pythonLaunchers = @(
        @{ File = "py"; Args = @("-3", "-m", "portablemc") },
        @{ File = "python"; Args = @("-m", "portablemc") },
        @{ File = "python3"; Args = @("-m", "portablemc") }
    )

    foreach ($launcher in $pythonLaunchers) {
        try {
            $allArgs = @($launcher.Args + $portablemcArgs)
            Write-Host "Launching Python fallback: $($launcher.File) $($allArgs -join ' ')"
            & $launcher.File @allArgs
            $pyExit = $LASTEXITCODE
            Write-Host "Python launcher '$($launcher.File)' exit code: $pyExit"
            if ($pyExit -eq 0) {
                exit 0
            }
        } catch {
            Write-Host "Python launcher '$($launcher.File)' failed: $($_.Exception.Message)"
        }
    }

    Write-Host "ERROR: All launch methods failed (native and Python fallback)."
    exit 1
} finally {
    Pop-Location
}
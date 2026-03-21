param(
    [string]$Username,
    [string]$ServerIp,
    [string]$JvmOpts
)

# Set compatibility layer to avoid UAC prompts
$env:__COMPAT_LAYER = "RUNASINVOKER"

# Fallback to positional arguments if needed
if (-not $JvmOpts -and $args.Count -gt 0) {
    $JvmOpts = $args[0]
}

Write-Host "DEBUG: Username = '$Username'"
Write-Host "DEBUG: ServerIp = '$ServerIp'"
Write-Host "DEBUG: JvmOpts = '$JvmOpts'"
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
    $url = "https://github.com/mindstorm38/portablemc/releases/download/v5.0.2/portablemc-5.0.2-windows-x86_64-msvc.zip"
    $zipPath = Join-Path $baseDir "portablemc.zip"

    # Check if insecure SSL is allowed
    $allowInsecure = $env:ALLOW_INSECURE_SSL -in @("true", "1", "yes")

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
$jvmArgs = $JvmOpts -replace ' ', ','
$arguments = "--main-dir . start --join-server $ServerIp --jvm-arg=$jvmArgs fabric: -u $Username"

Write-Host "Launching: $exePath $arguments"
Write-Host "Working directory: $baseDir"

# Ensure the environment variable is set for the child process
$env:__COMPAT_LAYER = "RUNASINVOKER"

# Launch Minecraft with output redirection
$tempOut = Join-Path $env:TEMP "portablemc_out.txt"
$tempErr = Join-Path $env:TEMP "portablemc_err.txt"
$process = Start-Process -FilePath $exePath -ArgumentList $arguments -WorkingDirectory $baseDir -NoNewWindow -PassThru -RedirectStandardOutput $tempOut -RedirectStandardError $tempErr

$timeout = 30
$elapsed = 0
while ($elapsed -lt $timeout) {
    if ($process.HasExited) { break }
    Start-Sleep -Seconds 1
    $elapsed++
}

if ($process.HasExited) {
    Write-Host "Process exited with code $($process.ExitCode)"
    if (Test-Path $tempOut) { Get-Content $tempOut }
    if (Test-Path $tempErr) { Get-Content $tempErr }
    Remove-Item $tempOut, $tempErr -Force -ErrorAction SilentlyContinue
    exit $process.ExitCode
} else {
    Write-Host "Process still running after $timeout seconds - detaching."
    Remove-Item $tempOut, $tempErr -Force -ErrorAction SilentlyContinue
    exit 0
}
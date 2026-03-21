$env:__COMPAT_LAYER = "RUNASINVOKER"

param(
    [string]$Username,
    [string]$ServerIp,
    [string]$JvmOpts
)

# Fallback to positional arguments if needed
if (-not $JvmOpts -and $args.Count -ge 0) {
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

# --- Function to create a junction, removing any existing folder/link first ---
function Ensure-Junction {
    param(
        [string]$Source,
        [string]$Target
    )
    # Create source folder if it doesn't exist (so junction has a target)
    if (-not (Test-Path $Source)) {
        New-Item -ItemType Directory -Path $Source -Force | Out-Null
        Write-Host "Created source folder: $Source"
    }
    # If target already exists, remove it (whether it's a folder or a junction)
    if (Test-Path $Target) {
        Remove-Item -Path $Target -Recurse -Force
        Write-Host "Removed existing target: $Target"
    }
    Write-Host "Creating junction: $Target -> $Source"
    cmd /c mklink /J "$Target" "$Source" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Junction created successfully."
    } else {
        Write-Host "Failed to create junction. Ensure source exists and you're on the same drive."
    }
}

# --- Ensure the base directory exists ---
if (-not (Test-Path $baseDir)) {
    New-Item -ItemType Directory -Path $baseDir -Force | Out-Null
}

# --- Download portablemc.exe if missing (with SSL fallback) ---
if (-not (Test-Path $exePath)) {
    Write-Host "portablemc.exe not found. Downloading to $binDir..."
    $url = "https://github.com/mindstorm38/portablemc/releases/download/v5.0.2/portablemc-5.0.2-windows-x86_64-msvc.zip"
    $zipPath = Join-Path $baseDir "portablemc.zip"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Host "First download attempt failed: $_"
        Write-Host "Retrying with SSL verification disabled..."
        try {
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = {$true}
            Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
        } catch {
            Write-Host "Download failed. Exiting."
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

# --- Create junctions for mods and resourcepacks (force recreation) ---
Ensure-Junction -Source (Join-Path $rootDir "mods") -Target (Join-Path $baseDir "mods")
Ensure-Junction -Source (Join-Path $rootDir "resourcepacks") -Target (Join-Path $baseDir "resourcepacks")

# --- Process JVM options and launch ---
$jvmArgs = $JvmOpts -replace ' ', ','
$arguments = "--main-dir . start --join-server $ServerIp --jvm-arg=$jvmArgs fabric: -u $Username"

Write-Host "Launching: $exePath $arguments"
Write-Host "Working directory: $baseDir"

$env:__COMPAT_LAYER = "RUNASINVOKER"

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
    Write-Host "Process still running after $timeout seconds – detaching."
    Remove-Item $tempOut, $tempErr -Force -ErrorAction SilentlyContinue
    exit 0
}
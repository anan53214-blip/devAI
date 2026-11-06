[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot "backend\.venv\Scripts\python.exe"
$frontendModules = Join-Path $projectRoot "frontend\node_modules\"
$processes = @(Get-CimInstance Win32_Process)
$targets = @($processes | Where-Object {
    ($_.Name -eq "python.exe" -and $_.ExecutablePath -eq $pythonPath -and
        $_.CommandLine -match '-m\s+uvicorn\s+app\.main:app') -or
    ($_.Name -eq "node.exe" -and $_.CommandLine -and
        $_.CommandLine.IndexOf($frontendModules, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -match '\\next\\')
})

if (-not $targets.Count) {
    Write-Host "No running DevFlow AI services found."
    return
}

$targetIds = @($targets | ForEach-Object { $_.ProcessId })
foreach ($process in $targets) {
    if ($process.ParentProcessId -in $targetIds) {
        continue
    }
    if ($PSCmdlet.ShouldProcess("$($process.Name) PID $($process.ProcessId)", "Stop DevFlow AI process tree")) {
        # Recheck identity before stopping a process from the earlier snapshot.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)"
        if (-not $current -or $current.CreationDate -ne $process.CreationDate) {
            continue
        }
        & taskkill.exe /PID $process.ProcessId /T /F
        if ($LASTEXITCODE -ne 0) {
            throw "Could not stop DevFlow AI process $($process.ProcessId)."
        }
    }
}

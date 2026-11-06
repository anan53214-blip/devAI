param(
    [switch]$Demo,
    [switch]$UseConfiguredLLM,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$pythonPath = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$dataDirectory = Join-Path $projectRoot ".data"

if ($Demo) {
    & (Join-Path $PSScriptRoot "start_rag.ps1") -UseConfiguredLLM:$UseConfiguredLLM -BackendPort $BackendPort -FrontendPort $FrontendPort
    return
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "未找到项目 Python 环境，请先安装后端依赖。"
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory "node_modules") -PathType Container)) {
    throw "未找到前端依赖，请先在 frontend 目录执行 npm install。"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".env") -PathType Leaf)) {
    throw "请先根据 .env.example 创建并填写项目根目录的 .env。"
}
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

function Wait-AppEndpoint {
    param([string]$Uri, [System.Diagnostics.Process]$Process, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw "服务进程提前退出，请查看 .data 目录中的日志。"
        }
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 3 | Out-Null
            return
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "等待服务就绪超时，请查看 .data 目录中的日志。"
}

function Test-DockerEngine {
    param([string]$DockerPath)
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $DockerPath info --format '{{.OSType}}' *> $null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
}

Push-Location $backendDirectory
try {
    $environmentNames = & $pythonPath -c 'import json; from app.core.config import Settings; print(json.dumps([name.upper() for name in Settings.model_fields]))'
    if ($LASTEXITCODE -ne 0) { throw "读取项目配置失败。" }
    foreach ($name in ($environmentNames | ConvertFrom-Json)) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    $configurationJson = & $pythonPath -c 'import json; from app.core.config import settings; print(json.dumps(dict(milvus_enabled=settings.milvus_enabled, demo_mode=settings.demo_mode)))'
    if ($LASTEXITCODE -ne 0) { throw "读取 .env 配置失败。" }
    $configuration = $configurationJson | ConvertFrom-Json

    if ($configuration.milvus_enabled) {
        $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
        $dockerCandidates = @(
            (Join-Path $env:USERPROFILE "AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"),
            (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
        )
        $dockerPath = $null
        if ($dockerCommand) { $dockerPath = $dockerCommand.Source }
        if (-not $dockerPath) {
            $dockerPath = $dockerCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        }
        if (-not $dockerPath) { throw "请先安装 Docker Desktop。" }
        $dockerBin = Split-Path -Parent $dockerPath
        $env:PATH = $dockerBin + ";" + $env:PATH
        if (-not (Test-DockerEngine -DockerPath $dockerPath)) {
            $dockerRoot = Split-Path -Parent (Split-Path -Parent $dockerBin)
            $desktopPath = Join-Path $dockerRoot "Docker Desktop.exe"
            if (-not (Test-Path -LiteralPath $desktopPath)) { throw "请先打开 Docker Desktop，等待引擎就绪。" }
            Start-Process -FilePath $desktopPath -WindowStyle Hidden
            $engineReady = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                if (Test-DockerEngine -DockerPath $dockerPath) { $engineReady = $true; break }
                Start-Sleep -Seconds 2
            }
            if (-not $engineReady) { throw "Docker 引擎尚未就绪，请查看 Docker Desktop。" }
        }
        & $dockerPath compose -f (Join-Path $projectRoot "docker-compose.milvus.yml") up -d --wait --wait-timeout 120
        if ($LASTEXITCODE -ne 0) { throw "Milvus 启动失败，请检查容器日志。" }
    }

    $statusJson = & $pythonPath -c 'import json; from app.main import runtime_status; print(json.dumps(runtime_status()))'
    if ($LASTEXITCODE -ne 0) { throw "依赖检查失败，请核对 PostgreSQL 和 Milvus 配置。" }
    $status = $statusJson | ConvertFrom-Json
    if ($status.status -ne "ready") { throw "数据库或向量库尚未就绪，请核对 .env 和服务状态。" }

    & (Join-Path $PSScriptRoot "stop_rag.ps1")
    $env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:$BackendPort"
    $env:PYTHONPATH = $backendDirectory
    $nodePath = (Get-Command node.exe -ErrorAction Stop).Source
    $nextCli = Join-Path $frontendDirectory "node_modules\next\dist\bin\next"
    $backendProcess = $null
    $frontendProcess = $null
    try {
        $backendProcess = Start-Process -FilePath $pythonPath `
            -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
            -WorkingDirectory $backendDirectory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataDirectory "devflow-backend.out.log") `
            -RedirectStandardError (Join-Path $dataDirectory "devflow-backend.err.log")
        Wait-AppEndpoint -Uri "http://127.0.0.1:$BackendPort/health" -Process $backendProcess -TimeoutSeconds 60
        $frontendProcess = Start-Process -FilePath $nodePath `
            -ArgumentList @(('"{0}"' -f $nextCli), "dev", "-p", "$FrontendPort") `
            -WorkingDirectory $frontendDirectory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dataDirectory "devflow-frontend.out.log") `
            -RedirectStandardError (Join-Path $dataDirectory "devflow-frontend.err.log")
        Wait-AppEndpoint -Uri "http://127.0.0.1:$FrontendPort" -Process $frontendProcess -TimeoutSeconds 120
    }
    catch {
        & (Join-Path $PSScriptRoot "stop_rag.ps1")
        throw
    }
    Write-Host "DevFlow AI 已按 .env 配置启动"
    Write-Host "前端 http://127.0.0.1:$FrontendPort"
    Write-Host "后端 http://127.0.0.1:$BackendPort"
    Write-Host "运行状态 http://127.0.0.1:$BackendPort/api/system/status"
    Write-Host "停止服务请执行 stop.cmd"
}
finally {
    Pop-Location
}

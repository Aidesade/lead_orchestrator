$ErrorActionPreference = "Stop"

# Лаунчеры лежат в lead_orchestrator/bootstrap: корень репозитория на два уровня выше.
$candidateRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$repoRoot = if (Test-Path -LiteralPath (Join-Path $candidateRoot "rusprofile_28_pending.json")) {
    $candidateRoot
} else {
    "D:\lead_gen"
}
$inputJson = Join-Path $repoRoot "rusprofile_28_pending.json"
$outputDir = "D:\лиды_нефтяная_отрасль"
$defaultMainPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$mainPython = if ($env:ORQ_MAIN_PY) { $env:ORQ_MAIN_PY } elseif (Test-Path -LiteralPath $defaultMainPython) { $defaultMainPython } else { $null }
if (-not $mainPython -or -not (Test-Path -LiteralPath $mainPython)) {
    throw "Основной Python 3.12 не найден. Задайте ORQ_MAIN_PY до python.exe."
}
& $mainPython -c "import docx; assert docx.__version__"
if ($LASTEXITCODE -ne 0) {
    throw "В основном Python отсутствует python-docx==1.2.0: $mainPython"
}

$env:PYTHONUTF8 = "1"
$env:ORQ_MAIN_PY = $mainPython
$env:ORQ_STORE = "local"
$env:ORQ_DATA_ROOT = $outputDir
$env:ORQ_DELIVERABLES_DIR = $outputDir
$env:ORQ_DELIVERABLE_KEY = "name"
$env:KIMI_TOOL_LOG_DIR = Join-Path $outputDir "tool_logs"
$env:DR_LLM_PROVIDER = "kimi"
$env:KIMI_WRITER_AGENT = "1"
# Внешний дедлайн охватывает сразу 2 DRE-прохода + 5 enrichment-ролей + 2 writer-сессии.
# Не обрываем весь company pipeline; каждая вложенная Kimi-стадия имеет свой timeout ниже.
$env:ORQ_RESEARCH_TIMEOUT = "0"
$env:KIMI_WRITER_AGENT_TIMEOUT = "2400"
$env:KIMI_RESEARCH_SUBAGENTS_TIMEOUT = "2400"
$env:KIMI_RESEARCH_COMPANY_CONCURRENCY = "2"
if (-not $env:KIMI_API_KEY) { $env:KIMI_API_KEY = $env:GPLLM_API_KEY }
if (-not $env:KIMI_BASE_URL) { $env:KIMI_BASE_URL = "https://gpllmkeeper.dtc.tatar/v1" }
if (-not $env:KIMI_MODEL_NAME) { $env:KIMI_MODEL_NAME = "kimi-k2.7-code" }

$host.UI.RawUI.WindowTitle = "Kimi Orchestrator - oil 28 - PHASE 2"
& $mainPython (Join-Path $repoRoot "_prepare_rusprofile_28.py")
if ($LASTEXITCODE -ne 0) { throw "Не удалось подготовить входной JSON" }

New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
New-Item -ItemType Directory -Force -Path $env:KIMI_TOOL_LOG_DIR | Out-Null

Write-Host "ФАЗА 2: нефтегазовые компании без сохранённого deep-research, модель Kimi"
Write-Host "Результаты: $outputDir\<название компании>"
Write-Host "Tool-use логи: $env:KIMI_TOOL_LOG_DIR"
Write-Host ""

Push-Location (Join-Path (Split-Path -Parent $PSScriptRoot) "app")
try {
    & $mainPython "orchestrator.py" $inputJson --model kimi --workers 1
    $rc = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Прогон завершён, код: $rc"
Read-Host "Нажмите Enter, чтобы закрыть окно"
exit $rc

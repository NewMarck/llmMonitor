$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $venvPython = Join-Path $PSScriptRoot '../.venv/Scripts/python.exe'
    if (Test-Path $venvPython) {
        & $venvPython -m llmmonitor.cli @args
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m llmmonitor.cli @args
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m llmmonitor.cli @args
    } else {
        throw 'Installa Python 3.10+ e aggiungilo al PATH.'
    }
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result

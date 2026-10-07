$ErrorActionPreference='Stop'
Push-Location $PSScriptRoot
try {
 if(-not(Test-Path -LiteralPath '.venv\Scripts\python.exe')){ & python -m venv .venv; if($LASTEXITCODE){throw 'Environment creation failed'} }
 & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
 if($LASTEXITCODE){throw 'Dependency installation failed'}
} finally { Pop-Location }

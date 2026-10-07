$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $python)){throw 'Run Setup.ps1 first to create the project environment.'}
Push-Location $PSScriptRoot
try { & $python 'photo_copy.py' @args; if($LASTEXITCODE){throw 'Application exited with an error'} } finally { Pop-Location }

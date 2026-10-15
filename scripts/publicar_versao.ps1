# Publica a versão do branch atual: push, PR, merge, tag, espera o workflow,
# assina o manifest (pede a senha da chave) e publica a release.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\publicar_versao.ps1 `
#       -Titulo "Titulo da PR" -CorpoArquivo .git\pr-body.md
#
# Para no primeiro erro. A chave privada fica fora do projeto.

param(
    [Parameter(Mandatory = $true)][string]$Titulo,
    [Parameter(Mandatory = $true)][string]$CorpoArquivo,
    [string]$Chave = "C:\chaves\acessopiscinas-release.pem"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Passo([string]$texto) {
    Write-Host ""
    Write-Host "==> $texto" -ForegroundColor Cyan
}

function Rodar {
    param([string]$exe, [string[]]$argumentos)
    & $exe @argumentos
    if ($LASTEXITCODE -ne 0) {
        throw "Falhou: $exe $($argumentos -join ' ')"
    }
}

$branch = (git branch --show-current).Trim()
if ($branch -eq "main" -or -not $branch) {
    throw "Rode a partir do branch da versão, não do main."
}
if (git status --porcelain --untracked-files=no) {
    throw "Há alterações sem commit. Faça o commit antes de publicar."
}
if (-not (Test-Path $CorpoArquivo)) { throw "Arquivo da descrição não encontrado: $CorpoArquivo" }
if (-not (Test-Path $Chave)) { throw "Chave privada não encontrada: $Chave" }

$versao = [regex]::Match((Get-Content app\version.py -Raw), 'VERSION = "([0-9.]+)"').Groups[1].Value
if (-not $versao) { throw "Não achei VERSION em app\version.py" }
$tag = "v$versao"
Write-Host "Branch $branch -> versão $versao ($tag)" -ForegroundColor Green

Passo "Enviando o branch"
Rodar git @("push", "-u", "origin", $branch)

Passo "Abrindo a PR"
$ErrorActionPreference = "Continue"
gh pr view $branch --json number 2>&1 | Out-Null
$prExiste = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $prExiste) {
    Rodar gh @("pr", "create", "--base", "main", "--head", $branch, "--title", $Titulo, "--body-file", $CorpoArquivo)
} else {
    Write-Host "A PR já existe."
}

Passo "Fazendo o merge da PR"
Rodar gh @("pr", "merge", $branch, "--merge")

Passo "Atualizando o main"
Rodar git @("switch", "main")
Rodar git @("pull", "--ff-only")

Passo "Criando e enviando a tag $tag"
Rodar git @("tag", $tag)
Rodar git @("push", "origin", $tag)

Passo "Esperando o workflow Release"
$runId = $null
for ($i = 0; $i -lt 30 -and -not $runId; $i++) {
    Start-Sleep -Seconds 5
    $runId = gh run list --workflow release.yml --branch $tag --limit 1 --json databaseId -q ".[0].databaseId"
}
if (-not $runId) { throw "O workflow da tag $tag não apareceu no GitHub." }
Rodar gh @("run", "watch", "$runId", "--exit-status")

Passo "Baixando o rascunho da release"
$pasta = "release-$tag"
if (Test-Path $pasta) { Remove-Item -Recurse -Force $pasta }
Rodar gh @("release", "download", $tag, "--dir", $pasta)

Passo "Conferindo e assinando (digite a senha da chave)"
Rodar ".venv\Scripts\python.exe" @(
    "scripts\release_sign.py",
    "--manifest", "$pasta\manifest.json",
    "--zip", "$pasta\AcessoPiscinas-$versao.zip",
    "--key", $Chave
)

Passo "Anexando a assinatura e publicando"
Rodar gh @("release", "upload", $tag, "$pasta\manifest.sig")
Rodar gh @("release", "edit", $tag, "--draft=false")

Write-Host ""
Write-Host "Versão $versao publicada. Nos PCs: Procurar atualização e depois Instalar." -ForegroundColor Green

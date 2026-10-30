# P23: execute the actual README blocks, only against the dedicated fixture stack.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Readme = Get-Content -LiteralPath (Join-Path $ProjectRoot 'README.md') -Raw
$Compose = Get-Content -LiteralPath (Join-Path $ProjectRoot 'docker-compose.e2e.yml') -Raw
$PublicToken = [regex]::Match($Compose, 'ARTICLE_GRAPH_APP_TOKEN: ([^\r\n]+)').Groups[1].Value
if (-not $PublicToken) { throw 'Missing public fixture App Token' }
$Names = docker ps --filter 'label=com.docker.compose.project=geomotiv-p23-docs' --format '{{.Names}}'
if ($LASTEXITCODE -ne 0 -or -not ($Names -match 'geomotiv-p23-docs-api')) {
    throw 'Start the dedicated geomotiv-p23-docs stack first'
}
$Scratch = Join-Path $ProjectRoot ('.cache/p23-curl-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Scratch | Out-Null

function Require([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}
function Read-Block([string] $Name) {
    $Pattern = '(?s)<!-- p23:' + [regex]::Escape($Name) + ' -->\s*```powershell\r?\n(.*?)```'
    $Match = [regex]::Match($Readme, $Pattern)
    Require $Match.Success ('Missing README block: ' + $Name)
    return $Match.Groups[1].Value
}
function Wait-Article([string] $Url, [string] $Expected) {
    $End = [DateTime]::UtcNow.AddSeconds(180)
    do {
        $Current = curl.exe --silent --show-error --fail-with-body "$Base$Url" -H "Authorization: Bearer $Access" | ConvertFrom-Json
        Require ($LASTEXITCODE -eq 0) 'Status curl failed'
        if ($Current.status -in @('completed', 'failed')) {
            Require ($Current.status -eq $Expected) 'Unexpected terminal state'
            return $Current
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $End)
    throw 'Status timeout'
}

Push-Location $ProjectRoot
try {
    # Fixture creation uses local Python only, without any provider settings.
    & '.venv/Scripts/uv.exe' run --frozen python -c 'import sys; from pathlib import Path; from tests.e2e.p21_fixture import diagram_pdf; root=Path(sys.argv[1]); (root/"article.pdf").write_bytes(diagram_pdf()); (root/"no-arrow.pdf").write_bytes(diagram_pdf(arrow=False))' $Scratch
    Require ($LASTEXITCODE -eq 0) 'PDF fixture creation failed'
    Push-Location $Scratch
    try {
        $Session = (Read-Block 'session').Replace('http://127.0.0.1:8000', 'http://127.0.0.1:38000').Replace('<app-token-from-local-env>', $PublicToken)
        . ([scriptblock]::Create($Session))
        Require ($Live.status -eq 'ok' -and $Ready.status -eq 'ok') 'Health checks failed'
        Require ([bool]$Access -and [bool]$Refresh) 'Token issuance failed'
        Require ([bool]$OpenApi.paths -and ($Swagger -match 'SwaggerUIBundle') -and ($Redoc -match 'redoc')) 'API documentation routes failed'
        . ([scriptblock]::Create((Read-Block 'upload')))
        Require ($Noop.unchanged -and $Noop.job_id -eq $JobId -and $Noop.revision_id -eq $RevisionId) 'Hash no-op mismatch'
        . ([scriptblock]::Create((Read-Block 'markdown-file')))
        Require ($FileUpload.unchanged -and $FileUpload.job_id -eq $JobId) 'File no-op mismatch'
        . ([scriptblock]::Create((Read-Block 'pdf')))
        $PdfState = Wait-Article $PdfUpload.status_url 'completed'
        Require ($PdfState.active_revision_id -eq $PdfUpload.revision_id) 'PDF not published'
        . ([scriptblock]::Create((Read-Block 'update')))
        $UpdatedState = Wait-Article $Update.status_url 'completed'
        Require ($Update.article_id -eq $ArticleId -and $Update.revision_id -ne $RevisionId) 'Update revision mismatch'
        . ([scriptblock]::Create((Read-Block 'idempotency')))
        Require ($First.job_id -eq $Replay.job_id -and $First.revision_id -eq $Replay.revision_id) 'Idempotency replay mismatch'
        $ReplayState = Wait-Article $First.status_url 'completed'
        . ([scriptblock]::Create((Read-Block 'graph')))
        Require (@($ArticleCharacters.items).Count -eq 2) 'Article graph read failed'
        Require (@($Types.items).Count -ge 1 -and @($Connected.items).Count -ge 1 -and @($Friends.items).Count -ge 1) 'Graph types/connected filter failed'
        $OldRefresh = $Refresh
        . ([scriptblock]::Create((Read-Block 'refresh')))
        Require ($Refresh -ne $OldRefresh -and $StateAfterRefresh.status -eq 'completed') 'Refresh rotation failed'
        $Failed = curl.exe --silent --show-error --fail-with-body -X POST "$Base/articles/upload" -H "Authorization: Bearer $Access" -F 'file=@no-arrow.pdf;type=application/pdf' | ConvertFrom-Json
        $FailedState = Wait-Article $Failed.status_url 'failed'
        $RetryBlock = (Read-Block 'retry').Replace('<failed-article-uuid>', $Failed.article_id)
        . ([scriptblock]::Create($RetryBlock))
        Require ($Retry.revision_id -eq $Failed.revision_id -and $Retry.job_id -ne $Failed.job_id) 'Retry identity mismatch'
        $RetriedState = Wait-Article $Retry.status_url 'failed'
        $Conflict = curl.exe --silent --show-error -X POST "$Base/articles/$ArticleId/retry" -H "Authorization: Bearer $Access" --output NUL --write-out '%{http_code}'
        Require ([int]$Conflict -eq [int][System.Net.HttpStatusCode]::Conflict) 'Completed retry must conflict'
        $NoopCode = curl.exe --silent --show-error -X POST "$Base/articles/upload" -H "Authorization: Bearer $Access" --form-string "article_id=$ArticleId" --form-string "text=$Changed" --output NUL --write-out '%{http_code}'
        Require ([int]$NoopCode -eq [int][System.Net.HttpStatusCode]::OK) 'No-op HTTP status mismatch'
        Write-Output 'README curl: all 11 API operations and 3 documentation routes passed; Markdown/PDF, poll, no-op/update/replay/refresh/retry checked. No token/body output.'
    } finally {
        Pop-Location
    }
} finally {
    Pop-Location
    # Only known synthetic filenames within the verified scratch directory.
    foreach ($Name in @('article.pdf', 'no-arrow.pdf', 'article.md')) {
        $Target = Join-Path $Scratch $Name
        if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target }
    }
    Remove-Item -LiteralPath $Scratch
}

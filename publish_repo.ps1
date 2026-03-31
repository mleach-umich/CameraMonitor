$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

if (-not (Test-Path .git)) {
    git init
}

if (-not (git config user.name)) {
    git config user.name "mleach-umich"
}

if (-not (git config user.email)) {
    git config user.email "mleach@users.noreply.github.com"
}

git add .

$staged = git diff --cached --name-only
if ($staged) {
    git commit -m "Initial commit"
}

$owner = "mleach-umich"
$repoName = "CameraMonitor"
$repoUrl = "https://github.com/$owner/$repoName.git"

$existingRemote = git remote
if ($existingRemote -contains "origin") {
    git remote set-url origin $repoUrl
}
else {
    git remote add origin $repoUrl
}

$repoExists = $true
gh repo view "$owner/$repoName" | Out-Null 2>$null
if ($LASTEXITCODE -ne 0) {
    $repoExists = $false
}

if (-not $repoExists) {
    gh repo create "$owner/$repoName" --public --source . --remote origin --push
}
else {
    git branch -M main
    git push -u origin main
}

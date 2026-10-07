# Daily local equivalent of the GitHub Actions / Azure Pipelines refresh job - runs collect.py,
# then commits and pushes data/ if anything changed. Meant to be invoked by a Windows Task
# Scheduler task (see README.md's "Run the daily refresh locally" section for how it's registered),
# not run interactively.
#
# Needs GEMINI_API_KEY / GROQ_API_KEY set as permanent (User or System) environment variables -
# Task Scheduler doesn't inherit a terminal session's env vars, only ones set that way.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$log = Join-Path $repo "refresh-local.log"
"=== $(Get-Date -Format o) ===" | Out-File -Append -FilePath $log

python scripts/collect.py *>> $log

git add data
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "Refresh feed $(Get-Date -Format yyyy-MM-dd) (local)" *>> $log
    git pull --rebase --autostash *>> $log
    git push *>> $log
    "Committed and pushed new data." | Out-File -Append -FilePath $log
} else {
    "No new data to commit." | Out-File -Append -FilePath $log
}

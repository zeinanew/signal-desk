# Daily local equivalent of the GitHub Actions / Azure Pipelines refresh job - runs collect.py,
# then commits and pushes data/ if anything changed. Runs via the daily 8:00 AM Task Scheduler
# trigger, or manually (Task Scheduler -> right-click -> Run) - either way, progress streams to
# both the console and refresh-local.log, so a manual run doesn't look like it's doing nothing
# while it's actually working (a slow run is usually the Gemini API having a rough day - see the
# log for the actual reason, not a hang).
#
# Needs GEMINI_API_KEY / GROQ_API_KEY set as permanent (User or System) environment variables -
# Task Scheduler doesn't inherit a terminal session's env vars, only ones set that way.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$log = Join-Path $repo "refresh-local.log"
"=== $(Get-Date -Format o) ===" | Tee-Object -FilePath $log -Append

python scripts/collect.py 2>&1 | Tee-Object -FilePath $log -Append

git add data
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "Refresh feed $(Get-Date -Format yyyy-MM-dd) (local)" 2>&1 | Tee-Object -FilePath $log -Append
    git pull --rebase --autostash 2>&1 | Tee-Object -FilePath $log -Append
    git push 2>&1 | Tee-Object -FilePath $log -Append
    "Committed and pushed new data." | Tee-Object -FilePath $log -Append
} else {
    "No new data to commit." | Tee-Object -FilePath $log -Append
}

---
name: release
description: Cut a Service Visuals release — bump version.py, run the smoke suite, commit, tag, push, wait for CI, and report the published assets. Use when the user asks to release, ship, cut a version, or publish a build.
disable-model-invocation: true
---

# Release Service Visuals

Pushing a `v*` tag builds the Mac `.app` and Windows `.exe` in GitHub Actions and
attaches both to the Release. **Users auto-update**, so a bad tag reaches real
churches on a Sunday. Every step below exists because of that.

The user invokes this deliberately. Do not run it off your own judgement.

## 1. Decide the version

`version.py` holds the single source of truth. Bump the **minor** for a new
feature or a user-visible fix (the project's habit — 1.21, 1.22, 1.23), the
**patch** only for a same-day correction to a release just shipped.

If the user named a version, use theirs.

## 2. Preflight — all of these, before touching anything

```bash
git status --short                 # know exactly what is going out
git log --oneline -3
git branch --show-current          # expect main
```

- Anything unrelated in the working tree? Ask before sweeping it into the release.
- **Never commit church content**: `Grouppoints.png`, `ChatGPT Image*.png`,
  `Summit-*.png` are gitignored — confirm none are staged.
- Delete test renders from `exports/` (it is the user's real output folder):
  `rm -f exports/*.mp4 exports/*.png`

## 3. Gate

```bash
.venv/bin/python -m pyflakes *.py render/*.py scripts/*.py
SERVICE_VISUALS_STATS=0 .venv/bin/python scripts/smoke.py
```

Both must be clean. `SERVICE_VISUALS_STATS=0` keeps the release run out of the
user's analytics. If smoke fails, stop and fix — never tag over a red suite.

If this release touched `render/timer.py`, also confirm countdown output is
still byte-identical (see CLAUDE.md for the recipe).

## 4. Bump, commit, tag, push

```bash
# version.py — the only place the version lives
printf '"""Single source of the app version (read by app.py, desktop.py, CI)."""\n\nAPP_VERSION = "X.Y.Z"\n' > version.py
```

Commit message style, matching the repo's history — write it as prose that
explains **why**, with concrete detail (numbers, the bug it fixes), not a
bullet list of files:

```
Area: what changed, in plain words (vX.Y.Z)

A paragraph on the problem this solves, in the user's terms. Name the
concrete thing — the measurement, the failure, the symptom they reported.

A second paragraph if there is a tradeoff or a limit worth recording, and
what was verified.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

```bash
git add -A <the files this release actually touches>
git commit -F - <<'EOF'
...message...
EOF
git tag vX.Y.Z
git push -q origin main && git push -q origin vX.Y.Z
```

## 5. Watch CI, then report

```bash
sleep 45
RUN=$(gh run list --limit 1 --json databaseId -q '.[0].databaseId')
until [ "$(gh run view $RUN --json status -q .status)" = "completed" ]; do sleep 20; done
gh run view $RUN --json conclusion -q .conclusion
gh release view vX.Y.Z --json assets -q '.assets[] | "\(.name) \(.size)"'
```

Run the wait in the background so the user isn't blocked.

**A release is not done until both assets exist.** Expect
`ServiceVisuals-mac.zip` (~40 MB) and `ServiceVisuals-windows.zip` (~55 MB).
One asset missing means one platform's build failed even if the run says
success — check `gh run view $RUN --log-failed`.

Then tell the user: the version, the release URL, what changed in a sentence,
and anything they need to do by hand (e.g. "re-upload the boards saved before
this fix"). If a change is unverifiable on macOS — the Windows updater, winocr,
the GPU encoder — say so plainly rather than implying it was tested.

## If it goes wrong

- **CI red**: `gh run view $RUN --log-failed`. Fix, commit, then move the tag:
  `git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z` and re-tag. Only
  safe within minutes of pushing, before anyone has downloaded it.
- **Already downloaded a bad build**: do not delete the release. Ship the fix
  as the next patch version so the auto-updater carries users forward.

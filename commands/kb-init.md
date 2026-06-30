---
description: Scaffold a knowledge-base vault into the current repo (Core; optional Obsidian + Astro board viewers).
---

# /kb-init

Scaffold the KB directory structure into a target git repository. Idempotent — files that
already exist are **never overwritten**. Follows STAGE → PAUSE → SUBMIT for every mutation.

## Usage

```
/kb-init [target-dir] [--with-obsidian] [--with-board]
```

- `target-dir` — path to the repo root to scaffold into. Defaults to `${CLAUDE_PROJECT_DIR}`
  (the current project) or, if that is not set, your current working directory.
- `--with-obsidian` — also copy the Obsidian layer (`scaffold/obsidian/`) and print plugin
  setup guidance.
- `--with-board` — also copy the Astro board (`scaffold/board/web/`) into `<target>/web/`
  and print the build commands.

---

## Step 1 — Resolve and validate the target directory

Resolve `target-dir`:

1. If a path argument was given, use it.
2. Else use `${CLAUDE_PROJECT_DIR}` if set.
3. Else use the current working directory.

Verify the resolved path is a git repository:

```bash
git -C "<target>" rev-parse --git-dir
```

If the command fails (exit non-zero), do **not** auto-run `git init`. Instead, tell the
user the path is not a git repository and **offer** to run `git init "<target>"`, then
**pause and wait for confirmation** before proceeding.

---

## Step 2 — Stage the core scaffold (STAGE → PAUSE → SUBMIT)

**STAGE:** List every file under `${CLAUDE_PLUGIN_ROOT}/scaffold/core/` and identify which
files **do not already exist** at the corresponding path under `<target>/`. Show the user
two lists:

- Files that WILL be copied (absent from target)
- Files that will be SKIPPED (already present in target — never overwritten)

**PAUSE:** Ask the user to confirm before copying anything. Do not proceed without
explicit confirmation.

**SUBMIT:** Copy only the absent files, preserving directory structure. Use the
platform-appropriate command:

*Linux / macOS:*
```bash
# -n = no-clobber (never overwrite existing files)
cp -rn "${CLAUDE_PLUGIN_ROOT}/scaffold/core/." "<target>/"
```

*Windows (PowerShell):*
```powershell
# robocopy /XC /XN = skip files that already exist (no overwrite)
robocopy "${env:CLAUDE_PLUGIN_ROOT}\scaffold\core" "<target>" /E /XC /XN /XO
```

*Windows (Copy-Item alternative):*
```powershell
Get-ChildItem -Path "${env:CLAUDE_PLUGIN_ROOT}\scaffold\core" -Recurse -File |
  ForEach-Object {
    $dest = Join-Path "<target>" $_.FullName.Substring("${env:CLAUDE_PLUGIN_ROOT}\scaffold\core".Length)
    if (-not (Test-Path $dest)) {
      New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
      Copy-Item $_.FullName $dest
    }
  }
```

After copying, report which files were written and which were skipped.

---

## Step 3 — Obsidian layer (`--with-obsidian` only)

If `--with-obsidian` was passed, repeat the STAGE → PAUSE → SUBMIT pattern for
`${CLAUDE_PLUGIN_ROOT}/scaffold/obsidian/` → `<target>/`:

*Linux / macOS:*
```bash
cp -rn "${CLAUDE_PLUGIN_ROOT}/scaffold/obsidian/." "<target>/"
```

*Windows (PowerShell):*
```powershell
robocopy "${env:CLAUDE_PLUGIN_ROOT}\scaffold\obsidian" "<target>" /E /XC /XN /XO
```

After copying, print:

> **Required Obsidian plugins:** See `<target>/00-meta/required-plugins.md` for the full
> list of plugins that must be installed and enabled before the KB renders correctly.
> Open that file in Obsidian or a text editor after vault setup.

---

## Step 4 — Astro board (`--with-board` only)

If `--with-board` was passed, stage `${CLAUDE_PLUGIN_ROOT}/scaffold/board/web/` →
`<target>/web/` (same only-if-absent rule, same STAGE → PAUSE → SUBMIT gate).

*Linux / macOS:*
```bash
cp -rn "${CLAUDE_PLUGIN_ROOT}/scaffold/board/web/." "<target>/web/"
```

*Windows (PowerShell):*
```powershell
robocopy "${env:CLAUDE_PLUGIN_ROOT}\scaffold\board\web" "<target>\web" /E /XC /XN /XO
```

Note: `node_modules` is NOT bundled. After copying, print:

```
cd "<target>/web"
npm install
npm run build
```

---

## Step 5 — Identity stamp

Run the reconciler stamp in dry-run first so the user can review what it would write:

```bash
py -3 "<target>/tools/reconciler/reconciler.py" stamp --vault "<target>" --dry-run
```

Review the output. If it looks correct, run without `--dry-run`:

```bash
py -3 "<target>/tools/reconciler/reconciler.py" stamp --vault "<target>"
```

This writes the `.kb-id` file (a stable UUID4) that anchors vault identity. It is
idempotent — re-running when `.kb-id` already exists is a no-op.

---

## Step 6 — Next steps

Print the following guidance:

```
KB scaffold complete. Before running /kb-sync, set two environment variables:

  KB_VAULT   — absolute path to the vault (the target repo root you just scaffolded)
  KB_DEV_ROOT — absolute path to the directory that CONTAINS your source repos
                (e.g. ~/repos on Linux; D:\repos on Windows)
                Used by kb-sync to locate repos and write relative paths into cards.

Example (Linux / macOS):
  export KB_VAULT="$HOME/repos/knowledge-base"
  export KB_DEV_ROOT="$HOME/repos"

Example (Windows PowerShell):
  $env:KB_VAULT  = "D:\repos\knowledge-base"
  $env:KB_DEV_ROOT = "D:\repos"

Then run:
  /kb-sync              — initial harvest + card generation
  /kb-status            — verify freshness and graph health

If you copied the Astro board (--with-board):
  cd "<target>/web" && npm install && npm run build
  Then serve or open dist/index.html.

If you copied the Obsidian layer (--with-obsidian):
  Open "<target>" as an Obsidian vault, install the required plugins listed in
  00-meta/required-plugins.md, then restart Obsidian.
```

---

## Invariants (enforce throughout)

- **Only-if-absent:** never overwrite a file that already exists in the target. The user's
  existing content is authoritative.
- **STAGE → PAUSE → SUBMIT:** show the file list and wait for confirmation before any write.
- **Cross-platform:** always offer both Linux/macOS and Windows variants of shell commands.
- **No hardcoded paths:** use `${CLAUDE_PLUGIN_ROOT}` (Bash/Linux) or
  `${env:CLAUDE_PLUGIN_ROOT}` (PowerShell) for all plugin-relative paths. Fill in
  `<target>` from the resolved directory — never assume a fixed location.

#!/usr/bin/env node
// kb-enforce.js — SessionStart hook: nudge adoption of the knowledge-base structure.
//
// Contract:
//   Reads CLAUDE_PROJECT_DIR (falls back to cwd) and checks whether the project looks
//   like a Git repo that has NOT yet adopted KB structure. If so, emits a JSON nudge
//   to stdout suggesting `/kb-init`.
//
// Opt-outs (silent exit 0, no output):
//   - Env var KB_ENFORCE === '0'
//   - File `.kb-ignore` present in the project dir
//   - Vault self-detection: dir contains BOTH `00-meta/` AND `llms.txt`
//   - KB already adopted: dir contains `active/plan/` OR `.kb-id`
//   - Not a Git repo: no `.git` entry in the project dir
//
// This hook WRITES NOTHING to disk and NEVER exits non-zero.
// Any error → silent exit 0 (a failing hook must never break a Claude session).
// Node built-ins only (fs, path). No stdin — runs synchronously.

'use strict';

const fs   = require('fs');
const path = require('path');

try {
  // ── Opt-out 1: env flag ────────────────────────────────────────────────────
  if (process.env.KB_ENFORCE === '0') process.exit(0);

  // ── Resolve project dir ────────────────────────────────────────────────────
  const dir = process.env.CLAUDE_PROJECT_DIR || process.cwd();

  // ── Opt-out 2: .kb-ignore file ────────────────────────────────────────────
  if (fs.existsSync(path.join(dir, '.kb-ignore'))) process.exit(0);

  // ── Opt-out 3: vault self-detection (00-meta/ + llms.txt both present) ────
  if (
    fs.existsSync(path.join(dir, '00-meta')) &&
    fs.existsSync(path.join(dir, 'llms.txt'))
  ) process.exit(0);

  // ── Opt-out 4: KB already adopted (active/plan/ or .kb-id present) ────────
  if (
    fs.existsSync(path.join(dir, 'active', 'plan')) ||
    fs.existsSync(path.join(dir, '.kb-id'))
  ) process.exit(0);

  // ── Opt-out 5: not a Git repo (no .git entry) ─────────────────────────────
  if (!fs.existsSync(path.join(dir, '.git'))) process.exit(0);

  // ── Nudge: repo is Git-tracked but lacks KB structure ─────────────────────
  const message =
    'This repo has not adopted the knowledge-base standard (no active/plan/ or .kb-id). ' +
    'Run /kb-init to scaffold KB structure. ' +
    'To silence this nudge, add a .kb-ignore file to the repo root or set KB_ENFORCE=0.';

  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName:     'SessionStart',
      additionalContext: message,
    },
  }));
} catch {
  // Fail-open: any error → silent exit 0.
}

process.exit(0);

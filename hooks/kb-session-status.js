#!/usr/bin/env node
// kb-session-status.js — SessionStart hook: KB freshness status.
//
// Zero-cost path: sessions whose cwd is NOT under the vault dir exit 0 immediately
// with no file I/O, no subprocess, and no stdout — only stdin accumulation is paid.
//
// Under the vault: emits one additionalContext line with KB sha, stale project count,
// board URL, and any prerequisite warnings. Reads a pre-computed JSON file (no live
// graph scan). One git subprocess call only, bounded to 1500 ms.
//
// Prerequisites check (non-fatal, warn only):
//   - Node.js < 24 → one-line warning folded into the status output.
//   - Python < 3.13 (checked via python3/python --version) → one-line warning.
//     Missing python binary is silently ignored — not all KB users need Python.
//
// Environment:
//   KB_VAULT      — absolute path to the vault repo root (default: CLAUDE_PROJECT_DIR, then cwd)
//   KB_BOARD_URL  — URL of the KB Mission Control board (default: http://localhost:4321)
//
// Fail-open: any unhandled error anywhere exits 0 silently.

'use strict';

const fs            = require('fs');
const path          = require('path');
const { spawnSync } = require('child_process');

// Vault dir: KB_VAULT takes priority, then CLAUDE_PROJECT_DIR, then cwd.
const KB_VAULT     = process.env.KB_VAULT || process.env.CLAUDE_PROJECT_DIR || process.cwd();
const LINEAGE_FILE = path.join(KB_VAULT, '00-meta', 'lineage-quality.json');
const BOARD_URL    = process.env.KB_BOARD_URL || 'http://localhost:4321';

const GIT_OPTS = {
  encoding:    'utf8',
  stdio:       ['ignore', 'pipe', 'ignore'],
  timeout:     1500,
  windowsHide: true,
};

// Safety: if stdin never ends (e.g. invoked bare in a test without input), exit.
const stdinTimeout = setTimeout(() => process.exit(0), 3000);

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  clearTimeout(stdinTimeout);

  // ── 1. Parse stdin JSON defensively ────────────────────────────────────────
  let data;
  try { data = JSON.parse(raw); } catch { process.exit(0); }
  // Guard: JSON.parse('null') returns null (no throw), and scalars/arrays are
  // not objects we can property-access.  Treat any non-plain-object as empty.
  if (data === null || typeof data !== 'object' || Array.isArray(data)) data = {};

  // ── 2. cwd scope guard — ZERO-COST path for sessions outside the vault ─────
  const cwd = (typeof data.cwd === 'string' ? data.cwd : process.cwd());
  // Normalise to forward-slash lower-case, strip trailing slash, for a
  // case-insensitive prefix check that works on Windows and Linux.
  const norm = (s) => s.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  if (!norm(cwd).startsWith(norm(KB_VAULT))) process.exit(0);

  // ── 3. Under the vault: build the status line cheaply ─────────────────────
  try {
    // 3a. Stale count — one cheap file read of a pre-computed JSON artifact.
    //     null  = file missing / corrupt / unexpected shape → "unavailable".
    //     0     = all fresh.
    //     N > 0 = N projects behind HEAD.
    let count = null;
    try {
      const lineageRaw = fs.readFileSync(LINEAGE_FILE, 'utf8');
      const parsed = JSON.parse(lineageRaw);
      const pf = parsed?.summary?.project_freshness;
      if (pf && typeof pf.stale === 'number' && typeof pf.very_stale === 'number') {
        count = pf.stale + pf.very_stale;
      }
    } catch { /* missing or corrupt — count stays null */ }

    // 3b. KB sha — one spawnSync, bounded, fail-soft.
    let sha = null;
    const gitResult = spawnSync('git', ['-C', KB_VAULT, 'rev-parse', '--short', 'HEAD'], GIT_OPTS);
    if (gitResult.status === 0 && gitResult.stdout) {
      sha = gitResult.stdout.trim();
    }

    // 3c. Prerequisite warnings (non-fatal).
    const warnings = [];

    // Node version check: process.versions.node is always available, no subprocess.
    const nodeMajor = parseInt(process.versions.node.split('.')[0], 10);
    if (!isNaN(nodeMajor) && nodeMajor < 24) {
      warnings.push(`Node.js ${process.versions.node} detected — v24+ required`);
    }

    // Python version check: try python3 first, fall back to python. ENOENT = skip.
    const pythonCandidates = ['python3', 'python'];
    for (const py of pythonCandidates) {
      const pyResult = spawnSync(py, ['--version'], {
        encoding:    'utf8',
        stdio:       ['ignore', 'pipe', 'pipe'], // python2 writes version to stderr
        timeout:     1500,
        windowsHide: true,
      });
      if (pyResult.error) continue; // ENOENT or other spawn error — skip
      const pyOut = (pyResult.stdout || pyResult.stderr || '').trim();
      const pyMatch = pyOut.match(/Python\s+(\d+)\.(\d+)/i);
      if (pyMatch) {
        const major = parseInt(pyMatch[1], 10);
        const minor = parseInt(pyMatch[2], 10);
        if (major < 3 || (major === 3 && minor < 13)) {
          warnings.push(`Python ${pyOut.replace(/^Python\s+/i, '')} detected — 3.13+ required`);
        }
        break; // found a python, stop searching
      }
    }

    // 3d. Compose the status line.
    const prefix = sha ? `KB @ ${sha}` : 'KB';
    let middle;
    if (count === null) {
      middle = '/kb-sync to refresh';
    } else if (count === 0) {
      middle = 'all documented';
    } else {
      middle = `${count} project(s) HEAD-moved — run /kb-sync`;
    }
    let line = `${prefix} · ${middle} · board: ${BOARD_URL}`;
    if (warnings.length > 0) {
      line += ` · WARN: ${warnings.join('; ')}`;
    }

    // ── 4. Emit additionalContext ──────────────────────────────────────────────
    process.stdout.write(JSON.stringify({
      hookSpecificOutput: {
        hookEventName:     'SessionStart',
        additionalContext: line,
      },
    }));
  } catch { /* fail-open — any runtime error, emit nothing */ }

  process.exit(0);
});

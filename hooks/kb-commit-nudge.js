#!/usr/bin/env node
// kb-commit-nudge.js — PostToolUse hook: nudge /kb-sync after commits in KB-tracked repos.
// Advisory only — never blocks, always exits 0. Fail-open: any error → silent exit 0.

'use strict';
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

// If the whole thing hangs for any reason, bail out silently.
const GLOBAL_TIMEOUT = setTimeout(() => process.exit(0), 5000);

const NUDGE_THRESHOLD = 1; // emit advisory whenever HEAD is >= N commits ahead

// Vault dir: KB_VAULT takes priority, then CLAUDE_PROJECT_DIR, then cwd.
// The tier repos live in the vault's parent directory (siblings of the vault).
const KB_VAULT = process.env.KB_VAULT || process.env.CLAUDE_PROJECT_DIR || process.cwd();
const KB_PROJECTS_DIR = path.join(KB_VAULT, '02-projects');
const TIER_ROOT = path.dirname(KB_VAULT); // parent of the vault = tier scan root
const TIER_NAMES = ['1.0-dev', '1.1-dev-tools', '2.0-career', '3.0-work', '5.0-home'];

const GIT_OPTS = {
  encoding: 'utf8',
  stdio: ['ignore', 'pipe', 'ignore'],
  timeout: 1500,
  windowsHide: true,
};

function git(args, cwd) {
  return spawnSync('git', args, { ...GIT_OPTS, cwd });
}

// Normalize a Windows or POSIX path for comparison: forward slashes, lowercase, no trailing slash.
function normPath(p) {
  return p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

// Normalize a git remote URL for clone detection: canonicalize SSH<->HTTPS,
// drop .git suffix / trailing slash, lowercase. SSH (git@host:owner/repo) and
// HTTPS (https://host/owner/repo) forms of the same remote must compare equal —
// clones on a Linux host often use SSH origins while cards store the HTTPS repo: URL.
function normUrl(u) {
  return u
    .replace(/^git@([^:]+):/i, 'https://$1/')   // git@host:owner/repo -> https://host/owner/repo
    .replace(/^ssh:\/\/git@/i, 'https://')       // ssh://git@host/...   -> https://host/...
    .replace(/\.git$/i, '')
    .replace(/\/+$/, '')
    .toLowerCase();
}

// Strip surrounding single or double quotes from a YAML scalar value string.
function stripQuotes(s) {
  return s.replace(/^['"]|['"]$/g, '');
}

// Extract `repo:` and `last-documented-sha:` from YAML frontmatter without a full YAML parser.
// Returns { repoUrl, lastSha } or null if either field is missing (path: is not required).
function parseFrontmatter(content) {
  const fmMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!fmMatch) return null;
  const fm = fmMatch[1];

  const shaMatch = fm.match(/^last-documented-sha:\s*"?([0-9a-f]{7,40})"?/m);
  const repoMatch = fm.match(/^repo:\s*(.+)$/m);
  if (!shaMatch || !repoMatch) return null;

  return {
    lastSha: shaMatch[1].trim(),
    repoUrl: normUrl(stripQuotes(repoMatch[1].trim())),
  };
}

// Walk KB_PROJECTS_DIR recursively, return all .md files excluding _INDEX.md.
function findCards(base) {
  const results = [];
  function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) { walk(full); continue; }
      if (e.isFile() && e.name.endsWith('.md') && e.name !== '_INDEX.md') {
        results.push(full);
      }
    }
  }
  walk(base);
  return results;
}

// Emit the advisory JSON line to stdout.
function nudge(msg) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: msg,
    },
  }));
}

// ── O(n) commit tokenizer — no regex backtracking ───────────────────────────
// Replaces the prior COMMIT_SUBCMD_RE which had nested quantifiers susceptible
// to ReDoS on long flag strings.  This tokenizer is strictly O(n) with no
// backtracking: split once on whitespace, then a single left-to-right scan.
//
// Behaviour preserved from prior implementation:
//   git commit -m x              → IS a commit
//   git -C /path commit          → IS a commit
//   git --no-pager commit -m x   → IS a commit
//   git log --grep=commit        → NOT a commit (log is the subcommand)
//   git commit-graph write       → NOT a commit (subcommand != "commit")
//   echo "git commit -m x"       → NOT a commit (token[0] is "echo", not git)
//
// Chained / cd-prefixed commits ARE detected (revised 2026-06-22): `isRealCommit`
// splits the command into shell segments on &&, ||, ;, and newline, then runs the
// per-segment tokenizer below on each. This is REQUIRED, not cosmetic — CLAUDE.md §7
// mandates directory-anchored `cd "<dir>" && git commit ...` for every command, so
// `cd x && git commit` is the DOMINANT real-world shape; the prior token[0]==git-only
// check silently no-op'd the nudge for virtually every real commit. PowerShell uses
// `;` to chain, also covered. Advisory-only: a non-commit whose QUOTED text contains
// "&& git commit -m y" may over-nudge — harmless (a spurious /kb-sync suggestion).

function isRealCommit(cmd) {
  // Belt-and-suspenders length cap: no realistic git command is > 2000 chars.
  if (typeof cmd !== 'string' || cmd.length > 2000) return false;
  // Split into shell command segments so chained / cd-prefixed commits are seen.
  // O(n) split, no backtracking. Each segment is validated by isCommitSegment.
  return cmd.split(/&&|\|\||;|\n/).some(isCommitSegment);
}

// Per-segment tokenizer: true iff THIS single segment is a real `git commit`.
function isCommitSegment(segment) {
  const tokens = segment.trim().split(/\s+/);
  if (tokens.length === 0 || tokens[0] === '') return false;

  // token[0] must be git (or a path whose basename is git / git.exe).
  const base = tokens[0].split(/[/\\]/).pop().toLowerCase();
  if (base !== 'git' && base !== 'git.exe') return false;

  // Walk tokens[1..] to find the git subcommand.
  // Global options that consume the NEXT token: -C <path>  and  -c <cfg>.
  // All other -xxx tokens are flags (consumed alone, even long ones with no =).
  let subcommand = null;
  let i = 1;
  while (i < tokens.length) {
    const t = tokens[i];
    if (t === '-C' || t === '-c') {
      i += 2; // skip this flag and its argument
      continue;
    }
    if (t.startsWith('-')) {
      i += 1; // skip single flag (long flags like --no-pager carry no separate arg)
      continue;
    }
    subcommand = t; // first non-option token is the subcommand
    i += 1;
    break;
  }

  if (subcommand !== 'commit') return false;

  // --dry-run check: only suppress if --dry-run appears as an option token
  // BEFORE the first -m / --message / -F / --file (which begins the message
  // value).  This prevents `git commit -m "fix --dry-run bug"` being falsely
  // suppressed (--dry-run inside the quoted value must not count).
  // i now points to the token after the subcommand.
  const MESSAGE_STARTERS = new Set(['-m', '--message', '-F', '--file']);
  while (i < tokens.length) {
    const t = tokens[i];
    if (MESSAGE_STARTERS.has(t)) break; // stop scanning; anything after is a value
    if (t === '--dry-run') return false; // real --dry-run option before message
    i += 1;
  }

  return true;
}

// ── Main ────────────────────────────────────────────────────────────────────
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { raw += c; });
process.stdin.on('end', () => {
  clearTimeout(GLOBAL_TIMEOUT);
  // Re-arm a tighter timeout for the real work.
  const workTimeout = setTimeout(() => process.exit(0), 4500);

  try {
    // 1. Parse stdin defensively.
    let data;
    try { data = JSON.parse(raw); } catch { process.exit(0); }

    // 2. Cheap filter: only proceed on real git commits.
    const cmd = (data.tool_input?.command || '');
    if (!isRealCommit(cmd)) { clearTimeout(workTimeout); process.exit(0); }

    // 3. Resolve repo toplevel.
    const cwd = data.cwd || process.cwd();
    const topResult = git(['rev-parse', '--show-toplevel'], cwd);
    if (topResult.status !== 0) { clearTimeout(workTimeout); process.exit(0); }
    const repoTop = normPath(topResult.stdout.trim());

    // 4. Scan KB cards; build URL→card map for origin-URL-based matching.
    const cards = findCards(KB_PROJECTS_DIR);
    const cardedRepoUrls = new Set(); // for clone/snapshot detection by remote URL
    const urlToCard = new Map();      // normUrl → { lastSha, name } (first card wins per URL)
    for (const cardPath of cards) {
      let content;
      try { content = fs.readFileSync(cardPath, 'utf8'); } catch { continue; }
      const fm = parseFrontmatter(content);
      if (!fm) continue;
      if (fm.repoUrl) {
        cardedRepoUrls.add(fm.repoUrl);
        if (!urlToCard.has(fm.repoUrl)) {
          urlToCard.set(fm.repoUrl, {
            lastSha: fm.lastSha,
            name: path.basename(cardPath, '.md'),
          });
        }
      }
      // NOTE: do NOT break — we must scan ALL cards to build cardedRepoUrls / urlToCard.
    }

    // Resolve this repo's remote origin URL for matching.
    const urlRes = git(['config', '--get', 'remote.origin.url'], repoTop);
    const originUrl = urlRes.status === 0 ? normUrl(urlRes.stdout.trim()) : '';
    const matched = originUrl ? urlToCard.get(originUrl) : null;

    if (!matched) {
      // System-wide onboarding nudge: a commit in a tier repo with NO card
      // is an untracked project the KB doesn't know about. Nudge to onboard it.
      // Skip repos outside the 5 concept-group tiers (standalone forks, the vault
      // itself) and obvious snapshots/clones to avoid noise.
      const TIERS = TIER_NAMES.map(t => normPath(path.join(TIER_ROOT, t)) + '/');
      const underTier = TIERS.some(t => repoTop.startsWith(t));
      const isVault = repoTop === normPath(KB_VAULT);
      const inStandalone = repoTop.includes('/standalone/');
      if (underTier && !isVault && !inStandalone) {
        // Clone/snapshot detection by REMOTE URL (not name substring): a true clone
        // shares a tracked project's origin (e.g. my-app-feat-x-snapshot == my-app's
        // remote), whereas a distinct tool like backup-tool has its own remote.
        // Substring matching on "snapshot" would wrongly skip backup-tool.
        const isClone = originUrl !== '' && cardedRepoUrls.has(originUrl);
        if (!isClone) {
          const projName = repoTop.split('/').pop();
          nudge(`KB: '${projName}' has no knowledge-base card — run /kb-sync to onboard this untracked project.`);
        }
      }
      clearTimeout(workTimeout); process.exit(0);
    }

    // 5. Check drift: HEAD vs documented sha.
    const headResult = git(['rev-parse', 'HEAD'], repoTop);
    if (headResult.status !== 0) { clearTimeout(workTimeout); process.exit(0); }
    const head = headResult.stdout.trim();

    if (head === matched.lastSha) {
      // Card is already current.
      clearTimeout(workTimeout); process.exit(0);
    }

    // Count commits ahead. Non-zero exit means documented sha not in history.
    const countResult = git(
      ['rev-list', '--count', `${matched.lastSha}..HEAD`],
      repoTop,
    );
    if (countResult.status !== 0) {
      // Documented sha not found in repo history (e.g. shallow clone or rewrite).
      nudge(`KB: ${matched.name} documented sha is not in history — run /kb-sync to refresh the knowledge base.`);
      clearTimeout(workTimeout); process.exit(0);
    }

    const drift = parseInt(countResult.stdout.trim(), 10);
    if (!isNaN(drift) && drift >= NUDGE_THRESHOLD) {
      nudge(`KB: ${matched.name} is ${drift} commit(s) ahead of its documented sha — run /kb-sync to refresh the knowledge base.`);
    }
  } catch {
    // Fail-open: any uncaught error → silent exit.
  }

  clearTimeout(workTimeout);
  process.exit(0);
});

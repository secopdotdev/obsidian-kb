#!/usr/bin/env node
/**
 * build-backlinks.mjs — prebuild script
 *
 * Scans the vault (repo root, two levels up from web/scripts/) for *.md, parses:
 *   1. [[wikilinks]] in body text
 *   2. `related: []` frontmatter field
 *
 * Builds a Map<target-slug, source-slug[]> and writes it to
 * src/data/backlinks.json.
 *
 * Called automatically as the `prebuild` npm script.
 * Also safe to run manually: `node scripts/build-backlinks.mjs`
 *
 * VAULT CONTRACT: Requires the vault (repo root) to be present.
 * If the vault is absent, writes an empty {} and exits cleanly.
 */

import { readFileSync, writeFileSync, readdirSync, existsSync, mkdirSync } from 'node:fs';
import { join, relative, extname, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
// App lives at knowledge-base/web/; this script at knowledge-base/web/scripts/.
// Vault root is two levels up (scripts -> web -> repo root). KB_VAULT overrides.
const VAULT_DIR = process.env.KB_VAULT ?? join(__dirname, '..', '..');
const OUT_FILE  = join(__dirname, '..', 'src', 'data', 'backlinks.json');

// 'web' is excluded so the backlink walk never recurses into the app's own
// subtree (incl. web/node_modules/**/*.md), which would pollute backlinks.
const EXCLUDED_DIRS = new Set(['.obsidian', '_templates', '_attachments', 'web']);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Collect all .md files, excluding _-prefixed dirs and EXCLUDED_DIRS. */
function collectMdFiles(dir) {
  const results = [];
  if (!existsSync(dir)) return results;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (EXCLUDED_DIRS.has(entry.name)) continue;
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...collectMdFiles(fullPath));
    } else if (
      entry.isFile() &&
      extname(entry.name) === '.md' &&
      !entry.name.startsWith('_')
    ) {
      results.push(fullPath);
    }
  }
  return results;
}

/** Parse YAML-ish frontmatter from a markdown file. Returns { body, frontmatter }. */
function parseFrontmatter(content) {
  const fm = {};
  let body = content;

  if (content.startsWith('---')) {
    const end = content.indexOf('\n---', 3);
    if (end !== -1) {
      const yaml = content.slice(3, end).trim();
      body = content.slice(end + 4);

      // Parse `related:` field (array of wikilink strings like ["[[slug]]"]).
      const relatedMatch = yaml.match(/^related:\s*\[([^\]]*)\]/m);
      if (relatedMatch) {
        const items = relatedMatch[1]
          .split(',')
          .map((s) => s.trim().replace(/^["']|["']$/g, ''))
          .filter(Boolean);
        fm.related = items;
      } else {
        // Multi-line related list.
        const lines = yaml.split('\n');
        let inRelated = false;
        const related = [];
        for (const line of lines) {
          if (/^related:/.test(line)) {
            inRelated = true;
            continue;
          }
          if (inRelated) {
            const m = line.match(/^\s+-\s+"?(\[\[.+?\]\])"?/);
            if (m) {
              related.push(m[1]);
            } else if (/^\S/.test(line)) {
              inRelated = false;
            }
          }
        }
        if (related.length > 0) fm.related = related;
      }
    }
  }

  return { body, frontmatter: fm };
}

/**
 * Extract all [[wikilinks]] from text (body or frontmatter strings).
 * Returns array of raw inner text: "some-note", "Some Note|alias", etc.
 */
function extractWikilinks(text) {
  const WIKILINK_RE = /\[\[([^\]]+)\]\]/g;
  const results = [];
  let match;
  while ((match = WIKILINK_RE.exec(text)) !== null) {
    // Support [[target|alias]] — take target only.
    const [target] = match[1].split('|');
    results.push(target.trim());
  }
  return results;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

if (!existsSync(VAULT_DIR)) {
  console.warn(`[build-backlinks] Vault not found at ${VAULT_DIR}. Writing empty backlinks.`);
  ensureDir(OUT_FILE);
  writeFileSync(OUT_FILE, '{}', 'utf-8');
  process.exit(0);
}

function ensureDir(filePath) {
  const dir = filePath.replace(/[\\/][^\\/]+$/, '');
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
}

const mdFiles = collectMdFiles(VAULT_DIR);

// Build slug → full relative path (e.g. "02-projects/example-cli").
/** @type {Map<string, string>} basename → full slug */
const slugByBasename = new Map();
/** @type {string[]} full slugs */
const allSlugs = [];

for (const file of mdFiles) {
  const rel = relative(VAULT_DIR, file).replace(/\\/g, '/').replace(/\.md$/, '');
  allSlugs.push(rel);
  const name = basename(rel);
  if (!slugByBasename.has(name)) {
    slugByBasename.set(name, rel);
  }
}

/**
 * Resolve a wikilink target string to a full slug.
 * Tries: exact full slug, then basename lookup.
 */
function resolveLink(target) {
  // Normalize: strip .md, handle subdirectory targets.
  const normalized = target.replace(/\.md$/, '').replace(/\\/g, '/');

  // Exact match on full slug.
  if (allSlugs.includes(normalized)) return normalized;

  // Basename match.
  const name = basename(normalized);
  return slugByBasename.get(name) ?? null;
}

/** @type {Map<string, Set<string>>} target-slug → Set of source-slugs */
const backlinks = new Map();

function addBacklink(target, source) {
  if (!backlinks.has(target)) backlinks.set(target, new Set());
  backlinks.get(target).add(source);
}

for (const file of mdFiles) {
  const rel = relative(VAULT_DIR, file).replace(/\\/g, '/').replace(/\.md$/, '');
  let content;
  try {
    content = readFileSync(file, 'utf-8');
  } catch {
    continue;
  }

  const { body, frontmatter } = parseFrontmatter(content);

  // 1. Wikilinks in body.
  for (const target of extractWikilinks(body)) {
    const resolved = resolveLink(target);
    if (resolved && resolved !== rel) {
      addBacklink(resolved, rel);
    }
  }

  // 2. `related:` frontmatter.
  for (const item of frontmatter.related ?? []) {
    // Items may be "[[some-note]]" or "some-note".
    const raw = item.replace(/^\[\[|\]\]$/g, '');
    const resolved = resolveLink(raw);
    if (resolved && resolved !== rel) {
      addBacklink(resolved, rel);
    }
  }
}

// Serialize: Map<slug, string[]> → plain object.
const output = {};
for (const [target, sources] of backlinks.entries()) {
  output[target] = [...sources].sort();
}

ensureDir(OUT_FILE);
writeFileSync(OUT_FILE, JSON.stringify(output, null, 2), 'utf-8');

const linkCount = Object.values(output).reduce((s, arr) => s + arr.length, 0);
console.log(`[build-backlinks] Wrote ${Object.keys(output).length} entries (${linkCount} links) to ${OUT_FILE}`);

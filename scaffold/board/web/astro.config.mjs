// astro.config.mjs
//
// VAULT AVAILABILITY NOTE:
// The app lives at knowledge-base/web/ — the vault root is exactly one level up (`..`).
// KB_VAULT overrides the default (Docker build sets KB_VAULT=/vault).
// Absence of the vault produces an empty permalinks list and empty content collection
// — the build still succeeds but renders no notes.
//
// Local dev: run from knowledge-base/web/ and the default fallback (`..`) resolves correctly.

import { defineConfig } from 'astro/config';
import { readdirSync, existsSync } from 'node:fs';
import { join, basename, extname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { remarkWikilinks } from './src/lib/remark-wikilinks.mjs';

const __dirname = fileURLToPath(new URL('.', import.meta.url));

// ---------------------------------------------------------------------------
// Build permalink list from vault at config time.
// obsidian-short resolves [[basename]] → slug; we map every note to its slug
// so remark-wiki-link can generate the correct href and mark broken links.
// Slugs are relative paths (without .md) from the vault root, matching
// the `note.id` shape that `[...id].astro` routes use.
// ---------------------------------------------------------------------------
// App at knowledge-base/web/; vault root is one level up (`..`).
// KB_VAULT overrides (Docker build sets it to the COPYed corpus path).
const VAULT_DIR = process.env.KB_VAULT ?? join(__dirname, '..');

// 'web' is excluded so the vault walk never recurses into the app's own
// source tree (including web/node_modules/**) when vault root is `..`.
const EXCLUDED_DIRS = new Set(['.obsidian', '_templates', '_attachments', 'web']);

/** Recursively collect all .md files under dir, excluding EXCLUDED_DIRS. */
function collectMdFiles(dir) {
  /** @type {string[]} */
  const results = [];
  if (!existsSync(dir)) return results;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (EXCLUDED_DIRS.has(entry.name)) continue;
    const fullPath = join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...collectMdFiles(fullPath));
    } else if (entry.isFile() && extname(entry.name) === '.md' && !entry.name.startsWith('_')) {
      results.push(fullPath);
    }
  }
  return results;
}

/**
 * Build the permalinks array expected by @portaljs/remark-wiki-link.
 * Each entry maps an Obsidian-style short name (basename without .md) to the
 * full slug (relative path without .md) used in our /notes/[...id] routes.
 *
 * @portaljs/remark-wiki-link accepts `permalinks` as string[] where each entry
 * is the *href target*. With pathFormat:'obsidian-short' the plugin matches the
 * wikilink text against the basename portion of each permalink. We supply the
 * full relative path so generated hrefs are correct.
 */
const mdFiles = collectMdFiles(VAULT_DIR);
const permalinks = mdFiles.map((file) => {
  // e.g. "02-projects/example-cli"
  const rel = relative(VAULT_DIR, file).replace(/\\/g, '/').replace(/\.md$/, '');
  return rel;
});

// Also build a flat map of basename → full slug for href templating.
// When obsidian-short can't match by basename alone (duplicates), first wins.
const slugByBasename = new Map();
for (const slug of permalinks) {
  const name = basename(slug);
  if (!slugByBasename.has(name)) slugByBasename.set(name, slug);
}

export default defineConfig({
  output: 'static',

  markdown: {
    remarkPlugins: [
      // Custom, dependency-free wikilink resolver (see src/lib/remark-wikilinks.mjs).
      // Resolves [[slug]] / [[basename]] / [[target|alias]] to /notes/<slug> routes,
      // marks unresolved links with .broken-link, and ignores code spans.
      [remarkWikilinks, { allSlugs: permalinks, slugByBasename }],
    ],
  },

  // Vite config: let Astro import JSON from src/data/ even before prebuild runs.
  vite: {
    // No special aliases needed — src/data/backlinks.json is within the project root.
  },
});

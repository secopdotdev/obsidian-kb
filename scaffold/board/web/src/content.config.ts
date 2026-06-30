// src/content.config.ts  (Astro 6 Content Layer API — NOT src/content/config.ts)
import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';
import { pathToFileURL } from 'node:url';

// ---------------------------------------------------------------------------
// Schema mirrors §5 of 01-kb-architecture.md exactly.
// Most fields are optional/.default so generated/partial notes never fail the
// build. Dates are leniently coerced from strings. The schema uses .catchall()
// (Zod 4 passthrough) so any extra frontmatter passes through without errors.
//
// NOTE on Zod 4 passthrough:
// In Zod 4 (shipped with Astro 6) the .passthrough() method is replaced by
// using z.object({...}).catchall(z.unknown()) which captures and retains all
// unknown keys. Astro's content layer tolerates unknown keys even without
// catchall, but we add it explicitly so frontmatter isn't stripped.
// ---------------------------------------------------------------------------

// Render-only site over 24 projects' worth of machine-generated frontmatter:
// EVERY field is lenient. Typed fields use .catch(fallback) so any value-shape
// mismatch (an object where a string was expected, a malformed date, etc.)
// degrades to the fallback instead of failing the whole build. .catchall keeps
// unknown keys. str()/strArr() helpers keep this terse and uniform.
const str = () => z.string().optional().catch(undefined);
const strArr = () => z.array(z.string()).optional().catch([]).default([]);
const bool = () => z.boolean().optional().catch(undefined);
const date = () => z.coerce.date().optional().catch(undefined);

const noteSchema = z
  .object({
    // --- Universal (§5) ---
    type: z
      .enum(['project', 'adr', 'cli', 'error', 'runbook', 'reference', 'moc', 'tool', 'gate', 'objective', 'goal'])
      .optional()
      .catch(undefined),
    title: str(),
    aliases: strArr(),
    tags: strArr(),
    status: z
      .enum(['active', 'deprecated', 'draft', 'accepted', 'proposed', 'rejected', 'superseded'])
      .optional()
      .catch(undefined),
    created: date(),
    updated: date(),
    related: strArr(),

    // --- Project card extras ---
    repo: str(),
    branch: str(),
    live: bool(),
    'rag-flag': z.enum(['red', 'yellow', 'green']).optional().catch(undefined),
    // Blockers round-trip from repo frontmatter as rich objects
    // ({text, severity, since, unblock}); older/hand-written cards may use bare strings.
    // Accept BOTH so the real object shape is no longer silently dropped to [].
    blockers: z
      .array(
        z.union([
          z.string(),
          z
            .object({
              text: z.string(),
              severity: str(),
              since: str(),
              unblock: str(),
              slug: str(),
            })
            .catchall(z.unknown()),
        ]),
      )
      .optional()
      .catch([])
      .default([]),
    'next-action': str(),
    'next-command': str(),
    'next-command-shell': z.enum(['bash', 'ps1', 'ssh']).optional().catch(undefined),
    'last-documented-sha': str(),
    tier: str(),
    docs: str(),
    stale: bool(),
    // Phase 4 artifact inventory — written by kb-harvest.harvest_artifacts()
    readme_index_exists: bool(),
    plan_file_exists: bool(),
    decision_count: z.number().optional().catch(undefined),
    // Operator completion audit trail (operator moves done nextsteps here; preserved across syncs)
    completed_steps: z.array(z.string()).optional().catch([]).default([]),
    // Generator-stamped ISO date of last kb-sync run that touched this card
    'last-sync': str(),
    requires: strArr(),   // explicit upstream-prerequisite edges (note slugs/aliases)
    goal: bool(),         // marks a project/milestone end-goal node
    kind: str(),          // 'ultimate' | 'milestone' (objective nodes only)

    // --- Mission Control swim-lane fields (operator-owned; written only-if-blank) ---
    // Which end-goal this project advances -> lane membership. 'shared' = a
    // cross-cutting dev utility used by several lanes (see `supports`).
    advances: z
      .enum(['objective-a', 'objective-b', 'career', 'home', 'shared'])
      .optional()
      .catch(undefined),
    // Maturity column, left (start) -> right (end-goal).
    phase: z.enum(['seed', 'build', 'harden', 'ship']).optional().catch(undefined),
    // Absolute local working dir for VS Code / terminal launch (verified on disk).
    path: str(),
    // For shared utilities: which end-goal lanes this tool supports (agent-readable
    // so zero-context agents in those projects can detect the shared dependency).
    supports: strArr(),
    blocks_objectives: strArr(), // objective ids this node advances/blocks
    blocked_by: z.array(z.unknown()).optional().catch([]).default([]), // upstream blockers for an objective

    // --- Data-plane operator fields (plan 03) ---
    // Prose; written only-if-blank.
    nextsteps: z.array(z.string()).optional().catch([]).default([]),
    objective: z.string().optional().catch(undefined),
    file: z.string().optional().catch(undefined),       // repo-relative; joined with `path` at launch
    problem: z.string().optional().catch(undefined),
    solution: z.string().optional().catch(undefined),
    adrs: strArr(), // references to ADR notes (slug/alias); summaries rendered from ADR bodies, never copied here

    // --- Remote working trees (governs the CLAUDE.md §9 homelab-sync obligation) ---
    // Each entry documents a homelab SSH checkout of this repo. Absent/empty array =>
    // no remote tree exists => the §9 SSH-sync step does NOT apply (an origin push
    // completes the sync). host = SSH alias/hostname; path = absolute checkout dir;
    // branch = the branch that tree tracks (optional). This is the single field that
    // makes "is there a remote tree to sync?" answerable without SSH-hunting.
    'remote-trees': z
      .array(
        z
          .object({ host: z.string(), path: z.string(), branch: str() })
          .catchall(z.unknown()),
      )
      .optional()
      .catch([])
      .default([]),

    // --- ADR stub extras ---
    'adr-id': str(),
    project: str(),
    supersedes: str(),
    'superseded-by': str(),
    deciders: strArr(),
    'date-decided': date(),
    source: str(),

    // --- CLI / error atomic extras ---
    tool: str(),
    command: str(),
    code: str(),
    'exit-code': z.union([z.string(), z.number()]).optional().catch(undefined),
    since: str(),

    // Breadcrumbs hierarchy
    up: str(),

    // Flags list (any note may emit flags:)
    flags: z.array(z.unknown()).optional().catch([]).default([]),

    // Gate extras (type: gate notes only; lenient so partial notes don't fail build)
    'gate-id': str(),
    blocking: bool(),
    gates: strArr(),    // downstream slugs this gate blocks (edges: gate -> X "gates")
    criteria: strArr(), // acceptance-criteria lines (from frontmatter; inline parsed separately)
  })
  .catchall(z.unknown());

// App lives at <vault>/web/; vault root is one level up (`..`).
// Override with KB_VAULT (Docker build sets KB_VAULT=/vault and COPYs the corpus there).
const VAULT = process.env.KB_VAULT ?? '..';

// Astro 6's glob loader runs fileURLToPath() on `base`, so an absolute path
// (the normal KB_VAULT case, e.g. /vault or C:\repos\kb) must be a file:// URL.
// Relative paths resolve against the project root and pass through unchanged.
const isAbsolute = VAULT.startsWith('/') || /^[A-Za-z]:[\\/]/.test(VAULT);
const base = isAbsolute ? pathToFileURL(VAULT.replace(/\\/g, '/').replace(/\/?$/, '/')) : VAULT;

export const collections = {
  notes: defineCollection({
    loader: glob({
      base,
      pattern: ['**/*.md', '!**/_*/**', '!**/.obsidian/**', '!_*', '!web/**'],
    }),
    schema: noteSchema,
  }),
};

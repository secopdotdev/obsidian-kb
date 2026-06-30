// src/lib/remark-wikilinks.mjs
//
// Minimal, dependency-free remark plugin that converts Obsidian-style
// [[target]] and [[target|alias]] wikilinks into mdast link nodes pointing at
// the /notes/<slug> routes. Resolution mirrors Obsidian's shortest-path match:
// try the full slug, then the basename. Unresolved links render with a
// `broken-link` class. Only `text` nodes are processed, so wikilinks inside
// `inlineCode`/`code` (e.g. TOML `[[repos]]`) are left untouched — by design.

const WIKILINK = /\[\[([^\]]+)\]\]/g;

/**
 * @param {{ allSlugs: string[], slugByBasename: Map<string,string> }} opts
 */
export function remarkWikilinks({ allSlugs, slugByBasename }) {
  const slugSet = new Set(allSlugs);

  function resolve(target) {
    const clean = target.split('#')[0].trim().replace(/\\/g, '/').replace(/\.md$/, '');
    if (!clean) return null;
    if (slugSet.has(clean)) return clean;
    const leaf = clean.split('/').pop();
    return slugByBasename.get(leaf) ?? null;
  }

  function splitTextValue(value) {
    /** @type {any[]} */
    const out = [];
    let last = 0;
    WIKILINK.lastIndex = 0;
    let m;
    while ((m = WIKILINK.exec(value)) !== null) {
      if (m.index > last) out.push({ type: 'text', value: value.slice(last, m.index) });
      const inner = m[1];
      const pipe = inner.indexOf('|');
      const targetRaw = pipe === -1 ? inner : inner.slice(0, pipe);
      const alias = pipe === -1 ? null : inner.slice(pipe + 1);
      const label = (alias ?? targetRaw).trim();
      const slug = resolve(targetRaw);
      if (slug) {
        out.push({
          type: 'link',
          url: `/notes/${slug}`,
          data: { hProperties: { className: 'internal-link' } },
          children: [{ type: 'text', value: label }],
        });
      } else {
        // Unresolved — render as a marked span (link to #) so it is visible but inert.
        out.push({
          type: 'link',
          url: '#',
          data: { hProperties: { className: 'broken-link', title: `unresolved: ${targetRaw.trim()}` } },
          children: [{ type: 'text', value: label }],
        });
      }
      last = m.index + m[0].length;
    }
    if (last < value.length) out.push({ type: 'text', value: value.slice(last) });
    return out;
  }

  function walk(node) {
    if (!node || !Array.isArray(node.children)) return;
    const next = [];
    for (const child of node.children) {
      if (child.type === 'text' && child.value.includes('[[')) {
        next.push(...splitTextValue(child.value));
      } else {
        // Do not descend into code; everything else can contain nested text.
        if (child.type !== 'code' && child.type !== 'inlineCode') walk(child);
        next.push(child);
      }
    }
    node.children = next;
  }

  return (tree) => walk(tree);
}

<%*
// Dispatcher: routes to _templates/cli.md for cmd- prefixed files,
// _templates/error.md for err- prefixed files.
// Applied by the 04-cli-errors/ folder-template rule.
// NOTE: validate in live Obsidian that the included sub-template's frontmatter
// lands on line 1 of the new file — the -%> trim removes trailing whitespace
// but the precise behavior is Templater-version-dependent.
const tplName = tp.file.title.startsWith("err-") ? "_templates/error" : "_templates/cli";
-%>
<% await tp.file.include("[[" + tplName + "]]") -%>

# Mission — Linter for Markdown links

A CLI that scans a directory tree for Markdown files and validates every link
in them: internal anchors, relative paths to files in the repo, and external
HTTP(S) URLs. Fails the build with a non-zero exit code when any link is
broken. Designed to drop into a CI workflow with no configuration.

## Who it's for

- Documentation maintainers on open-source repos who get tired of broken links
  silently rotting between releases.
- DevRel teams shipping docs sites where 404s in tutorials cost trust.
- Internal engineering wikis where stale links accumulate as services move.

## Success looks like

- A single static binary or a `pipx`-installable package that runs in under a
  second on a 200-file docs tree.
- Smart-enough internal-link resolution that it handles the common Hugo /
  Docusaurus / mdBook layouts without per-tool configuration.
- External-link checking that respects rate limits, caches successful HEADs
  for 24 hours, and degrades gracefully when offline (warns instead of fails).
- One-flag CI integration: `markdown-linkcheck ./docs`.
- Adoption by at least three other open-source projects in the first quarter
  after launch.

## Out of scope

- Full Markdown linting (style, grammar, formatting). That's `markdownlint`'s
  job; we don't compete.
- Auto-fix. We report; humans fix. Auto-fixing links is the territory of a
  separate tool that needs a different threat model.
- HTML output / dashboards. CLI text + a JSON dump are enough.

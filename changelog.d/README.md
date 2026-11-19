# Changelog notes

Give each change its own file so concurrent pull requests can add entries
without editing the same section of `CHANGELOG.md`. Name it
`changelog.d/<slug>.md` (any name but `README.md`) and write one or more
tagged entries:

```markdown
- [FIX] What changed and why it matters (#123)
  An indented line continues the entry above it.
```

Tags are `BREAKING`, `FEATURE`, `FIX`, `INTERNAL` and `SECURITY` (see the pull
request template). The PR number is optional.

`python scripts/changelog.py check` validates notes. Pull request CI runs it with
`--base HEAD^1` and rejects direct additions under `## Unreleased` in
`CHANGELOG.md`.

At release time, run `python scripts/changelog.py assemble`: it writes each note
into `## Unreleased` as a `- [TAG] text` line (the tag kept) and deletes the
note files. `changelog.d/sections.json` sets the tags and that flat form.

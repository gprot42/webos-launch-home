# Issues and pull requests

## Issues

The maintainer runs the issue tracker by hand and relays reporters'
messages to whatever is working on a fix. So, from an agent:

- Don't create issues.
- Don't comment on, label, assign or close issues or PRs.
- Don't post diagnostic results or questions for reporters on GitHub; give
  them to the maintainer, who passes them on.

There are no triage labels in this repo.

## Pull requests

A good PR description for this project:

1. **Changes by area**, each a few lines: what it does, what it replaces.
2. **What was tested, and where**: the TV model and webOS version, or
   "browser only". The browser (`.claude/launch.json` serves `dist/`) has
   no Luna services, no remote and no TV compositor, so it only proves
   layout and pure logic.
3. **Decisions for the maintainer**, called out rather than buried: a
   changed default, a new network source and its licensing, a changed
   convention, anything a user would notice without asking for it.
4. **Config changes**: a new setting needs a default in
   `src/js/config.js`, a migration step (bump `CONFIG_SCHEMA_VERSION`),
   and it must round-trip through backup and restore.

Keep unrelated changes in separate PRs when you can: a 23-commit PR was
merged here once, but each part had to be weighed separately.

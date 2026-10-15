# Guide for coding agents

Launch Home is a web app for rooted LG webOS TVs: a home screen with app
tiles, inputs, wallpapers and music, plus a voice assistant daemon. Most of
it can only be verified on a TV. These notes are for agents (and people)
preparing a change for a pull request.

## Before opening a PR

- `npm test` (vitest, jsdom) and `npm run build` must pass.
- Say in the PR which TV and webOS version the change was tried on, or that
  it was only run in a browser. Many problems only show on a TV: Luna
  services, the remote, the compositor, memory.
- Don't bump the version. Releases and the version files (`version.md`,
  `appinfo.json`, `voice/overlay/appinfo.json`, `package.json`) are the
  maintainer's; `scripts/set-version.js` keeps them in step at release time.
- Don't commit `dist/` or anything under `node_modules/`.

## Commits and PR descriptions

- Conventional Commits are welcome in PRs: `feat(background): …`,
  `fix(settings): …`. The body says why, not what.
- No AI attribution trailers (`Co-Authored-By: <assistant>` and the like).
- A PR description that groups the changes by area, says what was tested
  where, and flags anything the maintainer should decide (defaults, new
  network sources, licensing) is the ideal. See
  [docs/agents/issues-and-prs.md](docs/agents/issues-and-prs.md).

## The issue tracker

Agents don't create, label, comment on or close issues or PRs in this
repo. The maintainer handles the tracker by hand and relays reports to the
tools working on fixes. Put findings in your PR description instead.

## Where things live

[docs/agents/where-things-are.md](docs/agents/where-things-are.md) maps
the source files. The user-facing documentation is `README.md` and
`docs/background-sources.md`.

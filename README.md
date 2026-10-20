# Agency Workflow Kit

> **Public beta: `1.2.0-rc.1`. Not stable.** Interfaces, files and commands may change before `1.2.0`. Read [Known limitations](RELEASE-NOTES.md#known-limitations) before you use it on a project that matters.

Agency Workflow Kit is a governed working model for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview). It installs a small set of specialist agents and skills and a Front Door entry rule, plus a worktree gate, into a project. Then a plain Claude Code session in that project routes each task one of three ways:

- `DIRECT`: the session does the work itself.
- `DELEGATE`: Kit specialists do it: `agency-researcher`, `agency-coder`, `agency-resolver`, `agency-critic`.
- `DIRECTOR`: `agency-director` runs governed, multi-step execution.

The human Operator stays the authority. The Kit never merges, deploys or publishes on its own. It treats work as accepted only when an independent `agency-critic` run has passed it against the exact revision.

## Requirements

- macOS or Linux, with `git`.
- Python 3.12 or newer. The Kit uses only the standard library.
- Claude Code CLI. Version `2.1.301` is formally qualified. `2.1.302` has real-project dogfood evidence. Newer patch versions are not formally qualified yet.

## Get started

Follow the [Quick Start](QUICKSTART.md). It covers installation, first-project activation, the first workflow, update and removal.

**Start Claude Code inside the activated project, or inside the activated worktree.** A session started in another checkout does not load that project's Kit rule or hooks.

## Reporting

- Bugs and questions: open a GitHub issue. Include the Kit version, the Claude Code version (`claude --version`), your OS, and the exact command and output. Remove secrets first.
- Security problems: do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE).

# Quick Start

This guide is for Agency Workflow Kit `1.3.0-rc.2`, a public beta. In the commands below, `<project>` is the absolute path of the project you want to activate.

## 1. Requirements

| Item | Requirement |
|---|---|
| Platform | macOS: verified on real projects. Linux: the unit suite runs in CI. Windows: not supported. |
| Claude Code CLI | `2.1.301`: formally qualified. `2.1.302`: real-project dogfood evidence. `2.1.308`, `2.1.309`, `2.1.310`: observed in release certification and real-project dogfood, not formally qualified. Other newer patch versions: not formally qualified yet. Check yours with `claude --version`. |
| Python | 3.12 or newer, run as a normal (non-root) user. Standard library only. |
| git | Required. Updates refuse to run from anything but a linked git worktree of the Kit, so a source zip cannot be updated. |

## 2. Install the Kit source

**As a Claude Code plugin (simplest).** Install once per machine, pinned to the release tag:

```bash
claude plugin install agency-workflow-kit --marketplace agency-workflow-kit/agency-workflow-kit#v1.3.0-rc.2
```

Then, inside each project, start Claude Code and run `/agency-workflow-kit:activate`. It runs the preflight, activation and `--check-only` from step 4 for you, from the plugin's copy of the Kit, and stops at the first blocker. Start a new session afterwards so the Front Door rule and hooks load. To move to a newer release, replace the pin (a pinned marketplace cannot be re-pinned in place), then run `/agency-workflow-kit:activate` again in each project. Activation updates the shared `~/.claude` install in place. This route does not use the preview, authorize and update records of step 8, which need a clone:

```bash
claude plugin marketplace remove agency-workflow-kit && claude plugin install agency-workflow-kit --marketplace agency-workflow-kit/agency-workflow-kit#<new-tag>
```

To remove the Kit from a project, ask for it in that project: `/agency-workflow-kit:activate remove the Kit from this project` runs the step 9 uninstall. Then, once no project uses it, `claude plugin uninstall agency-workflow-kit@agency-workflow-kit` removes the plugin. Uninstalling the plugin does not remove the files activation wrote.

**From a clone.** Clone the repository, then add a linked worktree at the release tag. Run every Kit command from that worktree.

```bash
git clone https://github.com/agency-workflow-kit/agency-workflow-kit.git
cd agency-workflow-kit
git worktree add ../awk-1.3.0-rc.2 v1.3.0-rc.2
cd ../awk-1.3.0-rc.2
```

## 3. What the Kit writes

- **User-global, in `~/.claude`:** the five Kit agents (`agency-director`, `agency-researcher`, `agency-coder`, `agency-resolver`, `agency-critic`), the core Kit skills, their runtime, and the worktree gate hook `hooks/studio_worktree_gate.py`. Every project on the machine shares these.
- **Per project, in `<project>/.claude`:** the worktree gate hook `hooks/studio_worktree_gate.py`, the Front Door entry rule `rules/agency-workflow-kit.md`, the Front Door runtime under `runtime/`, and the worktree-isolation keys composed into `settings.json`. The Kit journals every file it installs.
- **Per project, in `<project>/_studio/project.json`:** the project manifest.
- **Per git project, in `.git/info/exclude`:** a `.abide/` line, unless git already ignores it, so tool state never dirties a worktree.
- Activation never writes your root `CLAUDE.md`. It does not install agents or core skills at project scope either.

## 4. Activate your first project

Set your project's absolute path once, so the commands below paste as they are:

```bash
PROJECT=/absolute/path/to/your/project
```

Check first. This step is read-only and exits non-zero on a blocker:

```bash
python3 studio_activation.py --kit _studio/kit --project "$PROJECT" --preflight
```

Activate. It prints `ACTIVATED` and the next command on success:

```bash
python3 studio_activation.py --kit _studio/kit --project "$PROJECT"
```

Verify. This exits 0 only when the state is `ACTIVATED`:

```bash
python3 studio_activation.py --kit _studio/kit --project "$PROJECT" --check-only
```

If any other state comes back (`NOT_ACTIVATED`, `STALE_GLOBAL_INSTALL`, `PROJECT_CONFIGURATION_MISSING`), the JSON output says what is missing. Fix that and run activation again. `--global-root` defaults to `~/.claude`.

## 5. Start Claude Code inside the activated project

**Important:** start the Claude Code session *inside* the activated project directory. If you work in a git worktree, the worktree itself must be activated, and the session must start inside it.

```bash
cd "$PROJECT"
claude
```

A session started in another checkout (a different clone, or the primary checkout of a worktree) does not load the project's `.claude/rules/agency-workflow-kit.md` or its hooks. Its native worktree isolation can also root in the wrong clone. In real-project use, a session started inside the activated worktree loaded the Front Door rule as expected; a session started in a sibling clone did not.

A linked worktree does not inherit activation from its primary checkout. Activate each worktree you work in and run `--check-only` there.

## 6. Your first workflow

In the activated session, ask for a small, real, bounded task, for example:

> Fix the security advisory in dependency X. Change only package.json and the lockfile, run the build and tests, and have the result independently reviewed.

The session is the Front Door. It does a clear task directly. To delegate, it writes a small decomposition and runs the installed router `.claude/runtime/studio_front_door.py`, which picks `DIRECT`, `DELEGATE` or `DIRECTOR`. For acceptance, a fresh `agency-critic` reviews the exact revision. You decide when to merge or deploy.

## 7. Permissions and safety model

- The Operator, you, is the only authority for merges, deploys, publication and anything irreversible. The Kit asks; it does not decide.
- Specialists have bounded roles. The Critic reviews; it does not implement. Researchers do not mutate code.
- Acceptance needs an independent Critic PASS bound to the exact revision. A self-review does not count.
- The worktree gate hook blocks specialist writes from a shared or main checkout.
- The Kit adds no tools, MCP servers or approvals beyond what you already allow in Claude Code. Your Claude Code permission settings still apply.

## 8. Update

From a linked worktree of the new release tag, update the user-global catalog first, then each project. Each update is preview → authorize → update. `authorize` prints the path of the authorization record that `update` needs.

User-global scope. `<empty-dir>` is any empty directory; it stands in for "no project":

```bash
python3 studio_update.py --kit _studio/kit --target ~/.claude --project <empty-dir> --global-root ~/.claude preview
python3 studio_update.py --kit _studio/kit --target ~/.claude --project <empty-dir> --global-root ~/.claude authorize --requested-by <your-name>
python3 studio_update.py --kit _studio/kit --target ~/.claude --project <empty-dir> --global-root ~/.claude update --authorization-file <record> --executed-by <your-name>
```

Each project: the same three commands, with `--target <project>/.claude --project <project> --global-root ~/.claude`. Then run `studio_activation.py --kit _studio/kit --project <project> --check-only`. If it is not `ACTIVATED`, run activation again (step 4).

`authorize` and `update` refuse a dirty, locked, shared or main checkout of the Kit. Use a clean linked worktree. If an update is interrupted, run `recover` with the same `--target`/`--project`.

## 9. Uninstall

Per project:

```bash
python3 studio_activation.py --project <project> --uninstall
```

This removes the Kit files that still match the journal and the gate hook entries the Kit composed. It keeps any file you modified and reports it. It keeps `_studio/project.json`, the rest of `settings.json` and `.studio-backups/`. A rerun is a no-op.

Uninstall never touches the user-global catalog, because other projects share it. Once no project uses the Kit, remove the global files by hand: `~/.claude/agents/agency-*.md`, the Kit skills under `~/.claude/skills/studio-*`, the Kit runtime under `~/.claude/runtime/`, and the worktree gate hook `~/.claude/hooks/studio_worktree_gate.py`. Then remove the Kit's own records there: the install journal `~/.claude/.studio-install.json` and, if present, `~/.claude/.studio-backups/`, `~/.claude/.studio-update-authorization/` and `~/.claude/.studio-update-evidence/`. Check each path before you delete it.

## 10. Known limitations

See [RELEASE-NOTES.md](RELEASE-NOTES.md#known-limitations).

## 11. Report a bug or a security problem

- Bugs: open a GitHub issue with the Kit version, `claude --version`, your OS, and the exact command and output. Never paste tokens, keys or private paths.
- Security: follow [SECURITY.md](SECURITY.md). Do not open a public issue.

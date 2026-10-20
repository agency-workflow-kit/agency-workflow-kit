# Security policy

## Supported versions

| Version | Supported |
|---|---|
| `1.2.0-rc.1` (public beta) | Yes |
| Anything older | No |

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting. Open this repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, discussion or pull request for a suspected vulnerability.

Include the affected version, your OS, `claude --version`, steps to reproduce, and the impact you observed. Never include real credentials, tokens or private data. If a secret was exposed, revoke it first, then report.

You will get an acknowledgement in the advisory thread. Fixes for confirmed issues ship in a new release, and the advisory is published once the fix is out.

## Scope

In scope: the Kit's installer, activation, update and uninstall commands; the worktree gate hook; the Front Door runtime; and the rendered agents and skills.

Out of scope: Claude Code itself, Anthropic services and third-party plugins. Report those to their maintainers.

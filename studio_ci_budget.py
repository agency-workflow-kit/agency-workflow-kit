#!/usr/bin/env python3
"""CI-minutes budget for Kit-driven runs (#223).

GitHub Actions minutes are spent only after local evidence is green:

- ``run_local_checks`` runs the project's lint, typecheck, test and build
  (``_studio/project.json`` ``commands``, else package.json scripts);
- ``ship`` refuses uncommitted changes, empty commits and a HEAD that is
  already pushed, reruns the local checks, then pushes once and opens the
  pull request as a draft;
- ``mark_ready`` marks it ready only for passing local evidence of the
  pushed HEAD;
- ``classify_checks`` reads the remote checks; a required check that never
  started or failed with no run behind it (billing, quota, runner
  unavailable) is an ``infrastructure_block``, and ``gate_report`` turns
  that into a gate report with the local evidence and no further push;
- ``ci_risks`` lists CI-minute risks in the project's workflows (report
  only, never edits);
- ``render_templates`` renders the CI workflow and pre-push hook templates
  for a project. A normal Kit task run applies them, as a pull request;
  activation never does.

Shipped into every activated project's ``.claude/runtime/`` with its
templates (studio_renderer.FRONT_DOOR_RUNTIME_MODULES) because the
project's own session runs it. Standard library only; it imports no other
Kit module. The workflow scan is a line-based reading of common GitHub
Actions layouts, not a YAML parser; it reports, it never decides a merge.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

CHECK_ORDER = ("lint", "typecheck", "test", "build")
MODES = ("strict", "normal")
DEFAULT_MODE = "strict"
CI_BUDGET_FIELDS = ("mode", "prBuildHost", "requiredChecks")
TEMPLATE_DIR = Path(__file__).resolve().parent / "_studio" / "kit" / "templates" / "ci"
WORKFLOW_TEMPLATE = "github-actions-ci.yml"
HOOK_TEMPLATE = "pre-push"
WORKFLOW_RELATIVE = ".github/workflows/ci.yml"
RISK_IDS = (
    "push-on-feature-branches",
    "no-concurrency-cancel",
    "no-draft-skip",
    "no-dependency-cache",
    "build-duplicates-pr-host",
)
GH_CHECK_FIELDS = "name,state,bucket,description,startedAt,completedAt,link,workflow"

_HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SCRIPT_NAMES = {
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "check-types", "tsc"),
    "test": ("test",),
    "build": ("build",),
}
_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"), ("bun.lock", "bun"), ("package-lock.json", "npm"),
)
_HOST_MARKERS = (("vercel.json", "vercel"), (".vercel/project.json", "vercel"), ("netlify.toml", "netlify"))
_PROTECTED_BRANCHES = {"main", "master", "trunk", "develop", "production", "staging"}
_INFRA_TEXT = re.compile(
    r"spending limit|payments? (?:have )?failed|billing|quota|not started|"
    r"no (?:hosted )?runners?|could not find any runners|runners? (?:is |are )?(?:unavailable|not available)",
    re.IGNORECASE,
)
_NEVER_STARTED = ("", "0001-01-01T00:00:00Z")
_ACTIONS_JOB_LINK = re.compile(r"github\.com/([^/]+/[^/]+)/actions/runs/\d+/job/(\d+)")
_INSTALL = re.compile(r"\b(?:npm (?:ci|install|i)\b|pnpm (?:install|i)\b|yarn install\b|yarn\s*$|bun install\b)", re.M)
_CACHE = re.compile(r"^\s*cache:\s*\S|actions/cache@|cache-dependency-path", re.M)
_BUILD = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b|\b(?:next|vite|astro|nuxt|turbo)\s+(?:run\s+)?build\b")
_RUN_SCRIPT = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+run\s+([A-Za-z0-9:_.-]+)")
_PLAIN_COMMAND = re.compile(r"^[A-Za-z0-9_./:@=+-]+(?: [A-Za-z0-9_./:@=+-]+)*$")
_ON_KEY = re.compile(r"""^(?:on|"on"|'on'|true)\s*:(.*)$""")
_IF_CODE = "        if: steps.changes.outputs.code == 'true'\n"

NEXT_ACTIONS = {
    "passed": "none: remote checks passed",
    "failed": "reproduce the failure locally, fix it, rerun the local checks, then make one new push",
    "pending": "wait for the running checks; do not push",
    "infrastructure_block": "do not push again; report the block at the gate with the local evidence",
}

Runner = Callable[[list[str], Path], subprocess.CompletedProcess]


class CiBudgetError(RuntimeError):
    pass


def default_runner(argv, cwd) -> subprocess.CompletedProcess:
    # GIT_*-stripped, like studio_worktree_gate: an inherited GIT_DIR would
    # retarget every git answer to another checkout.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True, env=env)
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def validate_ci_budget(value, where: str = "project.ciBudget") -> list[str]:
    """Contract for the optional ``ciBudget`` block of _studio/project.json.

    Names only: a mode, a host name and check names. No field can hold a
    URL, token or other secret."""
    if not isinstance(value, dict):
        return [f"{where} must be an object"]
    errors = [f"{where}.{name} is required" for name in CI_BUDGET_FIELDS if name not in value]
    errors += [f"{where}.{name} is not a ciBudget field" for name in sorted(set(value) - set(CI_BUDGET_FIELDS))]
    if "mode" in value and value["mode"] not in MODES:
        errors.append(f"{where}.mode must be one of {list(MODES)}")
    host = value.get("prBuildHost")
    if host is not None and (not isinstance(host, str) or not _HOST_NAME.match(host)):
        errors.append(f"{where}.prBuildHost must be null or a lowercase host name such as 'vercel'")
    if "requiredChecks" in value:
        checks = value["requiredChecks"]
        if not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks):
            errors.append(f"{where}.requiredChecks must be a list of non-empty check names")
        elif len(set(checks)) != len(checks):
            errors.append(f"{where}.requiredChecks must not repeat a name")
    return errors


# --- configuration ---------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _project_manifest(root: Path) -> dict | None:
    path = root / "_studio" / "project.json"
    if not path.is_file():
        return None
    data = _read_json(path)
    if not isinstance(data, dict):
        raise CiBudgetError("_studio/project.json is not valid JSON")
    errors = validate_ci_budget(data["ciBudget"]) if "ciBudget" in data else []
    commands = data.get("commands")
    if commands is None:
        commands = {}
    elif not isinstance(commands, dict):
        errors.append("project.commands must be an object")
        commands = {}
    for name in CHECK_ORDER + ("install",):
        cmd = commands.get(name) if isinstance(commands, dict) else None
        if cmd is not None and not (
            isinstance(cmd, dict) and isinstance(cmd.get("executable"), str) and cmd["executable"]
            and isinstance(cmd.get("args"), list) and all(isinstance(a, str) for a in cmd["args"])
        ):
            errors.append(f"project.commands.{name} must be null or {{executable, args}}")
    if errors:
        raise CiBudgetError("_studio/project.json is invalid: " + "; ".join(errors))
    return data


def _package_json(root: Path) -> dict | None:
    data = _read_json(root / "package.json")
    return data if isinstance(data, dict) else None


def _package_manager(root: Path, pkg: dict | None) -> str | None:
    if pkg is None:
        return None
    declared = pkg.get("packageManager")
    if isinstance(declared, str) and declared.strip():
        return declared.split("@", 1)[0].strip()
    for name, manager in _LOCKFILES:
        if (root / name).is_file():
            return manager
    return "npm"


def _script_commands(pkg: dict | None, manager: str | None) -> dict:
    scripts = pkg.get("scripts") if pkg else None
    scripts = scripts if isinstance(scripts, dict) else {}
    commands = {}
    for check, names in _SCRIPT_NAMES.items():
        script = next((n for n in names if isinstance(scripts.get(n), str) and scripts[n].strip()), None)
        if script == "test" and "no test specified" in scripts["test"]:
            script = None  # npm init's placeholder always fails
        commands[check] = [manager, "run", script] if script and manager else None
    return commands


def _pr_build_host(root: Path) -> str | None:
    return next((host for marker, host in _HOST_MARKERS if (root / marker).is_file()), None)


def _workflow_files(root: Path) -> list[Path]:
    directory = root / ".github" / "workflows"
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml"))


def _strip_comment(text: str) -> str:
    return re.sub(r"\s+#.*$", "", text)


def _top_block(lines: list[str], start: int) -> list[str]:
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line[0].isspace() and not line.lstrip().startswith("#"):
            break
        block.append(line)
    return block


def _children(block: list[str]) -> dict[str, list[str]]:
    """Map each key at the block's first indentation to the lines below it
    (the text after its colon first, when there is any)."""
    children: dict[str, list[str]] = {}
    current, indent = None, None
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        depth = len(line) - len(line.lstrip())
        if indent is None:
            indent = depth
        if depth <= indent:
            body = stripped[2:] if stripped.startswith("- ") else stripped
            key, _, rest = body.partition(":")
            current = key.strip().strip("'\"")
            children[current] = [rest] if _strip_comment(rest).strip() else []
        elif current is not None:
            children[current].append(line)
    return children


def _values(lines: list[str], key: str) -> list[str] | None:
    """A list-valued key (``[a, b]``, ``- a`` lines or a scalar); None when absent."""
    children = _children(lines)
    if key not in children:
        return None
    values = []
    for line in children[key]:
        text = _strip_comment(line).strip()
        if text.startswith("["):
            values += [v.strip().strip("'\"") for v in text.strip("[]").split(",") if v.strip()]
        elif text.startswith("- "):
            values.append(text[2:].strip().strip("'\""))
        elif text:
            values.append(text.strip("'\""))
    return values


def workflow_events(text: str) -> dict[str, list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ON_KEY.match(line)
        if not match:
            continue
        inline = _strip_comment(match.group(1)).strip()
        if inline:
            return {name.strip().strip("'\""): [] for name in inline.strip("[]").split(",") if name.strip()}
        return _children(_top_block(lines, index))
    return {}


def _job_check_names(text: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^jobs\s*:\s*$", line):
            names = []
            for job_id, body in _children(_top_block(lines, index)).items():
                declared = _values(body, "name")
                name = declared[0] if declared else ""
                names.append(name if name and "${{" not in name else job_id)
            return names
    return []


def _is_pull_request_workflow(events: dict) -> bool:
    return "pull_request" in events or "pull_request_target" in events


def _detected_required_checks(root: Path) -> list[str]:
    names: list[str] = []
    for path in _workflow_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _is_pull_request_workflow(workflow_events(text)):
            names += [n for n in _job_check_names(text) if n not in names]
    return names


def resolve_config(project_root) -> dict:
    """Effective CI budget: ``_studio/project.json`` wins, detection fills gaps.

    Detection is never written back; the project file stays user-owned."""
    root = Path(project_root)
    manifest = _project_manifest(root)
    pkg = _package_json(root)
    manager = _package_manager(root, pkg)
    scripts = _script_commands(pkg, manager)
    configured = (manifest or {}).get("commands") or {}
    commands, command_source = {}, {}
    for name in CHECK_ORDER:
        cmd = configured.get(name)
        if cmd:
            commands[name], command_source[name] = [cmd["executable"], *cmd["args"]], "project"
        else:
            commands[name] = scripts[name]
            command_source[name] = "package.json" if scripts[name] else None
    install = configured.get("install")
    budget = (manifest or {}).get("ciBudget")
    if budget:
        mode, host, required, source = budget["mode"], budget["prBuildHost"], list(budget["requiredChecks"]), "project"
    else:
        mode, host, required, source = DEFAULT_MODE, _pr_build_host(root), _detected_required_checks(root), "detected"
    return {
        "mode": mode,
        "prBuildHost": host,
        "requiredChecks": required,
        "packageManager": manager,
        "commands": commands,
        "install": [install["executable"], *install["args"]] if install else None,
        "source": {"ciBudget": source, "commands": command_source},
    }


# --- local checks and the one push -----------------------------------------

def _out(runner: Runner, root: Path, *args: str) -> str:
    result = runner(["git", *args], root)
    if result.returncode != 0:
        raise CiBudgetError(f"git {' '.join(args)} failed: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def run_local_checks(project_root, config: dict | None = None, runner: Runner | None = None) -> dict:
    """Run every configured check in order and bind the evidence to HEAD.

    ``passed`` needs at least one check, every check green and, in strict
    mode, no missing check."""
    root = Path(project_root)
    runner = runner or default_runner
    config = config or resolve_config(root)
    head = _out(runner, root, "rev-parse", "HEAD")
    missing = [name for name in CHECK_ORDER if not config["commands"][name]]
    checks = []
    for name in CHECK_ORDER:
        argv = config["commands"][name]
        if not argv:
            continue
        result = runner(list(argv), root)
        entry = {"name": name, "argv": list(argv), "exit_code": result.returncode, "passed": result.returncode == 0}
        if result.returncode != 0:
            entry["output_tail"] = "\n".join(((result.stdout or "") + (result.stderr or "")).splitlines()[-20:])
        checks.append(entry)
    passed = bool(checks) and all(c["passed"] for c in checks) and not (config["mode"] == "strict" and missing)
    return {"head": head, "mode": config["mode"], "passed": passed, "checks": checks, "missing": missing}


def _blocked(reason: str, local: dict | None = None) -> dict:
    return {"state": "BLOCKED", "reason": reason, "pushes": 0, "local_checks": local}


def _upstream(runner: Runner, root: Path) -> str | None:
    result = runner(["git", "rev-parse", "--verify", "--quiet", "@{u}"], root)
    return result.stdout.strip() if result.returncode == 0 else None


def ship(project_root, *, base: str, title: str, body: str,
         runner: Runner | None = None, config: dict | None = None) -> dict:
    """Local checks green, then exactly one push and a draft pull request."""
    root = Path(project_root)
    runner = runner or default_runner
    branch = _out(runner, root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch in ("HEAD", base):
        return _blocked(f"push from a task branch, not {branch!r}")
    if _out(runner, root, "status", "--porcelain"):
        return _blocked("uncommitted changes: commit them first so the local evidence matches what is pushed")
    head = _out(runner, root, "rev-parse", "HEAD")
    if _upstream(runner, root) == head:
        return _blocked(f"{head[:12]} is already pushed; nothing new to push (no retrigger pushes)")
    remote_base = runner(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}"], root)
    base_ref = f"origin/{base}" if remote_base.returncode == 0 else base
    log = _out(runner, root, "log", "--format=%H %T %P", f"{base_ref}..HEAD")
    if not log:
        return _blocked(f"no commits ahead of {base_ref}")
    for line in log.splitlines():
        sha, tree, *parents = line.split()
        if parents and _out(runner, root, "rev-parse", f"{parents[0]}^{{tree}}") == tree:
            return _blocked(f"empty commit {sha[:12]}: drop it; empty or retrigger commits are never pushed")

    local = run_local_checks(root, config, runner)
    if not local["passed"]:
        return _blocked("local checks are not green; fix them before any push", local)
    if _out(runner, root, "status", "--porcelain"):
        return _blocked("the local checks changed files: commit the changes and ship again", local)

    pushed = runner(["git", "push", "-u", "origin", "HEAD"], root)
    if pushed.returncode != 0:
        return _blocked(f"git push failed: {(pushed.stderr or '').strip()}", local)
    result = {"state": "PUSHED", "head": head, "branch": branch, "pushes": 1, "local_checks": local}
    existing = runner(["gh", "pr", "view", "--json", "url,isDraft"], root)
    if existing.returncode == 0:
        info = json.loads(existing.stdout or "{}")
        result["pr"] = {"url": info.get("url"), "draft": info.get("isDraft"), "created": False}
        return result
    created = runner(["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
                      "--title", title, "--body", body], root)
    if created.returncode != 0:
        result["pr"] = None
        result["reason"] = f"pushed, but the draft pull request was not created: {(created.stderr or '').strip()}"
        return result
    lines = (created.stdout or "").strip().splitlines()
    result["pr"] = {"url": lines[-1] if lines else None, "draft": True, "created": True}
    return result


def mark_ready(project_root, *, evidence: dict | None, runner: Runner | None = None) -> dict:
    """``gh pr ready`` only for passing local evidence of the pushed HEAD."""
    root = Path(project_root)
    runner = runner or default_runner
    head = _out(runner, root, "rev-parse", "HEAD")
    if not evidence or not evidence.get("passed"):
        return _blocked("local evidence is missing or not green")
    if evidence.get("head") != head:
        return _blocked(f"local evidence is for {str(evidence.get('head'))[:12]}, HEAD is {head[:12]}: rerun the checks")
    if _out(runner, root, "status", "--porcelain"):
        return _blocked("uncommitted changes are not covered by the evidence")
    if _upstream(runner, root) != head:
        return _blocked("HEAD is not pushed yet: ship it first")
    ready = runner(["gh", "pr", "ready"], root)
    if ready.returncode != 0:
        return _blocked(f"gh pr ready failed: {(ready.stderr or '').strip()}")
    return {"state": "READY", "head": head}


# --- remote checks ---------------------------------------------------------

def _check_result(check: dict) -> str:
    bucket = str(check.get("bucket") or "").lower()
    state = str(check.get("state") or "").upper()
    if state in ("STARTUP_FAILURE", "STALE"):
        return "infrastructure"
    if bucket in ("pass", "skipping") or state in ("SUCCESS", "SKIPPED", "NEUTRAL"):
        return "passed"
    if bucket == "cancel" or state == "CANCELLED":
        return "pending"  # not a result: a newer run on the same pull request replaces it
    if bucket == "fail" or state in ("FAILURE", "ERROR", "TIMED_OUT"):
        started = str(check.get("startedAt") or "")
        completed = str(check.get("completedAt") or "")
        if _INFRA_TEXT.search(str(check.get("description") or "")) or started in _NEVER_STARTED or started == completed:
            return "infrastructure"
        return "failed"
    return "pending"


def classify_checks(checks: list[dict], required: list[str], *, settled: bool = False) -> dict:
    """Classify ``gh pr checks --json`` output.

    A required check that is absent is pending until ``settled`` (its start
    window has passed, see ``fetch_remote_checks``); after that it never
    started. A failed check whose run has no duration, a start-up failure
    or a billing/quota/runner message is infrastructure, not a code
    failure. A cancelled check is pending: a newer run replaces it."""
    results = [{"name": c.get("name"), "result": _check_result(c), "description": c.get("description") or ""}
               for c in checks]
    reported = {r["name"] for r in results}
    results += [{"name": name, "result": "infrastructure" if settled else "pending",
                 "description": "never started" if settled else "not reported yet"}
                for name in required if name not in reported]
    seen = {r["result"] for r in results}
    if "failed" in seen:
        verdict = "failed"
    elif "infrastructure" in seen:
        verdict = "infrastructure_block"
    elif "pending" in seen:
        verdict = "pending"
    else:
        verdict = "passed"
    return {"verdict": verdict, "checks": results, "next_action": NEXT_ACTIONS[verdict]}


def gate_report(classification: dict, local_evidence: dict | None) -> dict:
    """What the gate shows. Only a real failure ever leads to another push,
    and only after a local fix."""
    verdict = classification["verdict"]
    state = {
        "passed": "REMOTE_PASSED", "failed": "REMOTE_FAILED",
        "pending": "REMOTE_PENDING", "infrastructure_block": "INFRASTRUCTURE_BLOCK",
    }[verdict]
    report = {
        "state": state,
        "push_allowed": verdict == "failed",
        "next_action": classification["next_action"],
        "blocked_checks": [c for c in classification["checks"] if c["result"] == "infrastructure"],
        "local_evidence": local_evidence,
    }
    if verdict == "infrastructure_block":
        report["message"] = (
            "Required remote checks did not run (billing, quota or runner unavailable). "
            "No further push: the local evidence is reported for the Operator's decision at the gate."
        )
    return report


def _gh_checks(root: Path, runner: Runner) -> list[dict]:
    result = runner(["gh", "pr", "checks", "--json", GH_CHECK_FIELDS], root)
    # gh exits non-zero while checks fail or are pending; the JSON is still printed.
    if not (result.stdout or "").strip():
        if "no checks reported" in (result.stderr or ""):
            return []
        raise CiBudgetError(f"gh pr checks failed: {(result.stderr or '').strip()}")
    return json.loads(result.stdout)


def _with_annotations(checks: list[dict], root: Path, runner: Runner) -> list[dict]:
    """A job GitHub refused to start (billing, spending limit) reports
    FAILURE with an empty description; the reason is in its check-run
    annotations. Append them to the description of each failed Actions job."""
    annotated = []
    for check in checks:
        match = _ACTIONS_JOB_LINK.search(str(check.get("link") or ""))
        if match and str(check.get("bucket") or "").lower() == "fail":
            result = runner(["gh", "api", f"repos/{match.group(1)}/check-runs/{match.group(2)}/annotations",
                             "--jq", ".[].message"], root)
            if result.returncode == 0 and (result.stdout or "").strip():
                description = " ".join(filter(None, [str(check.get("description") or ""), result.stdout.strip()]))
                check = {**check, "description": description}
        annotated.append(check)
    return annotated


def fetch_remote_checks(project_root, required: list[str], *, wait_seconds: int = 300, poll_seconds: int = 15,
                        runner: Runner | None = None, sleep: Callable[[float], None] | None = None,
                        clock: Callable[[], float] | None = None) -> tuple[list[dict], bool]:
    """Poll until every required check is reported or ``wait_seconds`` pass.

    Returns ``(checks, settled)``. Settled means each required check either
    appeared or stayed absent for the whole start window, so an absent one
    really never started."""
    import time
    runner = runner or default_runner
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    root = Path(project_root)
    deadline = clock() + wait_seconds
    while True:
        checks = _gh_checks(root, runner)
        reported = {c.get("name") for c in checks}
        if all(name in reported for name in required) or clock() >= deadline:
            return _with_annotations(checks, root, runner), True
        sleep(poll_seconds)


# --- health check ----------------------------------------------------------

def _push_reaches_feature_branches(push: list[str]) -> bool:
    if _values(push, "branches-ignore") is not None:
        return True
    branches = _values(push, "branches")
    if branches is None:
        return _values(push, "tags") is None
    return any(b not in _PROTECTED_BRANCHES and not b.startswith("release") for b in branches)


def _script_bodies(text: str, scripts: dict) -> str:
    """The package.json scripts a workflow runs, followed transitively."""
    pending, seen, bodies = _RUN_SCRIPT.findall(text), set(), []
    while pending:
        name = pending.pop()
        body = scripts.get(name)
        if name in seen or not isinstance(body, str):
            continue
        seen.add(name)
        bodies.append(body)
        pending += _RUN_SCRIPT.findall(body)
    return "\n".join(bodies)


def ci_risks(project_root, config: dict | None = None) -> list[dict]:
    """CI-minute risks in .github/workflows. Reads only; never edits."""
    root = Path(project_root)
    config = config or resolve_config(root)
    pkg = _package_json(root) or {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    risks = []
    for path in _workflow_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        events = workflow_events(text)
        on_pr = _is_pull_request_workflow(events)
        if "push" not in events and not on_pr:
            continue
        rel = path.relative_to(root).as_posix()

        def add(risk: str, detail: str) -> None:
            risks.append({"workflow": rel, "risk": risk, "detail": detail})

        if "push" in events and _push_reaches_feature_branches(events["push"]):
            add("push-on-feature-branches", "runs on pushes to feature branches; the pull request run already covers them")
        if not re.search(r"cancel-in-progress:\s*(?:true|\$\{\{)", text):
            add("no-concurrency-cancel", "no concurrency group with cancel-in-progress; superseded runs keep spending minutes")
        if on_pr and "github.event.pull_request.draft" not in text:
            add("no-draft-skip", "draft pull requests run the full workflow")
        if _INSTALL.search(text) and not _CACHE.search(text):
            add("no-dependency-cache", "installs dependencies without a package-manager cache")
        if config["prBuildHost"] and on_pr and _BUILD.search(text + "\n" + _script_bodies(text, scripts)):
            add("build-duplicates-pr-host", f"builds pull requests that {config['prBuildHost']} already builds")
    return risks


# --- templates -------------------------------------------------------------

def _run_line(argv: list[str], indent: str = "        ") -> str:
    command = shlex.join(argv)
    if _PLAIN_COMMAND.match(command):
        return f"{indent}run: {command}\n"
    return f"{indent}run: |\n{indent}  {command}\n"


def _node_setup(root: Path, pkg: dict, manager: str) -> str:
    lines = ""
    if manager == "pnpm":
        if not str(pkg.get("packageManager") or "").startswith("pnpm@"):
            raise CiBudgetError('pnpm needs "packageManager": "pnpm@<version>" in package.json so CI uses the same pnpm')
        lines += "      - uses: pnpm/action-setup@v4\n" + _IF_CODE
    lines += "      - uses: actions/setup-node@v4\n" + _IF_CODE + "        with:\n"
    version_file = next((n for n in (".nvmrc", ".node-version") if (root / n).is_file()), None)
    engines = pkg.get("engines") if isinstance(pkg.get("engines"), dict) else {}
    if version_file:
        lines += f"          node-version-file: {version_file}\n"
    elif isinstance(engines.get("node"), str) and engines["node"].strip():
        lines += f"          node-version: {json.dumps(engines['node'].strip())}\n"
    else:
        lines += "          node-version: lts/*\n"
    return lines + f"          cache: {manager}\n"


def _install_command(root: Path, manager: str, config: dict) -> list[str]:
    if config.get("install"):
        return config["install"]
    if manager == "npm":
        return ["npm", "ci"]
    if manager == "pnpm":
        return ["pnpm", "install", "--frozen-lockfile"]
    return ["yarn", "install", "--immutable" if (root / ".yarnrc.yml").is_file() else "--frozen-lockfile"]


def _hook_target(root: Path, pkg: dict) -> tuple[str, str]:
    deps = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    if "husky" in deps or (root / ".husky").is_dir():
        return ".husky/pre-push", "husky runs .husky/pre-push; commit it"
    if "simple-git-hooks" in deps or "simple-git-hooks" in pkg:
        return ("scripts/pre-push.sh",
                'add "pre-push": "sh scripts/pre-push.sh" to the "simple-git-hooks" block in package.json, '
                "then run the project's simple-git-hooks install")
    return ".githooks/pre-push", "enable it with: git config core.hooksPath .githooks"


def render_templates(project_root, out_dir, config: dict | None = None) -> list[dict]:
    """Render the workflow and pre-push hook for this project into out_dir.

    Never overwrites a file. Applying the result to a project is a normal
    task run's change, reviewed as a pull request."""
    root = Path(project_root)
    out = Path(out_dir)
    config = config or resolve_config(root)
    pkg = _package_json(root)
    manager = config["packageManager"]
    if pkg is None or manager not in ("npm", "pnpm", "yarn"):
        raise CiBudgetError(
            f"rendering supports npm, pnpm and yarn projects; found {manager!r}. "
            f"Adapt {TEMPLATE_DIR / WORKFLOW_TEMPLATE} by hand.")
    missing = [name for name in CHECK_ORDER if not config["commands"][name]]
    if config["mode"] == "strict" and missing:
        raise CiBudgetError(
            f"strict mode needs every local check; missing {missing}. Set them in _studio/project.json "
            "commands, or set ciBudget.mode to normal.")
    checks = [(name, config["commands"][name]) for name in CHECK_ORDER if config["commands"][name]]
    if not checks:
        raise CiBudgetError("no local check command is configured")

    steps = _node_setup(root, pkg, manager)
    steps += "      - name: Install dependencies\n" + _IF_CODE + _run_line(_install_command(root, manager, config))
    for name, argv in checks:
        if name == "build" and config["prBuildHost"]:
            continue  # the host already builds every pull request
        steps += f"      - name: {name}\n" + _IF_CODE + _run_line(argv)
    workflow = (TEMPLATE_DIR / WORKFLOW_TEMPLATE).read_text(encoding="utf-8").replace("      # __KIT_STEPS__\n", steps)
    hook = (TEMPLATE_DIR / HOOK_TEMPLATE).read_text(encoding="utf-8").replace(
        "# __KIT_CHECKS__\n", "".join(shlex.join(argv) + "\n" for _, argv in checks))
    hook_rel, hook_note = _hook_target(root, pkg)
    files = [
        ("workflow", WORKFLOW_RELATIVE, workflow,
         f"required check name: verify; build left to {config['prBuildHost']}" if config["prBuildHost"]
         else "required check name: verify"),
        ("pre-push", hook_rel, hook, hook_note),
    ]
    existing = [rel for _, rel, _, _ in files if (out / rel).exists()]
    if existing:
        raise CiBudgetError(f"refusing to overwrite {existing}; move them aside in the task run first")
    created = []
    for kind, rel, text, note in files:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        if kind == "pre-push":
            target.chmod(0o755)
        created.append({"kind": kind, "path": str(target), "instructions": note})
    return created


# --- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", type=Path, default=Path("."))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--config", action="store_true", help="print the effective CI budget")
    action.add_argument("--local-checks", action="store_true", help="run the local checks; exit 1 unless green")
    action.add_argument("--ship", action="store_true", help="local checks, one push, draft pull request")
    action.add_argument("--ready", action="store_true", help="mark the pull request ready (needs --evidence)")
    action.add_argument("--classify-checks", nargs="?", const="gh", metavar="FILE",
                        help="classify remote checks: fetch with gh (waiting --wait-seconds for required checks "
                             "to appear), or read gh pr checks JSON from FILE or - for stdin")
    action.add_argument("--risks", action="store_true", help="report CI-minute risks; never edits")
    action.add_argument("--render-templates", type=Path, metavar="DIR", help="render the CI templates into DIR")
    parser.add_argument("--base", help="base branch for --ship")
    parser.add_argument("--title", help="pull request title for --ship")
    parser.add_argument("--body-file", type=Path, help="pull request body for --ship")
    parser.add_argument("--evidence", type=Path, help="local evidence JSON (from --local-checks or --ship)")
    parser.add_argument("--wait-seconds", type=int, default=300,
                        help="--classify-checks with gh: how long a required check may take to appear (default 300)")
    parser.add_argument("--settled", action="store_true",
                        help="--classify-checks FILE: the snapshot was taken after the start window passed")
    args = parser.parse_args(argv)
    root = args.project

    try:
        evidence = None
        if args.evidence:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            evidence = evidence.get("local_checks", evidence) if isinstance(evidence, dict) else None
        if args.config:
            result, ok = resolve_config(root), True
        elif args.local_checks:
            result = run_local_checks(root)
            ok = result["passed"]
        elif args.ship:
            if not (args.base and args.title and args.body_file):
                parser.error("--ship needs --base, --title and --body-file")
            result = ship(root, base=args.base, title=args.title, body=args.body_file.read_text(encoding="utf-8"))
            ok = result["state"] == "PUSHED" and result.get("pr") is not None
        elif args.ready:
            result = mark_ready(root, evidence=evidence)
            ok = result["state"] == "READY"
        elif args.classify_checks:
            source = args.classify_checks
            required = resolve_config(root)["requiredChecks"]
            if source == "gh":
                checks, settled = fetch_remote_checks(root, required, wait_seconds=args.wait_seconds)
            else:
                checks = json.loads(sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8"))
                settled = args.settled
            classification = classify_checks(checks, required, settled=settled)
            result = {**classification, "gate": gate_report(classification, evidence)}
            ok = classification["verdict"] == "passed"
        elif args.risks:
            result, ok = {"risks": ci_risks(root)}, True
        else:
            result, ok = {"created": render_templates(root, args.render_templates)}, True
    except (CiBudgetError, OSError, ValueError) as exc:
        print(f"ci budget error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

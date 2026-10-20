#!/usr/bin/env python3
"""Transactional installer for rendered Studio OS Claude Code assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from studio_renderer import RenderError, render_kit


class InstallError(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write(path: Path, data: bytes) -> None:
    # #130: identical bytes already on disk -> no replace, so inode/mtime stay stable.
    if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _is_rel_path_confined(rel: str) -> bool:
    """True when ``rel`` is a non-empty relative path with no ``..`` escape."""
    if not isinstance(rel, str) or not rel:
        return False
    if rel.startswith("/") or "\\" in rel:
        return False
    parts = Path(rel).parts
    return bool(parts) and ".." not in parts


class StudioInstaller:
    """Install only rendered Studio-managed agents/skills into one Claude root."""

    JOURNAL = ".studio-install.json"
    TRANSACTION = ".studio-transaction.json"
    BACKUP_ROOT = ".studio-backups"

    def __init__(self, rendered_dir: Path, target_dir: Path):
        self.rendered_dir = Path(rendered_dir)
        self.target_dir = Path(target_dir)
        self.omitted_rels: set[str] = set()
        if not self.rendered_dir.is_dir():
            raise InstallError(f"rendered directory does not exist: {self.rendered_dir}")

    @property
    def journal_path(self) -> Path:
        return self.target_dir / self.JOURNAL

    @property
    def transaction_path(self) -> Path:
        return self.target_dir / self.TRANSACTION

    def _target_root(self) -> Path:
        return self.target_dir.resolve()

    def _confined_target_path(self, rel: str) -> Path:
        """Resolve ``rel`` under the install target; reject ``../`` and symlink-parent escape."""
        if not _is_rel_path_confined(rel):
            raise InstallError(f"escaping or invalid managed path: {rel!r}")
        root = self._target_root()
        current = self.target_dir
        parts = Path(rel).parts
        for index, part in enumerate(parts):
            current = current / part
            if index == len(parts) - 1:
                break
            if current.exists() or current.is_symlink():
                try:
                    resolved = current.resolve()
                except OSError as exc:
                    raise InstallError(f"cannot resolve managed path parent: {rel}") from exc
                if resolved != root and not resolved.is_relative_to(root):
                    raise InstallError(f"symlink-parent escape: {rel}")
        parent = current.parent
        if parent.exists() or parent.is_symlink():
            try:
                resolved_parent = parent.resolve()
            except OSError as exc:
                raise InstallError(f"cannot resolve managed path parent: {rel}") from exc
            if resolved_parent != root and not resolved_parent.is_relative_to(root):
                raise InstallError(f"symlink-parent escape: {rel}")
        return current

    def _sources(self) -> dict[str, Path]:
        sources = {}
        for root in ("agents", "skills", "hooks", "rules", "runtime"):
            # ADR 0008: the entry rule is project-scope only; the user-global
            # root would load it for every project on the machine.
            if root == "rules" and self._is_user_scope_target():
                continue
            base = self.rendered_dir / root
            if not base.is_dir():
                continue
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                rel = path.relative_to(self.rendered_dir).as_posix()
                if not _is_rel_path_confined(rel):
                    raise InstallError(f"escaping or invalid rendered source path: {rel!r}")
                sources[rel] = path
        return sources

    def _load_journal(self) -> dict:
        if not self.journal_path.is_file():
            return {"schema_version": 1, "managed": {}}
        try:
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"invalid install journal: {exc}") from exc
        if data.get("schema_version") != 1 or not isinstance(data.get("managed"), dict):
            raise InstallError("unsupported install journal")
        return data

    def _write_journal(self, journal: dict) -> None:
        data = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(self.journal_path, data)

    def _write_transaction(self, transaction: dict) -> None:
        data = (json.dumps(transaction, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(self.transaction_path, data)

    def _recover_interrupted_transaction(self) -> None:
        if not self.transaction_path.is_file():
            return
        try:
            transaction = json.loads(self.transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"invalid transaction journal: {exc}") from exc
        if transaction.get("schema_version") != 1:
            raise InstallError("unsupported transaction journal")

        for entry in reversed(transaction.get("changes", [])):
            rel = entry["path"]
            target = self._confined_target_path(rel)
            backup_rel = entry.get("backup")
            if backup_rel:
                if not _is_rel_path_confined(backup_rel):
                    raise InstallError(f"escaping recovery backup path: {backup_rel!r}")
                backup = self._confined_target_path(backup_rel)
                if not backup.is_file():
                    raise InstallError(f"transaction recovery backup missing: {backup_rel}")
                _atomic_write(target, backup.read_bytes())
            elif target.exists() and target.is_file():
                target.unlink()
                # A newly created file has no backup. Drop empty parents so a
                # skill directory does not survive and change the next plan.
                if not backup_rel:
                    self._prune_empty_parents(target)

        original_journal = transaction.get("original_journal", {"schema_version": 1, "managed": {}})
        if original_journal.get("managed"):
            for rel in original_journal["managed"]:
                if not _is_rel_path_confined(rel):
                    raise InstallError(f"escaping path in transaction journal: {rel!r}")
            self._write_journal(original_journal)
        elif self.journal_path.exists():
            self.journal_path.unlink()
        self.transaction_path.unlink()

    def _prune_empty_parents(self, target: Path) -> None:
        """Remove now-empty directories between ``target`` and the install root."""
        parent = target.parent
        while parent != self.target_dir:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def plan(self, *, recover_interrupted: bool = True) -> dict:
        """Compute the install plan.

        By default recovers an interrupted transaction first (apply/update
        path). ``recover_interrupted=False`` is for read-only surfaces such as
        ``--plan`` and must refuse an open transaction instead of rolling it
        back.
        """
        if recover_interrupted:
            self._recover_interrupted_transaction()
        elif self.transaction_path.is_file():
            raise InstallError(
                "interrupted install transaction exists; plan cannot recover or "
                "mutate it -- run recover() / apply recovery first"
            )
        journal = self._load_journal()
        managed = journal["managed"]
        writes: list[str] = []
        removals: list[str] = []
        collisions: list[str] = []
        satisfied: list[str] = []
        sources = self._sources()

        omitted = self.omitted_rels
        for rel, source in sources.items():
            if rel in omitted:
                continue
            target = self._confined_target_path(rel)
            source_hash = _sha256(source)
            if not target.exists():
                writes.append(rel)
                continue
            if not target.is_file():
                collisions.append(rel)
                continue
            target_hash = _sha256(target)
            if target_hash == source_hash:
                satisfied.append(rel)
                continue
            previous = managed.get(rel)
            if previous and previous.get("installed_sha256") == target_hash:
                writes.append(rel)
            else:
                collisions.append(rel)

        for rel, previous in sorted(managed.items()):
            if not _is_rel_path_confined(rel):
                raise InstallError(f"escaping path in install journal: {rel!r}")
            if rel in sources:
                continue
            target = self._confined_target_path(rel)
            if not target.exists():
                removals.append(rel)
            elif target.is_file() and _sha256(target) == previous.get("installed_sha256"):
                removals.append(rel)
            else:
                collisions.append(rel)

        return {
            "writes": writes,
            "removals": removals,
            "collisions": sorted(set(collisions)),
            "satisfied": satisfied,
            "source_hashes": {rel: _sha256(path) for rel, path in sources.items()},
        }

    def verify(self, *, allow_open_transaction: bool = False) -> dict:
        """Read-only verification of rendered assets against installed managed files."""
        if self.transaction_path.exists() and not allow_open_transaction:
            raise InstallError("interrupted install transaction exists; verification cannot recover or mutate it")
        journal = self._load_journal()
        managed = journal["managed"]
        assets = {}
        for rel, source in self._sources().items():
            source_hash = _sha256(source)
            target = self.target_dir / rel
            target_hash = _sha256(target) if target.is_file() else None
            managed_hash = managed.get(rel, {}).get("installed_sha256")
            assets[rel] = {
                "source_sha256": source_hash,
                "target_sha256": target_hash,
                "managed": managed_hash == source_hash,
            }
        verified = bool(assets) and all(
            item["source_sha256"] == item["target_sha256"] and item["managed"]
            for item in assets.values()
        )
        canonical = json.dumps(
            {rel: item["source_sha256"] for rel, item in sorted(assets.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return {
            "schema_version": 1,
            "verified": verified,
            "asset_count": len(assets),
            "source_fingerprint": hashlib.sha256(canonical).hexdigest(),
            "assets": assets,
        }

    def apply(
        self,
        plan: dict | None = None,
        *,
        compose_settings: bool = True,
        fail_after_writes: int | None = None,
        simulate_crash_after_writes: int | None = None,
        hold_transaction: bool = False,
    ) -> dict:
        plan = self.plan() if plan is None else plan
        if plan.get("collisions"):
            raise InstallError("unresolved collisions: " + ", ".join(plan["collisions"]))

        fresh = self.plan()
        if fresh["collisions"]:
            raise InstallError("pre-apply collision detected: " + ", ".join(fresh["collisions"]))
        if set(plan.get("writes", [])) != set(fresh["writes"]) or set(plan.get("removals", [])) != set(fresh["removals"]):
            raise InstallError("install plan became stale before apply")

        sources = self._sources()
        journal = self._load_journal()
        original_journal = json.loads(json.dumps(journal))
        managed = journal["managed"]
        written = 0
        removed = 0
        backup_dir = self.target_dir / self.BACKUP_ROOT / str(time.time_ns())
        transaction = {"schema_version": 1, "original_journal": original_journal, "changes": []}
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self._write_transaction(transaction)

        try:
            for rel in fresh["writes"]:
                target = self._confined_target_path(rel)
                old_data = target.read_bytes() if target.is_file() else None
                backup_rel = None
                if old_data is not None:
                    backup = backup_dir / rel
                    backup_rel_candidate = backup.relative_to(self.target_dir).as_posix()
                    if not _is_rel_path_confined(backup_rel_candidate):
                        raise InstallError(f"escaping recovery backup path: {backup_rel_candidate!r}")
                    _atomic_write(backup, old_data)
                    backup_rel = backup_rel_candidate
                transaction["changes"].append({"path": rel, "backup": backup_rel})
                self._write_transaction(transaction)

                data = sources[rel].read_bytes()
                _atomic_write(target, data)
                managed[rel] = {"installed_sha256": _sha256_bytes(data)}
                written += 1
                if simulate_crash_after_writes is not None and written >= simulate_crash_after_writes:
                    raise SystemExit("injected crash")
                if fail_after_writes is not None and written >= fail_after_writes:
                    raise InstallError("injected apply failure")

            for rel in fresh["removals"]:
                target = self._confined_target_path(rel)
                backup_rel = None
                if target.is_file():
                    backup = backup_dir / rel
                    backup_rel_candidate = backup.relative_to(self.target_dir).as_posix()
                    if not _is_rel_path_confined(backup_rel_candidate):
                        raise InstallError(f"escaping recovery backup path: {backup_rel_candidate!r}")
                    _atomic_write(backup, target.read_bytes())
                    backup_rel = backup_rel_candidate
                transaction["changes"].append({"path": rel, "backup": backup_rel})
                self._write_transaction(transaction)
                if target.is_file():
                    target.unlink()
                managed.pop(rel, None)
                removed += 1

            for rel in fresh["satisfied"]:
                managed.setdefault(rel, {"installed_sha256": fresh["source_hashes"][rel]})

            self._write_journal(journal)
            # update() holds this transaction open across a required migration
            # so a migration failure can still restore the pre-update tree.
            if not hold_transaction:
                self.transaction_path.unlink()
        except SystemExit as exc:
            raise InstallError("simulated process interruption") from exc
        except Exception as exc:
            self._recover_interrupted_transaction()
            if isinstance(exc, InstallError):
                raise
            raise InstallError(f"apply failed and was rolled back: {exc}") from exc

        used_backup = bool(transaction["changes"] and any(e.get("backup") for e in transaction["changes"]))
        result = {
            "written": written,
            "removed": removed,
            "satisfied": len(fresh["satisfied"]),
            "journal": self.JOURNAL,
            "backup_dir": backup_dir.relative_to(self.target_dir).as_posix() if used_backup else None,
        }
        if compose_settings:
            # Assets are already committed. A compose failure must not be
            # reported as an install rollback; surface it on the result and
            # leave recovery to a controlled caller.
            try:
                result.update(self._compose_settings())
                result["settings_composed"] = True
            except InstallError as exc:
                result["settings_conflicts"] = result.get("settings_conflicts") or []
                result["settings_migrations"] = result.get("settings_migrations") or []
                result["settings_composed"] = False
                result["settings_compose_error"] = str(exc)
        else:
            # Opt-out used by studio_update.update() and by activation's
            # global-root step. Settings stay outside the asset transaction;
            # project activation still composes them.
            result["settings_conflicts"] = []
            result["settings_migrations"] = []
            result["settings_composed"] = False
        return result

    def _is_user_scope_target(self) -> bool:
        """Smallest explicit guard against composing the project-scoped
        worktree-isolation recipe into the user-global Claude settings file
        (~/.claude/settings.json): the gate's hook command is
        $CLAUDE_PROJECT_DIR-relative, which is meaningless at user scope
        and would deny Bash for every project on the machine if installed
        there. Deliberately narrow -- not a general path-policy framework
        -- it recognizes exactly the one documented user-scope convention."""
        try:
            return self.target_dir.resolve() == (Path.home() / ".claude").resolve()
        except OSError:
            return False

    def _compose_settings(self) -> dict:
        """Additively compose the proven worktree-isolation recipe into this
        install's own settings.json, once the asset transaction above has
        fully committed. Runs only after a successful apply(), so a fresh
        install delivers the settings contract in the same call as the
        agents/skills/hooks assets rather than requiring a separate,
        hand-written step. Never raises on an incompatible existing value --
        the Operator's value is preserved and the conflict is returned here
        for the caller to surface, exactly as compose_worktree_isolation_settings
        already guarantees. Skips entirely (also reported, never a silent
        success) at the user-global scope -- see _is_user_scope_target.

        Returns {"settings_conflicts": [...], "settings_migrations": [...]},
        spread directly into apply()'s own result dict so a caller reading
        that result sees any stale artifact composition removed (e.g. the
        pre-ADR-0004 `"agent": "agency-director"` default) exactly as
        honestly as it already sees a real conflict.

        `apply(compose_settings=False)` does not call this method.
        `studio_update.update()` and activation's global-root step use that
        opt-out. Only project-scope callers keep the default and compose
        here: project activation and the project-local direct CLI (main()
        refuses any --target other than <project>/.claude)."""
        if self._is_user_scope_target():
            return {"settings_conflicts": [{
                "field": "settings.json",
                "required": "project-scoped install target",
                "existing": f"settings composition skipped: {self.target_dir} is the user-global "
                            "Claude settings scope; the gate's $CLAUDE_PROJECT_DIR-relative hook "
                            "command is only valid for a single project's own .claude/settings.json",
            }], "settings_migrations": []}

        from studio_settings_merge import SettingsMergeError, compose_worktree_isolation_settings

        path = self.target_dir / "settings.json"
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise InstallError(f"cannot compose settings.json: invalid JSON: {exc}") from exc
        else:
            existing = {}

        try:
            result = compose_worktree_isolation_settings(existing)
        except SettingsMergeError as exc:
            raise InstallError(f"cannot compose settings.json: {exc}") from exc

        data = (json.dumps(result["settings"], indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(path, data)
        return {"settings_conflicts": result["conflicts"], "settings_migrations": result["migrations"]}

    def uninstall(self) -> dict:
        self._recover_interrupted_transaction()
        journal = self._load_journal()
        removed: list[str] = []
        preserved_modified: list[str] = []

        for rel, record in sorted(journal["managed"].items()):
            if not _is_rel_path_confined(rel):
                raise InstallError(f"escaping path in install journal: {rel!r}")
            target = self._confined_target_path(rel)
            if not target.exists():
                continue
            if not target.is_file() or _sha256(target) != record.get("installed_sha256"):
                preserved_modified.append(rel)
                continue
            target.unlink()
            removed.append(rel)
            # Kit-created directories (skills/<name>/, runtime/...) must not
            # outlive their last managed file; a directory still holding
            # user content is not empty and stays.
            self._prune_empty_parents(target)

        if preserved_modified:
            journal["managed"] = {rel: journal["managed"][rel] for rel in preserved_modified}
            self._write_journal(journal)
        elif self.journal_path.exists():
            self.journal_path.unlink()

        # Keyed on the asset's actual absence from disk after processing above,
        # not on whether *this* call removed it: the script may already have
        # been missing going in (an out-of-band deletion, a prior interrupted
        # uninstall) and would otherwise land in neither `removed` nor
        # `preserved_modified`, silently skipping cleanup and leaving a stale
        # hook no later uninstall() call could ever trigger again.
        if not (self.target_dir / "hooks" / "studio_worktree_gate.py").is_file():
            self._remove_gate_hook_entries()

        return {"removed": removed, "preserved_modified": preserved_modified}

    def _remove_gate_hook_entries(self) -> None:
        """Called only once uninstall() has actually deleted the gate script
        asset: strip any PreToolUse hook entry whose (matcher, command) is a
        command Workflow Kit itself has ever shipped for this gate (current
        or a recognized superseded one), so no surviving hook can reference
        a script this uninstall just removed. Every other settings.json key
        and every other hook is left untouched; a missing/malformed/
        already-clean settings.json is a no-op, not an error, since
        uninstall's own asset removal above already succeeded regardless."""
        from studio_settings_merge import LEGACY_GATE_HOOKS, REQUIRED_GATE_HOOKS

        path = self.target_dir / "settings.json"
        if not path.is_file():
            return
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if not isinstance(settings, dict):
            return
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return
        pre_tool_use = hooks.get("PreToolUse")
        if not isinstance(pre_tool_use, list):
            return

        owned_hooks = set(REQUIRED_GATE_HOOKS) | LEGACY_GATE_HOOKS
        changed = False
        cleaned: list = []
        for entry in pre_tool_use:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue
            entry_hooks = entry.get("hooks")
            if not isinstance(entry_hooks, list):
                cleaned.append(entry)
                continue
            matcher = entry.get("matcher")
            kept_hooks = []
            for hook in entry_hooks:
                command = hook.get("command") if isinstance(hook, dict) else None
                if (isinstance(matcher, str) and isinstance(command, str)
                        and (matcher, command) in owned_hooks):
                    changed = True
                    continue
                kept_hooks.append(hook)
            if kept_hooks:
                cleaned.append({**entry, "hooks": kept_hooks} if len(kept_hooks) != len(entry_hooks) else entry)
            else:
                changed = True

        if not changed:
            return

        # Drop only containers this removal emptied, so an uninstall does not
        # leave a Kit-shaped `"hooks": {"PreToolUse": []}` residue behind.
        hooks = dict(hooks)
        if cleaned:
            hooks["PreToolUse"] = cleaned
        else:
            hooks.pop("PreToolUse")
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks")
        data = (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(path, data)


def main() -> int:
    """One-command Operator install: render the kit into a private temporary
    directory, then hand it unchanged to StudioInstaller. No new abstraction --
    this only chains render_kit() and StudioInstaller.plan()/apply() exactly as
    TransactionalInstallerContractTests already exercises them directly.

    Project-local only: --target must be exactly <project>/.claude, the same
    binding studio_update.py uses. A global or config root is refused before
    anything is rendered or written; studio_activation.py --global-root is
    the global install path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, default=Path("_studio/kit"))
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--target", type=Path, default=Path(".claude"))
    parser.add_argument("--plan", action="store_true",
                         help="print the install plan without writing anything")
    args = parser.parse_args()

    if args.target.resolve() != args.project.resolve() / ".claude":
        print(f"install error: studio_installer.py is project-local only; --target must be "
              f"<project>/.claude (project: {args.project.resolve()}), got {args.target.resolve()}. "
              "For a global or config root use studio_activation.py --global-root <root> "
              "--project <project>.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as rendered_dir:
        try:
            render_kit(args.kit, Path(rendered_dir))
        except RenderError as exc:
            print(f"install error: {exc}", file=sys.stderr)
            return 1

        installer = StudioInstaller(Path(rendered_dir), args.target)
        try:
            # --plan is read-only: never roll back an open transaction.
            plan = installer.plan(recover_interrupted=not args.plan)
        except InstallError as exc:
            print(f"install error: {exc}", file=sys.stderr)
            return 1

        if plan["collisions"]:
            print("install blocked by unresolved collisions:", file=sys.stderr)
            for rel in plan["collisions"]:
                print(f"  {rel}", file=sys.stderr)
            return 1

        if args.plan:
            print(json.dumps(plan, sort_keys=True))
            return 0

        try:
            result = installer.apply(plan)
        except InstallError as exc:
            print(f"install error: {exc}", file=sys.stderr)
            return 1
        if result.get("settings_compose_error"):
            print(f"install warning: assets committed; settings compose failed: {result['settings_compose_error']}", file=sys.stderr)

        print(json.dumps(result, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

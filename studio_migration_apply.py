#!/usr/bin/env python3
"""Transactional project-local application of Studio migration plans."""
from __future__ import annotations
import hashlib, json, os, shutil, time
from pathlib import Path
from studio_migration import MigrationPlanner

class MigrationApplyError(RuntimeError): pass

class MigrationApplier:
    PROFILE_ROOT = ".studio"
    PROFILE_FILES = {"project-profile": "project-profile.json", "project-profile-extension": "project-profile-extension.json"}

    def __init__(self, project: Path, *, state_base: Path|None=None):
        self.project = Path(project)
        base = Path(state_base) if state_base is not None else Path(os.environ.get("XDG_STATE_HOME", Path.home()/".local"/"state")) / "studio" / "migrations"
        project_key = hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()[:20]
        self.state_root = (base / project_key).resolve()

    def _assert_fresh(self, plan: dict) -> None:
        if MigrationPlanner(self.project).plan() != plan: raise MigrationApplyError("migration plan became stale before apply")

    @staticmethod
    def _fingerprint(plan: dict) -> str:
        return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _present(path: Path) -> bool: return path.exists() or path.is_symlink()

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _validated_global_evidence(evidence: dict|None) -> dict|None:
        if not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or evidence.get("verified") is not True:
            return None
        assets = evidence.get("assets")
        fingerprint = evidence.get("source_fingerprint")
        if not isinstance(assets, dict) or not assets or not isinstance(fingerprint, str) or not fingerprint:
            return None
        for item in assets.values():
            if not isinstance(item, dict): return None
            if item.get("source_sha256") != item.get("target_sha256") or item.get("managed") is not True: return None
        return evidence

    def _incomplete(self):
        if not self.state_root.exists(): return []
        found = []
        for journal in sorted(self.state_root.glob("*/journal.json")):
            try: data = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): continue
            if data.get("state") == "APPLYING": found.append((journal, data))
        return found

    def _assert_no_incomplete(self) -> None:
        if self._incomplete(): raise MigrationApplyError("incomplete migration transaction exists; recover it before continuing")

    def recover_incomplete(self) -> list[dict]:
        recovered = []
        for journal_path, journal in self._incomplete():
            root = journal_path.parent
            self._restore_changes(list(journal.get("changes", [])), root / "rollback")
            journal["state"] = "RECOVERED"
            self._atomic_json(journal_path, journal)
            recovered.append({"status": "RECOVERED", "transaction_id": journal.get("transaction_id")})
        return recovered

    def _verify(self, plan: dict, quarantine: Path) -> dict:
        quarantine_actions = plan.get("actions", {}).get("quarantine", [])
        replaced = [a for a in plan.get("actions", {}).get("replace", []) if "transformation" not in a]
        removed = quarantine_actions + replaced
        legacy_absent = all(not self._present(self.project / a["path"]) for a in removed)
        quarantine_complete = all(self._present(quarantine / a["path"]) for a in removed)
        claude = self.project / "CLAUDE.md"; imports_valid = True
        if claude.is_file():
            for line in claude.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("@"): imports_valid = imports_valid and (self.project / s[1:]).exists()
        profile_actions = [a for a in replaced if a.get("preserve_as") in self.PROFILE_FILES and a.get("profile_data")]
        profiles = all((self.project / self.PROFILE_ROOT / self.PROFILE_FILES[a["preserve_as"]]).is_file() for a in profile_actions)
        passed = legacy_absent and imports_valid and profiles and quarantine_complete
        return {"passed": passed, "legacy_authorities_absent": legacy_absent, "claude_imports_valid": imports_valid, "project_profiles_materialized": profiles, "quarantine_complete": quarantine_complete}

    def apply(self, plan: dict, *, execute: bool=False, expected_fingerprint: str|None=None, fail_after: int|None=None, crash_after: int|None=None, require_global_prerequisites: bool=False, global_prerequisite_evidence: dict|None=None) -> dict:
        self._assert_no_incomplete()
        self._assert_fresh(plan)
        globals_ = list(plan.get("actions", {}).get("install", [])); fingerprint = self._fingerprint(plan)
        if not execute: return {"status":"DRY_RUN","source_fingerprint":fingerprint,"global_actions_deferred":globals_}
        if expected_fingerprint != fingerprint: raise MigrationApplyError("execute requires the exact approved fingerprint from the dry run")
        verified_globals = self._validated_global_evidence(global_prerequisite_evidence)
        if require_global_prerequisites and globals_ and verified_globals is None:
            raise MigrationApplyError("global prerequisites blocked: global prerequisite evidence is missing or invalid")
        transaction_id=str(time.time_ns()); root=self.state_root/transaction_id; quarantine=root/"quarantine"; rollback=root/"rollback"; changes=[]; count=0
        journal_path = root / "journal.json"
        journal = {"schema_version": 1, "state": "APPLYING", "project": str(self.project.resolve()), "transaction_id": transaction_id, "source_fingerprint": fingerprint, "changes": changes}
        self._atomic_json(journal_path, journal)

        def persist_changes():
            journal["changes"] = list(changes)
            self._atomic_json(journal_path, journal)

        def record_original(rel):
            source=self.project/rel; backup=rollback/rel; backup.parent.mkdir(parents=True,exist_ok=True)
            if source.is_symlink(): os.symlink(os.readlink(source),backup); kind="symlink"
            elif source.is_dir(): shutil.copytree(source,backup); kind="dir"
            elif source.is_file(): shutil.copy2(source,backup); kind="file"
            else: raise MigrationApplyError(f"migration source missing: {rel}")
            changes.append({"path":rel,"kind":kind,"existed":True}); persist_changes(); return source

        def write_profile(action):
            filename=self.PROFILE_FILES.get(action.get("preserve_as")); data=action.get("profile_data")
            if not filename or not data: return
            rel=f"{self.PROFILE_ROOT}/{filename}"; target=self.project/rel
            if self._present(target): raise MigrationApplyError(f"migration profile destination already exists: {rel}")
            changes.append({"path":rel,"kind":"file","existed":False}); persist_changes()
            target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps(data,sort_keys=True,indent=2)+"\n",encoding="utf-8")

        def transform(source, action):
            content=source.read_text(encoding="utf-8")
            if hashlib.sha256(content.encode()).hexdigest()!=action.get("source_sha256"): raise MigrationApplyError("migration transformation source hash mismatch")
            exact=action.get("transformation",{}).get("remove_exact_import")
            if not exact: raise MigrationApplyError("unsupported migration transformation")
            lines=content.splitlines(); filtered=[line for line in lines if line.strip()!=exact]
            replacement="\n".join(filtered)+( "\n" if filtered else "")
            source.write_text(replacement,encoding="utf-8")

        def checkpoint():
            nonlocal count; count+=1
            if crash_after is not None and count>=crash_after: raise SystemExit("injected abrupt migration crash")
            if fail_after is not None and count>=fail_after: raise MigrationApplyError("injected migration failure")

        try:
            for action in plan.get("actions",{}).get("replace",[]):
                rel=action["path"]; source=record_original(rel)
                if "transformation" in action: transform(source,action)
                else:
                    write_profile(action); destination=quarantine/rel; destination.parent.mkdir(parents=True,exist_ok=True); shutil.move(str(source),str(destination))
                checkpoint()
            for action in plan.get("actions",{}).get("quarantine",[]):
                rel=action["path"]; source=record_original(rel); destination=quarantine/rel; destination.parent.mkdir(parents=True,exist_ok=True); shutil.move(str(source),str(destination)); checkpoint()
        except Exception as exc:
            self._restore_changes(changes,rollback)
            journal["state"] = "ROLLED_BACK"; self._atomic_json(journal_path, journal)
            if isinstance(exc,MigrationApplyError): raise
            raise MigrationApplyError(f"migration failed and was rolled back: {exc}") from exc

        verification=self._verify(plan,quarantine)
        if not verification["passed"]:
            self._restore_changes(changes,rollback); journal["state"] = "ROLLED_BACK"; self._atomic_json(journal_path, journal)
            raise MigrationApplyError("post-migration verification failed and was rolled back")
        evidence_summary = None if verified_globals is None else {"schema_version": 1, "verified": True, "asset_count": verified_globals["asset_count"], "source_fingerprint": verified_globals["source_fingerprint"]}
        manifest={"schema_version":1,"project":plan.get("project"),"transaction_id":transaction_id,"source_fingerprint":fingerprint,"changes":changes,"global_actions_deferred":globals_,"global_prerequisite_evidence":evidence_summary,"verification":verification}
        manifest_path=root/"manifest.json"; self._atomic_json(manifest_path, manifest)
        journal["state"] = "COMMITTED"; self._atomic_json(journal_path, journal)
        return {"status":"APPLIED","source_fingerprint":fingerprint,"quarantine_dir":str(quarantine),"transaction":str(manifest_path),"verification":verification,"global_actions_deferred":globals_,"global_prerequisite_evidence":evidence_summary}

    def _restore_changes(self, changes, rollback):
        for change in reversed(changes):
            rel=change["path"]; target=self.project/rel; backup=rollback/rel
            if target.is_symlink(): target.unlink()
            elif target.is_dir(): shutil.rmtree(target)
            elif target.exists(): target.unlink()
            if not change.get("existed",True): continue
            target.parent.mkdir(parents=True,exist_ok=True)
            if change["kind"]=="symlink": os.symlink(os.readlink(backup),target)
            elif change["kind"]=="dir": shutil.copytree(backup,target)
            else: shutil.copy2(backup,target)

    def rollback(self, transaction: str) -> dict:
        manifest_path=Path(transaction)
        if not manifest_path.is_absolute(): manifest_path=self.project/manifest_path
        try: root=manifest_path.parent; manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc: raise MigrationApplyError("invalid migration transaction for rollback") from exc
        if not root.resolve().is_relative_to(self.state_root): raise MigrationApplyError("migration transaction is outside the Studio state root")
        self._restore_changes(list(manifest.get("changes",[])),root/"rollback")
        journal_path=root/"journal.json"
        if journal_path.is_file():
            journal=json.loads(journal_path.read_text(encoding="utf-8")); journal["state"]="ROLLED_BACK"; self._atomic_json(journal_path,journal)
        return {"status":"ROLLED_BACK","transaction":str(manifest_path)}

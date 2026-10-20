#!/usr/bin/env python3
"""Restore the latest crawler state artifact for this workflow and branch."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote


ARTIFACT_NAME = "trendradar-state"
STATE_DIRECTORIES = ("news", "rss", "briefings")
PAGE_SIZE = 100


class StateRestoreError(RuntimeError):
    """Raised when state cannot be safely and reliably restored."""


def _gh_environment() -> dict[str, str]:
    env = os.environ.copy()
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        raise StateRestoreError("GITHUB_TOKEN is required to read Actions artifacts")
    env["GH_TOKEN"] = token
    return env


def _gh_api_json(endpoint: str, env: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", endpoint],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "gh api failed"
        raise StateRestoreError(f"GitHub API request failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StateRestoreError("GitHub API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise StateRestoreError("GitHub API returned an unexpected response")
    return payload


def _download_artifact(
    repository: str, artifact_id: int, zip_path: Path, env: dict[str, str]
) -> None:
    endpoint = f"repos/{repository}/actions/artifacts/{artifact_id}/zip"
    with zip_path.open("wb") as output:
        result = subprocess.run(
            ["gh", "api", endpoint],
            check=False,
            stdout=output,
            stderr=subprocess.PIPE,
            env=env,
        )
    if result.returncode != 0:
        zip_path.unlink(missing_ok=True)
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise StateRestoreError(
            f"Actions artifact download failed: {detail or 'gh api failed'}"
        )


def _repo_api_path(repository: str) -> str:
    pieces = repository.split("/")
    if len(pieces) != 2 or any(not part for part in pieces):
        raise StateRestoreError("GITHUB_REPOSITORY must have the form owner/repository")
    return "/".join(quote(part, safe="-._") for part in pieces)


def _workflow_api_path(workflow: str) -> str:
    if not workflow or workflow.startswith("/") or ".." in workflow.split("/"):
        raise StateRestoreError("Workflow must be a repository-relative workflow file")
    return quote(workflow, safe="/-._")


def _find_latest_artifact(
    repository: str,
    branch: str,
    workflow: str,
    env: dict[str, str],
) -> dict[str, Any] | None:
    repo_path = _repo_api_path(repository)
    workflow_path = _workflow_api_path(workflow)
    workflow_data = _gh_api_json(
        f"repos/{repo_path}/actions/workflows/{workflow_path}", env
    )
    workflow_id = workflow_data.get("id")
    if not isinstance(workflow_id, int) or isinstance(workflow_id, bool):
        raise StateRestoreError("Workflow API response did not include a valid id")

    candidates: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _gh_api_json(
            f"repos/{repo_path}/actions/artifacts?name={ARTIFACT_NAME}"
            f"&per_page={PAGE_SIZE}&page={page}",
            env,
        )
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise StateRestoreError("Artifact API response did not include an artifact list")

        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("name") != ARTIFACT_NAME:
                continue
            expired = artifact.get("expired")
            if not isinstance(expired, bool):
                raise StateRestoreError(
                    "Matching artifact metadata is incomplete; refusing bootstrap"
                )
            if expired:
                continue

            run = artifact.get("workflow_run")
            artifact_id = artifact.get("id")
            if (
                not isinstance(artifact_id, int)
                or isinstance(artifact_id, bool)
                or not isinstance(run, dict)
                or not isinstance(run.get("id"), int)
                or isinstance(run.get("id"), bool)
                or not isinstance(run.get("head_branch"), str)
                or not isinstance(artifact.get("created_at"), str)
            ):
                raise StateRestoreError(
                    "Matching artifact metadata is incomplete; refusing bootstrap"
                )
            if run["head_branch"] == branch:
                candidates.append(
                    {
                        "id": artifact_id,
                        "run_id": run["id"],
                        "created_at": artifact["created_at"],
                    }
                )

        if len(artifacts) < PAGE_SIZE:
            break
        page += 1

    candidates.sort(
        key=lambda artifact: (artifact["created_at"], artifact["id"]), reverse=True
    )
    for candidate in candidates:
        run_data = _gh_api_json(
            f"repos/{repo_path}/actions/runs/{candidate['run_id']}", env
        )
        run_workflow_id = run_data.get("workflow_id")
        run_branch = run_data.get("head_branch")
        if not isinstance(run_workflow_id, int) or isinstance(run_workflow_id, bool):
            raise StateRestoreError(
                "Workflow run API response did not include a valid workflow id"
            )
        if not isinstance(run_branch, str):
            raise StateRestoreError(
                "Workflow run API response did not include a valid branch"
            )
        if run_workflow_id == workflow_id and run_branch == branch:
            return candidate
    return None


def _canonical_archive_path(info: zipfile.ZipInfo) -> tuple[str, ...] | None:
    name = info.filename
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise StateRestoreError("Artifact contains an unsafe archive path")

    parts = name[:-1].split("/") if info.is_dir() and name.endswith("/") else name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StateRestoreError("Artifact contains an unsafe archive path")
    if parts == ["output"] and info.is_dir():
        return None
    if parts and parts[0] == "output":
        parts = parts[1:]
    if not parts or parts[0] not in STATE_DIRECTORIES:
        raise StateRestoreError("Artifact contains data outside the crawler state directories")
    if len(parts) == 1 and not info.is_dir():
        raise StateRestoreError("Artifact contains a file at a state directory root")

    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise StateRestoreError("Artifact contains a symbolic link")
    return tuple(parts)


def _extract_state_archive(zip_path: Path, staging: Path) -> None:
    staged_output = staging / "output"
    staged_output.mkdir(parents=True)
    file_count = 0
    total_size = 0

    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                parts = _canonical_archive_path(info)
                if parts is None:
                    continue
                target = staged_output.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                file_count += 1
                total_size += info.file_size
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, StateRestoreError):
            raise
        raise StateRestoreError(f"Artifact is not a valid state archive: {exc}") from exc

    if file_count == 0 or total_size == 0:
        raise StateRestoreError("State artifact is empty; refusing to replace saved state")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _install_staged_state(destination: Path, staging: Path) -> None:
    output_root = destination / "output"
    if output_root.is_symlink():
        raise StateRestoreError("output directory cannot be a symbolic link")
    output_root.mkdir(parents=True, exist_ok=True)

    staged_output = staging / "output"
    backup_root = staging / "backup"
    backup_root.mkdir()
    backed_up: list[str] = []
    installed: list[str] = []
    try:
        for name in STATE_DIRECTORIES:
            target = output_root / name
            saved = backup_root / name
            if target.exists() or target.is_symlink():
                target.replace(saved)
                backed_up.append(name)
            staged = staged_output / name
            if staged.exists():
                staged.replace(target)
                installed.append(name)
    except OSError as exc:
        for name in reversed(STATE_DIRECTORIES):
            target = output_root / name
            if name in installed:
                _remove_path(target)
            saved = backup_root / name
            if name in backed_up and saved.exists():
                saved.replace(target)
        raise StateRestoreError(f"Could not install restored state: {exc}") from exc


def restore_state(
    repository: str,
    branch: str,
    workflow: str,
    destination: str | Path,
) -> bool:
    """Restore latest matching state; return False only for first-run bootstrap."""
    if not branch:
        raise StateRestoreError("GITHUB_REF_NAME is required")
    env = _gh_environment()
    artifact = _find_latest_artifact(repository, branch, workflow, env)
    if artifact is None:
        print(f"No {ARTIFACT_NAME} artifact found for {workflow} on {branch}; bootstrapping")
        return False

    destination_path = Path(destination).resolve()
    if not destination_path.is_dir():
        raise StateRestoreError("Project destination must be an existing directory")
    with tempfile.TemporaryDirectory(prefix=".trendradar-state-", dir=destination_path) as temp:
        staging = Path(temp)
        zip_path = staging / "state.zip"
        _download_artifact(repository, artifact["id"], zip_path, env)
        _extract_state_archive(zip_path, staging)
        _install_staged_state(destination_path, staging)
    print(f"Restored {ARTIFACT_NAME} artifact {artifact['id']} for {branch}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", ""))
    parser.add_argument("--workflow", default="crawler.yml")
    parser.add_argument("--destination", default=".")
    args = parser.parse_args(argv)

    try:
        restore_state(args.repository, args.branch, args.workflow, args.destination)
    except StateRestoreError as exc:
        print(f"State restore failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

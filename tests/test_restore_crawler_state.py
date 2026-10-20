from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts import restore_crawler_state as state_restore


def _artifact(
    artifact_id: int,
    *,
    branch: str = "master",
    workflow_id: int = 101,
    run_number: int = 1,
) -> dict:
    return {
        "id": artifact_id,
        "name": state_restore.ARTIFACT_NAME,
        "expired": False,
        "created_at": f"2026-10-19T00:00:{run_number:02d}Z",
        "workflow_run": {
            "id": 1000 + artifact_id,
            "head_branch": branch,
            "_workflow_id": workflow_id,
        },
    }


def _state_zip(*, traversal: bool = False, marker: bytes = b"saved") -> bytes:
    contents = BytesIO()
    with zipfile.ZipFile(contents, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("news/2026-10-19.db", marker)
        archive.writestr("rss/2026-10-19.db", b"rss-state")
        archive.writestr("briefings/2026-10-19.json", b"send-state")
        if traversal:
            archive.writestr("news/../../escaped.txt", b"unsafe")
    return contents.getvalue()


def _empty_zip() -> bytes:
    contents = BytesIO()
    with zipfile.ZipFile(contents, "w"):
        pass
    return contents.getvalue()


class RestoreCrawlerStateTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _api(self, artifacts):
        def response(endpoint, env):
            if "/actions/workflows/" in endpoint:
                return {"id": 101}
            if "/actions/runs/" in endpoint:
                run_id = int(endpoint.rsplit("/", 1)[-1])
                for artifact in artifacts:
                    run = artifact["workflow_run"]
                    if run["id"] == run_id:
                        return {
                            "workflow_id": run["_workflow_id"],
                            "head_branch": run["head_branch"],
                        }
                raise AssertionError(f"Unexpected workflow run: {endpoint}")
            if "/actions/artifacts?" in endpoint:
                return {"artifacts": artifacts}
            raise AssertionError(f"Unexpected API endpoint: {endpoint}")

        return response

    def _restore_with_zip(self, root: Path, artifacts, zip_data: bytes):
        with (
            patch.object(state_restore, "_gh_api_json", side_effect=self._api(artifacts)),
            patch.object(
                state_restore,
                "_download_artifact",
                side_effect=lambda repository, artifact_id, path, env: path.write_bytes(
                    zip_data
                ),
            ) as download,
        ):
            restored = state_restore.restore_state(
                "owner/repo", "master", "crawler.yml", root
            )
        return restored, download

    def test_restores_latest_same_workflow_branch_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = [
                _artifact(8, run_number=8),
                _artifact(9, run_number=9),
                _artifact(10, branch="feature", run_number=10),
                _artifact(11, workflow_id=999, run_number=11),
            ]
            restored, download = self._restore_with_zip(root, artifacts, _state_zip())

            self.assertTrue(restored)
            download.assert_called_once()
            self.assertEqual(download.call_args.args[1], 9)
            self.assertEqual(
                (root / "output/news/2026-10-19.db").read_bytes(), b"saved"
            )
            self.assertEqual(
                (root / "output/rss/2026-10-19.db").read_bytes(), b"rss-state"
            )
            self.assertEqual(
                (root / "output/briefings/2026-10-19.json").read_bytes(), b"send-state"
            )

    def test_no_artifact_bootstraps_without_erasing_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_state = root / "output/news/old.db"
            old_state.parent.mkdir(parents=True)
            old_state.write_bytes(b"keep")
            with (
                patch.object(
                    state_restore, "_gh_api_json", side_effect=self._api([])
                ),
                patch.object(state_restore, "_download_artifact") as download,
            ):
                restored = state_restore.restore_state(
                    "owner/repo", "master", "crawler.yml", root
                )

            self.assertFalse(restored)
            download.assert_not_called()
            self.assertEqual(old_state.read_bytes(), b"keep")

    def test_api_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(
                    state_restore.subprocess,
                    "run",
                    return_value=state_restore.subprocess.CompletedProcess(
                        ["gh", "api"], 1, "", "API unavailable"
                    ),
                ) as gh_call,
                patch.object(state_restore, "_download_artifact") as download,
            ):
                with self.assertRaisesRegex(state_restore.StateRestoreError, "API unavailable"):
                    state_restore.restore_state(
                        "owner/repo", "master", "crawler.yml", root
                    )

            gh_call.assert_called_once()
            self.assertEqual(gh_call.call_args.args[0][:2], ["gh", "api"])
            self.assertEqual(gh_call.call_args.kwargs["env"]["GH_TOKEN"], "test-token")
            download.assert_not_called()

    def test_download_error_fails_without_changing_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_state = root / "output/news/old.db"
            old_state.parent.mkdir(parents=True)
            old_state.write_bytes(b"keep")
            with (
                patch.object(
                    state_restore,
                    "_gh_api_json",
                    side_effect=self._api([_artifact(9, run_number=9)]),
                ),
                patch.object(
                    state_restore,
                    "_download_artifact",
                    side_effect=state_restore.StateRestoreError("download failed"),
                ),
            ):
                with self.assertRaisesRegex(
                    state_restore.StateRestoreError, "download failed"
                ):
                    state_restore.restore_state(
                        "owner/repo", "master", "crawler.yml", root
                    )

            self.assertEqual(old_state.read_bytes(), b"keep")

    def test_artifacts_from_other_branches_are_never_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = [_artifact(9, branch="release", run_number=9)]
            with (
                patch.object(
                    state_restore,
                    "_gh_api_json",
                    side_effect=self._api(artifacts),
                ),
                patch.object(state_restore, "_download_artifact") as download,
            ):
                restored = state_restore.restore_state(
                    "owner/repo", "master", "crawler.yml", root
                )

            self.assertFalse(restored)
            download.assert_not_called()
            self.assertFalse((root / "output").exists())

    def test_empty_artifact_is_rejected_without_replacing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_state = root / "output/news/old.db"
            old_state.parent.mkdir(parents=True)
            old_state.write_bytes(b"keep")
            with (
                patch.object(
                    state_restore,
                    "_gh_api_json",
                    side_effect=self._api([_artifact(9, run_number=9)]),
                ),
                patch.object(
                    state_restore,
                    "_download_artifact",
                    side_effect=lambda repository, artifact_id, path, env: path.write_bytes(
                        _empty_zip()
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    state_restore.StateRestoreError, "State artifact is empty"
                ):
                    state_restore.restore_state(
                        "owner/repo", "master", "crawler.yml", root
                    )

            self.assertEqual(old_state.read_bytes(), b"keep")

    def test_zip_traversal_is_rejected_without_replacing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_state = root / "output/news/old.db"
            old_state.parent.mkdir(parents=True)
            old_state.write_bytes(b"keep")
            with (
                patch.object(
                    state_restore,
                    "_gh_api_json",
                    side_effect=self._api([_artifact(9, run_number=9)]),
                ),
                patch.object(
                    state_restore,
                    "_download_artifact",
                    side_effect=lambda repository, artifact_id, path, env: path.write_bytes(
                        _state_zip(traversal=True)
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    state_restore.StateRestoreError, "unsafe archive path"
                ):
                    state_restore.restore_state(
                        "owner/repo", "master", "crawler.yml", root
                    )

            self.assertEqual(old_state.read_bytes(), b"keep")
            self.assertFalse((root / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()

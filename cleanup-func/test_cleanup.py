import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from cleanup import (
    AzureFiles,
    BlobState,
    Config,
    IndexedChunk,
    SearchIndex,
    StateSnapshot,
    parse_source,
    reconcile,
)


NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
CONFIG = Config(
    search_endpoint="https://example.search.windows.net",
    index_name="af-index-all",
    account_name="shareiarag",
    shares=("admin-share", "alluser-share"),
    dry_run=False,
    confirmation_seconds=300,
    scope_hash=None,
)
URL = "https://shareiarag.file.core.windows.net/admin-share/folder/a%20file.pdf"
CHUNKS = [
    IndexedChunk("chunk-1", URL, "admin-share"),
    IndexedChunk("chunk-2", URL, "admin-share"),
]


class MemoryIndex:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.deleted = []

    def list_chunks(self):
        return iter(self.chunks)

    def delete_chunks(self, chunk_ids):
        self.deleted.extend(chunk_ids)
        self.chunks = [chunk for chunk in self.chunks if chunk.chunk_id not in chunk_ids]


class MemoryFiles:
    def __init__(self, exists=False):
        self.default_exists = exists
        self.results = {}
        self.checked_shares = []
        self.checked_sources = []

    def check_share(self, share):
        self.checked_shares.append(share)

    def exists(self, source):
        self.checked_sources.append(source)
        values = self.results.get(source.identity_hash)
        if values:
            return values.pop(0)
        return self.default_exists


class MemoryState:
    def __init__(self, missing_since=None):
        self.missing_since = dict(missing_since or {})
        self.etag = "initial"
        self.saves = 0

    def load(self):
        return StateSnapshot(dict(self.missing_since), self.etag)

    def save(self, missing_since, etag):
        if etag != self.etag:
            raise RuntimeError("Concurrent state update.")
        self.saves += 1
        self.etag = f"etag-{self.saves}"
        self.missing_since = dict(missing_since)
        return self.etag


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.index = MemoryIndex(CHUNKS)
        self.files = MemoryFiles()
        self.state = MemoryState()
        self.source = parse_source(CHUNKS[0], CONFIG.account_name, CONFIG.shares)

    def run_cleanup(self, now, config=CONFIG):
        return reconcile(config, self.index, self.files, self.state, now)

    def test_missing_file_requires_two_observations_and_grace_period(self):
        first = self.run_cleanup(NOW)
        self.assertEqual((first.checked_files, first.missing_files, first.deleted_chunks), (1, 1, 0))
        self.assertEqual(self.state.missing_since[self.source.identity_hash], NOW)

        early = self.run_cleanup(NOW + timedelta(seconds=299))
        self.assertEqual(early.eligible_files, 0)
        self.assertEqual(self.index.deleted, [])

        confirmed = self.run_cleanup(NOW + timedelta(seconds=300))
        self.assertEqual((confirmed.eligible_files, confirmed.deleted_files, confirmed.deleted_chunks), (1, 1, 2))
        self.assertEqual(self.index.deleted, ["chunk-1", "chunk-2"])
        self.assertEqual(self.files.checked_shares.count("admin-share"), 4)
        self.assertEqual(self.files.checked_shares.count("alluser-share"), 3)

    def test_dry_run_never_deletes(self):
        self.run_cleanup(NOW, replace(CONFIG, dry_run=True))
        report = self.run_cleanup(NOW + timedelta(minutes=5), replace(CONFIG, dry_run=True))
        self.assertTrue(report.dry_run)
        self.assertEqual(report.eligible_files, 1)
        self.assertEqual(report.deleted_chunks, 0)
        self.assertEqual(self.index.deleted, [])

    def test_existing_file_clears_previous_missing_state(self):
        self.run_cleanup(NOW)
        self.files.default_exists = True
        self.run_cleanup(NOW + timedelta(minutes=5))
        self.assertEqual(self.state.missing_since, {})
        self.assertEqual(self.index.deleted, [])

    def test_reappearing_during_final_check_resets_confirmation(self):
        self.run_cleanup(NOW)
        self.files.results[self.source.identity_hash] = [False, True]
        report = self.run_cleanup(NOW + timedelta(minutes=5))
        self.assertEqual(report.deleted_chunks, 0)
        self.assertEqual(self.state.missing_since, {})

        next_report = self.run_cleanup(NOW + timedelta(minutes=6))
        self.assertEqual(next_report.eligible_files, 0)
        self.assertEqual(self.state.missing_since[self.source.identity_hash], NOW + timedelta(minutes=6))

    def test_scope_keeps_other_pending_state_and_skips_other_files(self):
        other = IndexedChunk(
            "other",
            "https://shareiarag.file.core.windows.net/alluser-share/b.pdf",
            "alluser-share",
        )
        other_source = parse_source(other, CONFIG.account_name, CONFIG.shares)
        self.index.chunks.append(other)
        self.state.missing_since[other_source.identity_hash] = NOW - timedelta(hours=1)
        config = replace(CONFIG, scope_hash=self.source.identity_hash)
        self.run_cleanup(NOW, config)
        self.assertEqual(self.state.missing_since[other_source.identity_hash], NOW - timedelta(hours=1))
        self.assertEqual(self.files.checked_sources, [self.source])

    def test_unavailable_share_aborts_before_state_save_or_deletion(self):
        self.files.check_share = Mock(side_effect=RuntimeError("Share unavailable"))
        with self.assertRaisesRegex(RuntimeError, "Share unavailable"):
            self.run_cleanup(NOW)
        self.assertEqual(self.state.saves, 0)
        self.assertEqual(self.index.deleted, [])

    def test_storage_error_aborts_before_state_save_or_deletion(self):
        self.files.exists = Mock(side_effect=RuntimeError("Storage unavailable"))
        with self.assertRaisesRegex(RuntimeError, "Storage unavailable"):
            self.run_cleanup(NOW)
        self.assertEqual(self.state.saves, 0)
        self.assertEqual(self.index.deleted, [])

    def test_state_conflict_aborts_before_deletion(self):
        self.state.missing_since[self.source.identity_hash] = NOW - timedelta(hours=1)
        self.state.save = Mock(side_effect=RuntimeError("Concurrent state update"))
        with self.assertRaisesRegex(RuntimeError, "Concurrent state update"):
            self.run_cleanup(NOW)
        self.assertEqual(self.index.deleted, [])

    def test_partial_deletion_is_retried_on_a_later_run(self):
        self.state.missing_since[self.source.identity_hash] = NOW - timedelta(hours=1)
        original_delete = self.index.delete_chunks

        def partial_delete(chunk_ids):
            original_delete(chunk_ids[:1])
            raise RuntimeError("Search rejected a chunk.")

        self.index.delete_chunks = partial_delete
        with self.assertRaisesRegex(RuntimeError, "Search rejected"):
            self.run_cleanup(NOW)
        self.assertNotIn(self.source.identity_hash, self.state.missing_since)
        self.index.delete_chunks = original_delete
        self.run_cleanup(NOW + timedelta(minutes=5))
        self.assertEqual(self.index.deleted, ["chunk-1"])
        self.run_cleanup(NOW + timedelta(minutes=10))
        self.assertEqual(self.index.deleted, ["chunk-1", "chunk-2"])

    def test_deleted_then_reused_path_requires_a_new_confirmation(self):
        self.state.missing_since[self.source.identity_hash] = NOW - timedelta(hours=1)
        self.run_cleanup(NOW)
        self.assertNotIn(self.source.identity_hash, self.state.missing_since)
        self.index.chunks.append(IndexedChunk("new-chunk", URL, "admin-share"))
        report = self.run_cleanup(NOW + timedelta(minutes=5))
        self.assertEqual(report.eligible_files, 0)
        self.assertEqual(self.index.deleted, ["chunk-1", "chunk-2"])

    def test_invalid_source_aborts_before_state_save_or_deletion(self):
        self.index.chunks.append(IndexedChunk("bad", "https://other.example/file", "admin-share"))
        with self.assertRaises(ValueError):
            self.run_cleanup(NOW)
        self.assertEqual(self.state.saves, 0)
        self.assertEqual(self.index.deleted, [])

    def test_duplicate_chunk_key_aborts(self):
        self.index.chunks.append(CHUNKS[0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_cleanup(NOW)
        self.assertEqual(self.state.saves, 0)
        self.assertEqual(self.index.deleted, [])

    def test_unindexed_pending_entry_is_removed(self):
        self.state.missing_since["f" * 64] = NOW - timedelta(hours=1)
        self.run_cleanup(NOW)
        self.assertNotIn("f" * 64, self.state.missing_since)


class SourceValidationTests(unittest.TestCase):
    def test_decodes_spaces_and_keeps_paths(self):
        source = parse_source(CHUNKS[0], CONFIG.account_name, CONFIG.shares)
        self.assertEqual(source.path, "folder/a file.pdf")
        self.assertEqual(source.share, "admin-share")
        self.assertEqual(len(source.identity_hash), 64)

    def test_same_path_with_different_encoding_has_same_hash(self):
        other = replace(CHUNKS[0], source_url=URL.replace("%20", " "))
        self.assertEqual(
            parse_source(CHUNKS[0], CONFIG.account_name, CONFIG.shares).identity_hash,
            parse_source(other, CONFIG.account_name, CONFIG.shares).identity_hash,
        )

    def test_rejects_mismatched_or_unsafe_paths(self):
        urls = [
            "https://other.file.core.windows.net/admin-share/a.pdf",
            "http://shareiarag.file.core.windows.net/admin-share/a.pdf",
            "https://shareiarag.file.core.windows.net/admin-share/a.pdf?token=secret",
            "https://shareiarag.file.core.windows.net/admin-share/../a.pdf",
            "https://shareiarag.file.core.windows.net/admin-share/a%2Fb.pdf",
            "https://shareiarag.file.core.windows.net/admin-share/a%zz.pdf",
            "https://shareiarag.file.core.windows.net/admin-share//a.pdf",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_source(replace(CHUNKS[0], source_url=url), CONFIG.account_name, CONFIG.shares)

    def test_rejects_unmanaged_share_and_share_mismatch(self):
        with self.assertRaises(ValueError):
            parse_source(replace(CHUNKS[0], share="alluser-share"), CONFIG.account_name, CONFIG.shares)
        with self.assertRaises(ValueError):
            parse_source(CHUNKS[0], CONFIG.account_name, ("alluser-share",))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "SEARCH_ENDPOINT": "https://example.search.windows.net/",
            "SEARCH_INDEX_NAME": "af-index-all",
            "FILES_ACCOUNT_NAME": "shareiarag",
            "FILES_SHARE_NAMES": "admin-share;alluser-share",
        }

    def test_defaults_are_safe(self):
        config = Config.from_env(self.env)
        self.assertTrue(config.dry_run)
        self.assertEqual(config.confirmation_seconds, 300)
        self.assertEqual(config.search_endpoint, "https://example.search.windows.net")

    def test_invalid_configuration_fails_explicitly(self):
        for setting, value in [
            ("CLEANUP_DRY_RUN", "yes"),
            ("CLEANUP_CONFIRMATION_SECONDS", "59"),
            ("CLEANUP_SCOPE_SOURCE_SHA256", "invalid"),
            ("FILES_SHARE_NAMES", "admin-share;admin-share"),
            ("SEARCH_ENDPOINT", "http://example.search.windows.net"),
        ]:
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                Config.from_env({**self.env, setting: value})


class AzureFilesTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.share = self.service.get_share_client.return_value
        self.files = AzureFiles(self.service, CONFIG.shares)
        self.source = parse_source(CHUNKS[0], CONFIG.account_name, CONFIG.shares)

    def test_only_missing_file_or_parent_is_treated_as_absent(self):
        client = self.share.get_file_client.return_value
        for code in ("ResourceNotFound", "ParentNotFound"):
            error = ResourceNotFoundError("missing")
            error.error_code = code
            client.get_file_properties.side_effect = error
            self.assertFalse(self.files.exists(self.source))

        error = ResourceNotFoundError("share missing")
        error.error_code = "ShareNotFound"
        client.get_file_properties.side_effect = error
        with self.assertRaises(ResourceNotFoundError):
            self.files.exists(self.source)

    def test_other_errors_are_not_treated_as_missing(self):
        client = self.share.get_file_client.return_value
        client.get_file_properties.side_effect = RuntimeError("forbidden")
        with self.assertRaisesRegex(RuntimeError, "forbidden"):
            self.files.exists(self.source)

    def test_share_check_fetches_a_page_even_when_the_share_is_empty(self):
        pager = self.share.get_directory_client.return_value.list_directories_and_files.return_value
        pager.by_page.return_value = iter([iter([])])
        self.files.check_share("admin-share")
        pager.by_page.assert_called_once()


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = SearchIndex(CONFIG.search_endpoint, CONFIG.index_name, Mock())

    def test_paginates_with_unique_key_and_escapes_filter(self):
        self.index._post = Mock(side_effect=[
            {"value": [{"chunk_id": "a'b", "metadata_storage_path": URL, "share": "admin-share"}]},
            {"value": [{"chunk_id": "c", "metadata_storage_path": URL, "share": "admin-share"}]},
            {"value": []},
        ])
        rows = list(self.index.list_chunks())
        self.assertEqual([row.chunk_id for row in rows], ["a'b", "c"])
        self.assertEqual(self.index._post.call_args_list[1].args[1]["filter"], "chunk_id gt 'a''b'")
        self.assertTrue(all(call.kwargs["elevated"] for call in self.index._post.call_args_list))

    def test_invalid_response_fails(self):
        self.index._post = Mock(return_value={"value": [{"chunk_id": "a"}]})
        with self.assertRaisesRegex(ValueError, "missing"):
            list(self.index.list_chunks())

    def test_deletes_batches_and_checks_each_result(self):
        def result(operation, payload):
            self.assertEqual(operation, "index")
            return {"value": [{"key": item["chunk_id"], "status": True} for item in payload["value"]]}

        self.index._post = Mock(side_effect=result)
        keys = [f"chunk-{number}" for number in range(1001)]
        self.index.delete_chunks(keys)
        self.assertEqual([len(c.args[1]["value"]) for c in self.index._post.call_args_list], [1000, 1])

    def test_partial_delete_error_is_surfaced(self):
        self.index._post = Mock(return_value={
            "value": [{"key": "chunk-1", "status": True}, {"key": "chunk-2", "status": False}]
        })
        with self.assertRaisesRegex(RuntimeError, "failed to delete 1"):
            self.index.delete_chunks(["chunk-1", "chunk-2"])

    def test_incomplete_delete_response_is_rejected(self):
        self.index._post = Mock(return_value={"value": [{"key": "chunk-1", "status": True}]})
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.index.delete_chunks(["chunk-1", "chunk-2"])

    def test_initial_inventory_error_exposes_reason_without_token(self):
        self.index._credential.get_token.return_value.token = "secret-token"
        response = BytesIO(b'{"error":{"message":"Missing permission, secret-token"}}')
        error = HTTPError("https://example.search.windows.net", 400, "Bad Request", {}, response)
        with patch("cleanup.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                self.index._post("search", {"search": "*"}, elevated=True)
        self.assertIn("Missing permission, [redacted]", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))

    def test_later_page_error_does_not_log_source_key(self):
        self.index._credential.get_token.return_value.token = "secret-token"
        response = BytesIO(b'{"error":{"message":"sensitive-source-key"}}')
        error = HTTPError("https://example.search.windows.net", 400, "Bad Request", {}, response)
        with patch("cleanup.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                self.index._post("search", {"search": "*", "filter": "chunk_id gt 'key'"}, elevated=True)
        self.assertNotIn("sensitive-source-key", str(raised.exception))


class BlobStateTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.container = self.service.get_container_client.return_value
        self.blob = self.container.get_blob_client.return_value
        self.state = BlobState(self.service)

    def test_new_blob_starts_without_pending_files(self):
        error = ResourceNotFoundError("missing")
        error.error_code = "BlobNotFound"
        self.blob.download_blob.side_effect = error
        self.assertEqual(self.state.load(), StateSnapshot({}, None))

    def test_corrupt_state_is_not_silently_discarded(self):
        self.blob.download_blob.return_value.readall.return_value = b'{"version": 2}'
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.state.load()

    def test_reads_timestamps_and_updates_with_etag(self):
        identity_hash = "a" * 64
        download = self.blob.download_blob.return_value
        download.readall.return_value = json.dumps({
            "version": 1, "missing_since": {identity_hash: NOW.isoformat()}
        }).encode()
        download.properties.etag = "old-etag"
        self.container.create_container.side_effect = ResourceExistsError("already exists")
        snapshot = self.state.load()
        self.assertEqual(snapshot.missing_since[identity_hash], NOW)

        self.blob.upload_blob.return_value = {"etag": "new-etag"}
        self.assertEqual(self.state.save(snapshot.missing_since, snapshot.etag), "new-etag")
        kwargs = self.blob.upload_blob.call_args.kwargs
        self.assertEqual(kwargs["etag"], "old-etag")
        self.assertEqual(kwargs["match_condition"], MatchConditions.IfNotModified)

    def test_concurrent_blob_update_is_not_overwritten(self):
        self.blob.upload_blob.side_effect = ResourceModifiedError("etag changed")
        with self.assertRaises(ResourceModifiedError):
            self.state.save({}, "old-etag")

    def test_new_blob_creation_is_conditional(self):
        self.blob.upload_blob.return_value = {"etag": "created"}
        self.assertEqual(self.state.save({}, None), "created")
        self.assertFalse(self.blob.upload_blob.call_args.kwargs["overwrite"])


class FunctionRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import function_app

        cls.function_app = function_app
        cls.functions = function_app.app.get_functions()

    def test_timer_is_registered_once_every_five_minutes(self):
        functions = self.functions
        self.assertEqual(len(functions), 1)
        self.assertEqual(functions[0].get_function_name(), "reconcile_deleted_files")
        bindings = json.loads(functions[0].get_function_json())["bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["type"], "timerTrigger")
        self.assertEqual(bindings[0]["schedule"], "0 */5 * * * *")
        self.assertFalse(bindings[0]["runOnStartup"])
        self.assertTrue(bindings[0]["useMonitor"])

    def test_configuration_error_is_logged_and_reraised(self):
        timer = Mock(past_due=False)
        handler = self.functions[0].get_user_function()
        with patch.object(self.function_app.Config, "from_env", side_effect=ValueError("invalid setting")):
            with self.assertLogs(level="ERROR") as logs, self.assertRaisesRegex(ValueError, "invalid setting"):
                handler(timer)
        self.assertIn("Index cleanup failed: ValueError: invalid setting", logs.output[0])


if __name__ == "__main__":
    unittest.main()

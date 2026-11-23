import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError


SEARCH_API_VERSION = "2026-08-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
STATE_CONTAINER = "af-index-cleanup-state"
STATE_BLOB = "missing-sources.json"
PAGE_SIZE = 1000
DELETE_BATCH_SIZE = 1000


@dataclass(frozen=True)
class Config:
    search_endpoint: str
    index_name: str
    account_name: str
    shares: tuple[str, ...]
    dry_run: bool
    confirmation_seconds: int
    scope_hash: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        endpoint = env["SEARCH_ENDPOINT"].rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("SEARCH_ENDPOINT must be an HTTPS service endpoint.")
        index_name = env["SEARCH_INDEX_NAME"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", index_name):
            raise ValueError("SEARCH_INDEX_NAME is invalid.")
        account_name = env["FILES_ACCOUNT_NAME"]
        if not re.fullmatch(r"[a-z0-9]{3,24}", account_name):
            raise ValueError("FILES_ACCOUNT_NAME is invalid.")
        shares = tuple(part.strip() for part in env["FILES_SHARE_NAMES"].split(";"))
        if (
            not shares
            or len(set(shares)) != len(shares)
            or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", share) for share in shares)
        ):
            raise ValueError("FILES_SHARE_NAMES must contain distinct share names separated by semicolons.")
        dry_run_value = env.get("CLEANUP_DRY_RUN", "true").lower()
        if dry_run_value not in ("true", "false"):
            raise ValueError("CLEANUP_DRY_RUN must be true or false.")
        confirmation_seconds = int(env.get("CLEANUP_CONFIRMATION_SECONDS", "300"))
        if confirmation_seconds < 60:
            raise ValueError("CLEANUP_CONFIRMATION_SECONDS must be at least 60.")
        scope_hash = env.get("CLEANUP_SCOPE_SOURCE_SHA256") or None
        if scope_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", scope_hash):
            raise ValueError("CLEANUP_SCOPE_SOURCE_SHA256 must be a lowercase SHA-256 hash.")
        return cls(
            search_endpoint=endpoint,
            index_name=index_name,
            account_name=account_name,
            shares=shares,
            dry_run=dry_run_value == "true",
            confirmation_seconds=confirmation_seconds,
            scope_hash=scope_hash,
        )


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    source_url: str
    share: str


@dataclass(frozen=True)
class SourceFile:
    share: str
    path: str
    identity_hash: str


@dataclass(frozen=True)
class StateSnapshot:
    missing_since: dict[str, datetime]
    etag: str | None


@dataclass(frozen=True)
class CleanupReport:
    checked_files: int
    missing_files: int
    eligible_files: int
    deleted_files: int
    deleted_chunks: int
    dry_run: bool


class Index(Protocol):
    def list_chunks(self) -> Iterable[IndexedChunk]: ...

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None: ...


class Files(Protocol):
    def check_share(self, share: str) -> None: ...

    def exists(self, source: SourceFile) -> bool: ...


class State(Protocol):
    def load(self) -> StateSnapshot: ...

    def save(self, missing_since: Mapping[str, datetime], etag: str | None) -> str: ...


def parse_source(chunk: IndexedChunk, account_name: str, shares: Sequence[str]) -> SourceFile:
    parsed = urlsplit(chunk.source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != f"{account_name}.file.core.windows.net"
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("An indexed source URL does not belong to the configured Azure Files account.")
    raw_parts = parsed.path.split("/")
    if not raw_parts or raw_parts[0] != "" or len(raw_parts) < 3 or any(not p for p in raw_parts[1:]):
        raise ValueError("An indexed source URL has an invalid path.")
    if any(re.search(r"%(?![0-9a-fA-F]{2})", part) for part in raw_parts[1:]):
        raise ValueError("An indexed source URL has invalid percent encoding.")
    parts = [unquote(part, errors="strict") for part in raw_parts[1:]]
    if any(part in (".", "..") or "/" in part or "\\" in part or "\0" in part for part in parts):
        raise ValueError("An indexed source URL has an unsafe path.")
    share, path_parts = parts[0], parts[1:]
    if share not in shares or chunk.share != share:
        raise ValueError("An indexed source does not match an allowed share.")
    path = "/".join(path_parts)
    identity = f"{account_name}\0{share}\0{path}".encode("utf-8")
    return SourceFile(share, path, hashlib.sha256(identity).hexdigest())


def reconcile(
    config: Config,
    index: Index,
    files: Files,
    state: State,
    now: datetime,
) -> CleanupReport:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("The cleanup time must include a timezone.")
    now = now.astimezone(timezone.utc)
    snapshot = state.load()
    for share in config.shares:
        files.check_share(share)

    sources: dict[str, tuple[SourceFile, list[str]]] = {}
    seen_chunks: set[str] = set()
    for chunk in index.list_chunks():
        if not chunk.chunk_id or chunk.chunk_id in seen_chunks:
            raise ValueError("The index returned an empty or duplicate chunk key.")
        seen_chunks.add(chunk.chunk_id)
        source = parse_source(chunk, config.account_name, config.shares)
        if config.scope_hash and source.identity_hash != config.scope_hash:
            continue
        if source.identity_hash not in sources:
            sources[source.identity_hash] = (source, [])
        previous_source, chunk_ids = sources[source.identity_hash]
        if previous_source != source:
            raise ValueError("Different source files produced the same identity hash.")
        chunk_ids.append(chunk.chunk_id)

    pending = dict(snapshot.missing_since) if config.scope_hash else {}
    if config.scope_hash:
        pending.pop(config.scope_hash, None)
    missing: dict[str, tuple[SourceFile, list[str]]] = {}
    for identity_hash, (source, chunk_ids) in sources.items():
        if not files.exists(source):
            missing[identity_hash] = (source, chunk_ids)
            pending[identity_hash] = snapshot.missing_since.get(identity_hash, now)

    eligible = [
        (identity_hash, source, chunk_ids)
        for identity_hash, (source, chunk_ids) in missing.items()
        if identity_hash in snapshot.missing_since
        and now - snapshot.missing_since[identity_hash] >= timedelta(seconds=config.confirmation_seconds)
    ]
    state_etag = state.save(pending, snapshot.etag)

    deleted_files = 0
    deleted_chunks = 0
    if not config.dry_run:
        for identity_hash, source, chunk_ids in eligible:
            # A reused path or partial failure must start a new confirmation cycle.
            pending.pop(identity_hash, None)
            state_etag = state.save(pending, state_etag)
            files.check_share(source.share)
            if files.exists(source):
                continue
            index.delete_chunks(chunk_ids)
            deleted_files += 1
            deleted_chunks += len(chunk_ids)
    return CleanupReport(
        checked_files=len(sources),
        missing_files=len(missing),
        eligible_files=len(eligible),
        deleted_files=deleted_files,
        deleted_chunks=deleted_chunks,
        dry_run=config.dry_run,
    )


class SearchIndex:
    def __init__(self, endpoint: str, index_name: str, credential) -> None:
        self._url = f"{endpoint}/indexes/{quote(index_name, safe='')}/docs"
        self._credential = credential

    def _post(self, operation: str, payload: dict, elevated: bool = False) -> dict:
        token = self._credential.get_token(SEARCH_SCOPE).token
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if elevated:
            headers["x-ms-enable-elevated-read"] = "true"
        url = f"{self._url}/{operation}?api-version={SEARCH_API_VERSION}"
        request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=60) as response:
                result = json.load(response)
        except HTTPError as error:
            message = f"Azure AI Search {operation} failed with HTTP {error.code}."
            if operation == "search" and "filter" not in payload:
                try:
                    details = json.load(error)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    message += " The service returned an invalid error response."
                else:
                    service_error = details.get("error") if isinstance(details, dict) else None
                    detail = service_error.get("message") if isinstance(service_error, dict) else None
                    if isinstance(detail, str) and detail:
                        message += " " + " ".join(detail.replace(token, "[redacted]").split())[:600]
                    else:
                        message += " The service did not provide an error message."
            raise RuntimeError(message) from error
        except URLError as error:
            raise RuntimeError(f"Azure AI Search {operation} could not be reached.") from error
        if not isinstance(result, dict):
            raise ValueError("Azure AI Search returned an invalid response.")
        return result

    def list_chunks(self) -> Iterable[IndexedChunk]:
        last_key = None
        while True:
            query = {
                "search": "*",
                "select": "chunk_id,metadata_storage_path,share",
                "orderby": "chunk_id asc",
                "top": PAGE_SIZE,
            }
            if last_key is not None:
                escaped_key = last_key.replace("'", "''")
                query["filter"] = f"chunk_id gt '{escaped_key}'"
            result = self._post("search", query, elevated=True)
            rows = result.get("value")
            if not isinstance(rows, list):
                raise ValueError("Azure AI Search returned an invalid result page.")
            if not rows:
                return
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Azure AI Search returned an invalid chunk.")
                values = [row.get(name) for name in ("chunk_id", "metadata_storage_path", "share")]
                if any(not isinstance(value, str) or not value for value in values):
                    raise ValueError("An indexed chunk is missing its key, source URL, or share.")
                yield IndexedChunk(*values)
            next_key = rows[-1]["chunk_id"]
            if next_key == last_key:
                raise ValueError("Azure AI Search pagination did not advance.")
            last_key = next_key

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        for offset in range(0, len(chunk_ids), DELETE_BATCH_SIZE):
            batch = chunk_ids[offset : offset + DELETE_BATCH_SIZE]
            result = self._post(
                "index",
                {"value": [{"@search.action": "delete", "chunk_id": key} for key in batch]},
            )
            values = result.get("value")
            if not isinstance(values, list) or len(values) != len(batch):
                raise ValueError("Azure AI Search returned an incomplete deletion result.")
            returned_keys = {item.get("key") for item in values if isinstance(item, dict)}
            if returned_keys != set(batch):
                raise ValueError("Azure AI Search returned unexpected deletion keys.")
            failed = [item for item in values if item.get("status") is not True]
            if failed:
                raise RuntimeError(f"Azure AI Search failed to delete {len(failed)} chunk(s).")


class AzureFiles:
    def __init__(self, service, shares: Sequence[str]) -> None:
        self._shares = {share: service.get_share_client(share) for share in shares}

    def check_share(self, share: str) -> None:
        root = self._shares[share].get_directory_client("")
        pages = root.list_directories_and_files(results_per_page=1).by_page()
        next(iter(pages), None)

    def exists(self, source: SourceFile) -> bool:
        try:
            self._shares[source.share].get_file_client(source.path).get_file_properties()
            return True
        except ResourceNotFoundError as error:
            if error.error_code in ("ResourceNotFound", "ParentNotFound"):
                return False
            raise


class BlobState:
    def __init__(self, service) -> None:
        self._container = service.get_container_client(STATE_CONTAINER)
        self._blob = self._container.get_blob_client(STATE_BLOB)

    def load(self) -> StateSnapshot:
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass
        try:
            download = self._blob.download_blob()
            payload = json.loads(download.readall())
        except ResourceNotFoundError as error:
            if error.error_code == "BlobNotFound":
                return StateSnapshot({}, None)
            raise
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("missing_since"), dict)
        ):
            raise ValueError("The cleanup state is invalid.")
        missing_since = {}
        for identity_hash, value in payload["missing_since"].items():
            if not isinstance(identity_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", identity_hash):
                raise ValueError("The cleanup state contains an invalid source hash.")
            if not isinstance(value, str):
                raise ValueError("The cleanup state contains an invalid timestamp.")
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("The cleanup state timestamp must include a timezone.")
            missing_since[identity_hash] = timestamp.astimezone(timezone.utc)
        return StateSnapshot(missing_since, download.properties.etag)

    def save(self, missing_since: Mapping[str, datetime], etag: str | None) -> str:
        payload = {
            "version": 1,
            "missing_since": {key: value.isoformat() for key, value in missing_since.items()},
        }
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        if etag is None:
            result = self._blob.upload_blob(data, overwrite=False)
        else:
            result = self._blob.upload_blob(
                data,
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        new_etag = result.get("etag")
        if not isinstance(new_etag, str) or not new_etag:
            raise ValueError("The cleanup state did not return an ETag.")
        return new_etag

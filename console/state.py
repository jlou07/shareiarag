import copy
import json
import logging
from datetime import datetime, timezone

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError
from azure.storage.blob import BlobServiceClient


class StateError(RuntimeError):
    pass


class StateConflict(StateError):
    pass


class LeaseLost(StateError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def initial_state():
    return {"version": 1, "operations": [], "drafts": [], "audit": []}


def validate_state(state):
    if not isinstance(state, dict) or state.get("version") != 1 or any(
        not isinstance(state.get(key), list) for key in ("operations", "drafts", "audit")
    ):
        raise StateError("Console state has an unsupported structure.")
    return state


class BlobState:
    def __init__(self, settings, credential):
        self.service = BlobServiceClient(
            f"https://{settings.state_account}.blob.core.windows.net",
            credential=credential, connection_timeout=10, read_timeout=20,
            retry_total=1,
        )
        self.container = self.service.get_container_client(settings.state_container)
        self.blob = self.container.get_blob_client("state.json")
        self.initialized = False

    def _ensure(self):
        if self.initialized:
            return
        try:
            self.blob.upload_blob(json.dumps(initial_state()), overwrite=False)
        except ResourceExistsError:
            pass
        self.initialized = True

    def read(self):
        try:
            self._ensure()
            download = self.blob.download_blob()
            state = json.loads(download.readall())
        except AzureError as error:
            logging.error("Console state read failed: %s.", type(error).__name__)
            raise StateError("Console state storage is unavailable. Check container RBAC.") from error
        except (ValueError, UnicodeDecodeError) as error:
            logging.error("Console state contains invalid JSON.")
            raise StateError("Console state is corrupt; no action was performed.") from error
        return validate_state(state)

    def update(self, change):
        for _ in range(3):
            try:
                self._ensure()
                download = self.blob.download_blob()
                state = validate_state(json.loads(download.readall()))
                result = change(state)
                self.blob.upload_blob(
                    json.dumps(state, ensure_ascii=True), overwrite=True,
                    etag=download.properties.etag, match_condition=MatchConditions.IfNotModified,
                )
                return result
            except ResourceModifiedError:
                continue
            except AzureError as error:
                logging.error("Console state update failed: %s.", type(error).__name__)
                raise StateError("Console state could not be saved; refresh before retrying.") from error
            except (ValueError, UnicodeDecodeError) as error:
                raise StateError("Console state is corrupt; no action was performed.") from error
        raise StateConflict("Another administrator changed the console state. Refresh and retry.")

    def claim(self, operation_id):
        blob = self.container.get_blob_client(f"locks/{operation_id}")
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        return blob.acquire_lease(lease_duration=60)


class MemoryState:
    """Explicit test dependency; never selected by application configuration."""

    def __init__(self):
        self.data = initial_state()

    def read(self):
        return copy.deepcopy(self.data)

    def update(self, change):
        candidate = copy.deepcopy(self.data)
        result = change(candidate)
        self.data = candidate
        return result

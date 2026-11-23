import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from azure.storage.fileshare import ShareServiceClient

from cleanup import AzureFiles, BlobState, Config, SearchIndex, reconcile


app = func.FunctionApp()
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


@app.function_name(name="reconcile_deleted_files")
@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def reconcile_deleted_files(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("The index cleanup timer is past due.")
    try:
        config = Config.from_env(os.environ)
        with (
            ManagedIdentityCredential() as credential,
            ShareServiceClient(
                account_url=f"https://{config.account_name}.file.core.windows.net",
                credential=credential,
                token_intent="backup",
            ) as file_service,
            BlobServiceClient.from_connection_string(os.environ["AzureWebJobsStorage"]) as blob_service,
        ):
            report = reconcile(
                config,
                SearchIndex(config.search_endpoint, config.index_name, credential),
                AzureFiles(file_service, config.shares),
                BlobState(blob_service),
                datetime.now(timezone.utc),
            )
    except AzureError as error:
        logging.error(
            "Index cleanup failed: %s (Azure error code: %s).",
            type(error).__name__,
            getattr(error, "error_code", None),
        )
        raise
    except (KeyError, RuntimeError, ValueError) as error:
        logging.error("Index cleanup failed: %s: %s", type(error).__name__, error)
        raise
    logging.info(
        "Index cleanup: checked_files=%d missing_files=%d eligible_files=%d "
        "deleted_files=%d deleted_chunks=%d dry_run=%s",
        report.checked_files,
        report.missing_files,
        report.eligible_files,
        report.deleted_files,
        report.deleted_chunks,
        report.dry_run,
    )

import logging
import os

import azure.functions as func
import requests
from azure.identity import ManagedIdentityCredential

from rebuild import Rebuilder, SearchApi


app = func.FunctionApp()
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


@app.function_name(name="rebuild_search_index")
@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def rebuild_search_index(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("The index rebuild timer is past due.")
    with requests.Session() as session, ManagedIdentityCredential() as credential:
        api = SearchApi(session, os.environ["SEARCH_ENDPOINT"], credential=credential)
        try:
            report = Rebuilder(api).run()
        except Exception as error:
            logging.error("Index rebuild failed; the active index was kept: %s: %s",
                          type(error).__name__, error)
            raise
    logging.info(
        "Index rebuild: switched %s -> %s, items=%s, chunks %d -> %d.",
        report.previous_index, report.new_index, report.items_processed,
        report.previous_chunks, report.new_chunks,
    )

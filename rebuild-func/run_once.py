"""Run one rebuild locally with the Azure CLI identity (no Search API key)."""

import logging
from pathlib import Path

import requests
from azure.identity import AzureCliCredential
from dotenv import dotenv_values

from rebuild import Rebuilder, SearchApi


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
    with requests.Session() as session:
        api = SearchApi(session, env["SEARCH_ENDPOINT"], credential=AzureCliCredential())
        report = Rebuilder(api).run()
    print(f"Switched {report.previous_index} -> {report.new_index}; items={report.items_processed}; "
          f"chunks {report.previous_chunks} -> {report.new_chunks}")


if __name__ == "__main__":
    main()

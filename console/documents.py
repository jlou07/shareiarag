import logging
from concurrent.futures import ThreadPoolExecutor

from azure.core.exceptions import AzureError
from azure.storage.fileshare import ShareClient

from azure_gateway import AzureApiError
from source_connections import connection


def file_identity(item):
    return item["account"], item["share"], item["path"]


def source_files(row):
    connection_string, _ = connection(row["account"])
    files = []
    try:
        with ShareClient.from_connection_string(connection_string, row["share"],
                                               connection_timeout=10, read_timeout=20, retry_total=1) as client:
            def entries(directory):
                return directory, list(client.get_directory_client(directory).list_directories_and_files(include=["timestamps"]))

            directories = [""]
            with ThreadPoolExecutor(max_workers=8) as executor:
                while directories:
                    level, directories = directories, []
                    for directory, items in executor.map(entries, level):
                        for entry in items:
                            path = f"{directory}/{entry['name']}" if directory else entry["name"]
                            if entry["is_directory"]:
                                directories.append(path)
                            else:
                                modified = entry.get("last_modified")
                                files.append({**{key: row[key] for key in ("sourceKey", "account", "share", "resourceGroup")},
                                              "path": path, "modified": modified.isoformat() if modified else None})
    except AzureError as error:
        logging.error("Azure Files inventory failed: %s.", type(error).__name__)
        raise AzureApiError("File inventory is unavailable. Check the server connection and Azure Files access.") from error
    return sorted(files, key=file_identity)


def paginate(files, offset, limit):
    return {"total": len(files), "offset": offset, "limit": limit, "files": files[offset:offset + limit]}

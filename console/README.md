# RAG Control

Administrative web console for the existing Azure Files / AI Search / Foundry
application. The interface and application-generated errors are in English.
No demonstration data or unauthenticated development mode is included.

## What it manages

- Live resource topology, active Search slot and index statistics.
- One subscription, with cascading RG / storage account / share filters across
  all connected sources. Group and incremental schedule settings are per share.
- Confirmed connections of existing SMB shares in additional accounts/RGs.
- Existing source/share/group associations and exact indexer execution results.
- Unique file counts and paginated metadata lists, globally and per share.
- Confirmed incremental indexing and verified schedule changes.
- Confirmed source/group changes through durable maintenance operations.
- Scheduled or manual purges that delete only the index chunks of deleted source
  files (seconds), with an automatic blue/green rebuild fallback.
- Azure Monitor metrics, model 429s and aggregate file-opening Application
  Insights latency/HTTP counts. Missing data is displayed as unavailable, not zero.
- Sanitized integration settings and a private, persistent administration audit.
- **MCP · Copilot** tab: live status of the hosted MCP server (Web App state,
  `/healthz`, anonymous `/mcp` must be 401, non-secret `MCP_*` settings), a
  confirmed and audited Start/Stop button, and the step-by-step setup (Azure →
  Teams Developer Portal SSO registration → Microsoft 365 admin center connector →
  end user). Secret settings are never returned; only whether the legacy client
  secret is still present.
- **Foundry agent** tab: read-only status of the Foundry account, project,
  knowledge-base MCP connection, model deployments and agent, plus the
  publication steps to Microsoft 365 Copilot and Teams. Starting/stopping the MCP
  server requires Website Contributor on it; reading agents requires a Foundry
  project role (Azure AI User). Missing rights are shown per section.

Creating Azure storage accounts or shares, changing deployed AI models, editing
group membership, enforcing NTFS ACLs and reading document contents are not
implemented by this console. These are not presented as working controls.

## Authentication

Use a single-tenant Entra SPA/API registration:

- SPA redirect URI: `https://<console-host>/auth/callback`.
- For local development, separately register
  `http://localhost:8000/auth/callback`.
- Expose `api://<client-id>/Console.Access`, with the required consent.
- Set `api.requestedAccessTokenVersion` to `2`.
- Set `ADMIN_OBJECT_IDS` to explicitly authorized user object IDs.

MSAL Browser uses authorization code + PKCE, with no client secret. The backend
verifies the RS256 signature, tenant-specific issuer, audience, required
timestamps, delegated scope and administrator object ID on every protected
request. Being a member of a document-access group does **not** make a user a
console administrator. Forwarded EasyAuth identity headers are not trusted.
Mutations require JSON, a same-origin request and explicit confirmation.

The browser bundle includes MSAL locally; no CDN scripts are loaded. All Azure
management calls use server-side credentials: managed identity on App Service,
Azure CLI credentials locally. There are no Search/storage administrator keys
in the browser, source, deployment parameters or state.

### Accepted risks

- **Share-level, not NTFS, permissions.** Each Azure Files share maps to one
  Entra group (`SHARE_GROUPS` / index `group_ids`). Per-folder or per-file NTFS
  ACLs inside a share are not enforced by search, Copilot or the download
  Function; split sensitive content into separate shares.
- **Administrator delegated Search token.** Elevated inventory and purge use the
  administrator's own Search token, so administrators hold the `Index Purger` and
  `Delegated Inventory Reader` roles directly. Any app the administrator signs in
  to with Search delegation (including the MCP/Copilot connector) receives a token
  carrying those rights. Prefer PIM (just-in-time) activation for these roles and a
  separate non-administrator account for day-to-day Copilot use.
- **Public endpoints.** The console, Key Vault and MCP server are reachable from
  the internet (the MCP server must be, for Microsoft 365). They rely on Entra
  authentication; network isolation would require VNet integration and private
  endpoints.
- **Function app settings write.** The console identity can write the download
  Function's app settings (to publish `SHARE_GROUPS` and account-key references).
  `function_app.py` refuses all requests if App Service Authentication is turned
  off, so that permission cannot be used to forge user identities.
- **Shared key on the Azure Files account.** `shareiarag` keeps shared-key access
  because the Azure Files indexer data sources and the short-lived file SAS links
  require it. The key lives only in Key Vault (`files-connection-shareiarag`) and
  the Search data sources; rotate it by switching both to the other key before
  regenerating the old one.
- **Group assignment on the MCP and download apps.** Both enterprise apps require
  assignment and only the share groups are assigned. When you add a share group
  to `SHARE_GROUPS`, also assign it to both enterprise apps, or its members are
  refused at sign-in.

## Configuration

Required environment variables:

| Variable | Purpose |
| --- | --- |
| `AZURE_SUBSCRIPTION_ID` | Managed subscription UUID |
| `AZURE_TENANT_ID` | Single Entra tenant UUID |
| `ENTRA_CLIENT_ID` | SPA/API application UUID |
| `ADMIN_OBJECT_IDS` | Comma-separated authorized user object UUIDs |
| `HOSTING_PLAN_ID` | Existing App Service plan resource ID |

Optional variables default to the current application's resource names:
`AZURE_RESOURCE_GROUP`, `SEARCH_SERVICE_NAME`, `SEARCH_API_VERSION`,
`KNOWLEDGE_SOURCE_NAME`, `KNOWLEDGE_BASE_NAME`, `FILES_ACCOUNT_NAME`,
`FOUNDRY_ACCOUNT_NAME`, `DOWNLOAD_APP_NAME`, `STATE_ACCOUNT_NAME`,
`STATE_CONTAINER_NAME`, `MCP_APP_NAME`, `FOUNDRY_PROJECT_NAME`,
`FOUNDRY_AGENT_NAME`. `AUTH_MODE` can only be `entra`.

`GROUP_DIRECTORY_ENABLED=false` is the application default. In this mode, the
console offers only group IDs already configured in the application's skillsets,
without inventing their names. To search existing security groups by name and
select another group, grant the console App Service's **managed identity** the
Microsoft Graph **application** permission `Group.ReadBasic.All`, with tenant
administrator approval, and set `GROUP_DIRECTORY_ENABLED=true`. This reads
basic properties of all tenant groups but does not grant access to group members.
The deployment parameter `groupDirectoryEnabled` in
[main.parameters.json](../infra/main.parameters.json) preserves this setting on
redeployment; the deployment helper does not grant the Graph permission. If the
managed identity is recreated, its permission must be granted again.

Both share connection and group-change forms search by the beginning of a group
name and can load further results. A complete group GUID also works as a search.
Only security-enabled groups can be selected. The console does not create groups
or manage members, and applying a new association retains the existing
maintenance and access-verification requirements.

Minimum server permissions are scoped Reader/Monitoring Reader, Search Service
Contributor, Index Inventory Reader as described above, the Search-scoped
**ShareIARAG Console Index Purger** custom role (document read, elevated read and
document delete/write through `docs/index`; the console only sends `delete`
actions), Blob Data Contributor on the **console container only**, read access
to the existing hosting plan, and the reviewed downloader configuration role.
Search Index Data Contributor is not required. File metadata enumeration uses
the existing reviewed account connection, without reading file contents.
New account connections require the server-side storage credentials described
below; the console never calls `listKeys` or grants itself additional roles.

## Global scope and additional shares

The top filters select **connected** sources, not arbitrary resources in the
subscription. Changing a parent clears child selections. Identically named
shares in different accounts have distinct identities. Filters persist between
pages during navigation; reloading the application resets them. The reset
button restores the global view without reloading.

**Sources**, **Synchronization**, **Groups and access**, and the source counts and
topology in the overview follow the selection. Indexed chunk counts, Insights,
integration configuration, **Purges**, and **Maintenance** remain global and
are labelled accordingly. Rebuilds and permission changes still operate on the
common Search indexes; selecting one share never limits their impact.

### File counts, pause, and history

Counters come **only from the last jobs**; opening a page never lists Azure Files
or reads the Search index. Overview loads in a single request (resource sections
are read concurrently) and shows the sum of **Indexed files** recorded by the
latest jobs. Synchronization opens with an active/standby index summary showing
index names, their role in agent searches, and indexer counts within the selected
filters. Active and inactive indexers have visually distinct tables; a paused
refresh remains part of the active index. If no inactive indexers match the
filters, the summary explicitly shows "None in view".
Synchronization shows, per share:

- **LAST SYNC**: the number of new or modified files listed for the latest
  indexer run (the same list the counter opens). Azure's `itemsProcessed` is not
  shown because it can include unchanged files the indexer re-read; the count
  appears once the share is listed, about one minute after the run.
- **INDEXED**: the share's file count recorded by its latest job. When idle, the
  worker lists a share (at most once a minute, app-only) after a refresh that
  processed files, so INDEXED follows syncs about one minute after they end; a
  more recent purge takes precedence. Files deleted since then stay counted
  until the next purge removes them from the index.
- **PURGED**: every file purged from the share, summed over all completed purges.
  A targeted purge counts the files whose chunks it deleted from the index; a
  full rebuild counts files present in the Azure Files listing at the previous
  purge and absent at that one (the first rebuild only records the baseline).
- **FAILED**: Azure's `itemsFailed` for the latest indexer run.

A dash means no completed job recorded the value yet; it is never shown as zero.
Counters are links, including zeros (which open an empty list): LAST SYNC, INDEXED and PURGED open the paginated
file list recorded by the job (`/api/sources/files?sourceKey=&kind=lastSync|indexed|purged`,
409 when no job recorded it); FAILED opens Azure's error messages for the run.
A refresh that processed 0 files has a known empty LAST SYNC list. When it processed
files, its list is recorded about one minute after it ends; until then the number is
shown without a link.
LAST SYNC files are those new or modified since the previous listing (the first
listing uses the most recently modified files).
The purge worker lists the active shares with their stored connection (app-only,
no user token). A targeted purge fails without deleting anything if a share
cannot be listed; a full rebuild records the listing failure on the operation
and still completes. Only the latest listing is kept as
the baseline. Source cards resolve each configured group directly through
Microsoft Graph; unresolved names show an explicit error rather than a GUID.
Backend HTTPS connection pools are shared across short-lived executor threads, and
token acquisition is serialized per Azure audience.

**Pause** removes that share's incremental refresh schedule. It does not stop an
already-running indexer, disable explicitly confirmed manual refreshes, or exclude
the share from global purges. **Resume** restores a supported refresh interval;
the default after a reload is five minutes.

**Purges** contains the global schedule, rebuild history, and paginated lists of
files purged by each run. Purges without a recorded count are marked unavailable.
Files are removed from the active Search index, never from Azure Files.
**Maintenance** contains the concise non-purge operation log and administrative
audit, including existing recovery controls.
No Search or storage key is sent to the browser. Baseline listings and purge
lists are persisted in the existing console state, excluded from the general
state response, and exposed only through the protected paginated endpoint.

To connect an additional share:

1. Deploy the updated [download Function](../download-func/function_app.py) and
   publish the updated [agent instructions](../agent-instructions.txt). Build
   the Function package with
   `python infra/test/build_function_package.py --client-id <download-client-id> --output <new.zip>`:
   the source only contains zero-UUID placeholders and the script pins the
   tenant (from `az account`) and client ID into the package. Preserve
   the original `FILES_ACCOUNT_NAME` and `SHARE_GROUPS`; `FILES_ACCOUNT_KEY`
   is a Key Vault reference to `files-connection-<account>` resolved by the
   Function's system identity (the Function accepts either a bare key or that
   connection string). Only after deploying the compatible Function, set its
   app setting `MULTI_ACCOUNT_SOURCES_VERSION=1`. This explicit operator
   readiness marker prevents the console from assuming an old downloader
   supports new links.
2. Storage credentials live only in Key Vault `kv-shareiarag-e8e7` (RBAC,
   purge protection). For the primary account, [main.bicep](../infra/main.bicep)
   creates secret `files-connection-shareiarag` from `listKeys` inside Azure
   Resource Manager, references it from the console setting
   `FILES_CONNECTION_SHAREIARAG`, and grants **Key Vault Secrets User** on that
   secret only to the console and download Function managed identities. For
   another account, create secret `files-connection-<account>` with the full
   HTTPS connection string
   (`DefaultEndpointsProtocol=https;AccountName=<account>;AccountKey=<secret>;EndpointSuffix=core.windows.net`),
   grant both identities the same role on it, and add
   `FILES_CONNECTION_<ACCOUNT>=@Microsoft.KeyVault(VaultName=kv-shareiarag-e8e7;SecretName=files-connection-<account>)`
   to [app-service.bicep](../infra/modules/app-service.bicep) (manual settings are
   replaced on redeploy). The console publishes the same Key Vault reference,
   never the key, to the download Function. Azure Files indexers only accept
   account-key connection strings, so managed identity cannot replace this
   credential for indexing; Key Vault keeps it out of source, settings,
   the browser and console state.
3. Give the console identity Reader access to the subscription for account
   discovery, and read access to the relevant shares. Existing deployment RBAC
   remains limited to the application's original RG: these additional rights
   require separate review. Allow network access from Search to each account.
   No account/share or IAM assignment is created by the console.
4. Open **Sources → Connect a share**, choose RG, account, existing SMB
   share, verified group and incremental frequency, then confirm. The
   confirmation dialog shows pending checks and keeps rejection details visible;
   closing it does not cancel a request already sent. Additional group
   verification requires Graph consent if the group is not already known.
5. Follow the durable operation on **Maintenance**. It creates a
   Search data source and both slots' skillsets/indexers using the reviewed
   existing source as a template. It configures account-qualified file links,
   enables only the active indexer, starts indexing, verifies a successful
   execution with zero failed items, and
   only then enables that share's schedule on the active slot. New indexers
   are initially disabled so Azure cannot auto-index at creation before their
   download route is configured; the inactive indexer is enabled by a rebuild.

The storage connection goes directly from server configuration to Search.
For five-minute SAS links and the existing Office viewer behavior, the server
also writes `FILES_ACCOUNT_KEY_<ACCOUNT>` to the download Function's protected
app settings. These account-wide secrets must be governed and rotated in both
consumers; rotating only the console setting does not rotate existing Search or
Function connections. This does not claim per-share secret isolation.

New download routes are `/api/f/accounts/<account>/<share>/<path>`, with
`SHARE_GROUPS` keys `<account>/<share>`. Legacy routes and bare-share mappings
remain unchanged. Group maintenance uses the qualified identity so matching
share names in two accounts cannot overwrite each other's permissions.

Failed connections remain visible and block conflicting index/maintenance
actions until explicitly resumed. Steps preserve matching resources after
restart and reject conflicting configurations rather than overwrite them.
Successful indexing ends at `awaiting_access_verification` and is shown as
**Published** in the operation history. This confirms publication and indexing,
not end-to-end access: test authorized and unauthorized users and the deployed
agent's links before declaring access validated. No live deployment is performed
by the local build or tests.

## Development and verification

From the workspace root, using PowerShell:

```powershell
python -m venv console\.venv
.\console\.venv\Scripts\python.exe -m pip install -r console\requirements.txt
npm --prefix console ci
npm --prefix console run build
npm --prefix console test
.\console\.venv\Scripts\python.exe -m unittest discover -s console -p "test_*.py"
pwsh -NoProfile -File .\infra\test-deploy-console.ps1
```

Set the required environment variables, sign in with `az login`, ensure the
private state container already exists with appropriate RBAC, then:

```powershell
.\console\.venv\Scripts\python.exe console\app.py
```

Open `http://localhost:8000`. Real Entra authentication is still required.
`/healthz` only checks process liveness, not Azure connectivity. Protected API
responses, individual resource sections and worker status report failures
explicitly. Blob state is never replaced with an in-memory success-shaped
fallback. Tests inject their own fake gateway and state dependencies.

## Permission maintenance

Changing the group in a skillset alone does not change permissions already
stored on indexed chunks. The console therefore requires a preview, detects
configuration changes since that preview, and requires the typed confirmation
`REBUILD`. The API also accepts the previous `RECONSTRUIRE` confirmation for
compatibility with older clients.

**This is disruptive maintenance, not a zero-downtime switch.** It:

1. Persists an operation and a sanitized index schema in the private console
   container, pauses active schedules, and waits for indexers to become idle.
2. Removes the affected share from the download Function's allowed mapping.
3. Deletes both application indexes (`af-index-all`, `af-index-all-b`), including
   the old rollback contents. It does **not** delete Azure Files source files.
4. Updates the affected share's skillsets in both slots and recreates both
   indexes with permission filtering enabled. Vectorization uses the existing
   Search managed identity; administrator keys are not persisted.
5. Resets/runs both active source indexers, verifies successful new executions,
   failed-item counts and stable index statistics.
6. Publishes the new download group, restores prior active schedules and checks
   source/group consistency.

The operation ends as `awaiting_access_verification`, **not** as proof that all
end-user access paths have been tested. Test authorized and unauthorized users
in Search, Teams/Copilot and file opening. Existing SAS links can remain valid
for five minutes; cached or previous conversation content cannot be recalled.
NTFS rights are still not evaluated.

Operations survive browser closure and application restarts. A worker resumes
them from Blob state and uses a renewable blob lease to avoid concurrent steps
across instances. Errors leave the operation failed and visible; download
restrictions are not silently lifted. Correct the reported issue and explicitly
resume. A failed operation that never passed its initial snapshot can be
cancelled; once maintenance starts, use controlled recovery rather than an
unsafe automatic rollback. Do not edit the same Search/downloader resources
externally while a maintenance operation is active.

The same renewable control lease serializes manual indexer actions, maintenance
reservation and background steps; another administrator must retry a conflicting
action. Cancelling an unstarted job cannot be overwritten by a stale worker.

The downloader's app-settings update preserves other current settings and
verifies the resulting mapping. This ARM operation is not a distributed
transaction with Search. Changes can restart the downloader and have propagation
delay. The UI does not promise immediate revocation.

## Deleted-file purge (index rebuild)

Incremental indexer runs add or update files but never remove chunks of files
deleted from Azure Files. The **Purges** page offers a
**Global purge of deleted files** card: enable/disable a scheduled purge,
choose its frequency (every 1, 2, 3,
4, 6, 8 or 12 hours, or daily) and a reference hour in Europe/Paris wall-clock
time, or start a purge immediately. Runs occur at the reference hour and every
interval after it within each day (for example 02:00, 08:00, 14:00, 20:00 for
6 hours). The schedule is stored in the console state blob and evaluated by the
background worker every 10 seconds. Enabling or changing it never triggers an
immediate catch-up run; missed slots during downtime collapse into one run.
This is one global schedule, not a separately configurable purge per share.
The **Sources** and **Synchronization** pages show the same global purge
frequency next to each share's individual refresh frequency. If the schedule
is disabled, they show that status alongside the configured frequency. If the
schedule cannot be loaded, they show it as unavailable instead of assuming it is
disabled.

By default a purge is **targeted** and runs in one worker step (seconds rather
than a full reindex of several minutes), without re-embedding anything:

1. Reads the `metadata_storage_path` of every chunk of the active index
   (paginated by `chunk_id`, elevated read so permission filtering does not hide
   chunks), **before** listing the shares.
2. Lists every connected share (app-only). If any listing fails, the purge fails
   and nothing is deleted.
3. Deletes, in batches through `docs/index`, the chunks whose file is no longer
   in its share (case-insensitive path comparison) and the chunks of accounts or
   shares that are no longer connected sources. Chunks with an unparsable URL are
   kept and counted as skipped.

Only index chunks are deleted, never Azure Files files. The active index does not
change, so the inactive slot is not updated: it still holds the deleted files'
chunks, and rolling back to it would bring them back until the next purge.
If Azure rejects the elevated read or the deletion (HTTP 400, 401 or 403), the
same operation automatically continues as the full rebuild below and records
the reason (`fallbackReason`). `POST /api/rebuild/run` accepts
`{"mode": "rebuild"}` to force a full rebuild.

A full rebuild ([index_rebuild.py](index_rebuild.py)) is zero-downtime:

1. Records the active slot's incremental schedules and unschedules the inactive
   slot's indexers, then waits for them to become idle.
2. Recreates the inactive index empty from the sanitized active schema
   (permission filtering is verified; keys are not persisted).
3. Ensures the inactive skillsets/indexers exist (copied from the active slot
   if missing) and that each share's group matches between slots.
4. Resets and runs every connected source's inactive indexer and requires successful new
   executions with zero failed items.
5. Requires every source indexer to complete successfully with no failed items,
   then waits for stable, non-zero index statistics. A large reduction in chunks
   is allowed because deleted files can legitimately account for most of the
   active index.
6. Points the knowledge source at the rebuilt index (ETag-protected, verified),
   restores the recorded schedules on the new slot and unschedules the old one.

Every full rebuild re-embeds all source files (Azure OpenAI cost and indexing time).
The previous index is kept unqueried as a rollback target until the next
rebuild recreates it; it still contains the deleted files' chunks. Retrieval
may return old chunks for some seconds after the switch.

A failure before the switch leaves the agent on the untouched active index;
resuming restarts the rebuild from the beginning (a failed targeted purge resumes
as a targeted purge). A failure after the switch
(schedule move) blocks other operations until it is resumed. Rebuilds, source connections and
permission maintenance exclude each other, and indexer actions are refused
while a rebuild runs. Scheduled runs are audited with the actor `scheduler`.

## Hosting

The approved infrastructure is in [../infra/](../infra/). The console runs on the
Linux B2 plan `plan-shareiarag-shared` (rg-shareiarag), shared only with the MCP
Web App (see [../infra/mcp/](../infra/mcp/)). Always On and a single Gunicorn worker
with four threads keep persistent maintenance running with limited overhead.
Monitor the combined plan's memory after deployment.

Build the browser bundle before packaging. The deployment ZIP must contain only
the application Python modules, `requirements.txt` and `static/`, never
`.venv/`, `node_modules/`, environment files, tests or session artifacts.
Infrastructure creation and Entra registration require a separate deployment
approval; scaffolding and local tests do not deploy.

After approval, copy `infra/main.parameters.local.example.json` to the
Git-ignored `infra/main.parameters.local.json` (resource group, administrator
object IDs, download Function client ID), then run
`pwsh -NoProfile -File .\infra\deploy-console.ps1`. The subscription and tenant
come from the active Azure CLI account (`-SubscriptionId`/`-TenantId` pin them),
and the helper asks you to type the resource group name before changing
anything (`SHAREIARAG_CONFIRM_RG` answers non-interactively). It creates or
reuses the marked console registration, runs a real-client-ID what-if and
refuses deletes or changes to the existing plan/application before provisioning.
The helper preserves the permission-scope UUID on redeployment, creates a ZIP
with relative static paths and verifies the actual hosted hostname and API
protection. SCM basic publishing authentication remains disabled. Its isolated
test shadows all cloud/HTTP commands and never calls Azure.

Uploads are asynchronous and the helper tracks their exact deployment UUID to
completion, avoiding the synchronous gateway timeout observed during the first
Oryx build. Build acceptance alone is not considered success; public health and
authentication protection are checked after the build.

The audit retains the latest 500 events. It records actor object IDs, action,
target and operation IDs, not document contents, prompts, access tokens, keys,
SAS URLs or full application-settings responses. Treat the state container as
administrative data; access must remain private and narrowly scoped.

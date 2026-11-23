# Azure Files to Azure AI Search

Two pipelines index `admin-share` and `alluser-share` in storage account
`shareiarag`: text splitting, `text-embedding-3-small` embeddings (1536 dimensions),
vector search, and semantic configuration `sem`.
All Search requests use API version `2026-08-01-preview`.

## Azure resources (rg-shareiarag, France Central)

| Resource | Name | Details |
| --- | --- | --- |
| Storage | `shareiarag` | FileStorage Provisioned v2 (StandardV2_LRS), SMB shares |
| Azure AI Search | `srch-shareiarag` | Basic, semantic ranker (free plan), system-assigned managed identity |
| Foundry (AIServices) | `aif-shareiarag` | Project `proj-shareiarag` |
| Embedding deployment | `text-embedding-3-small` | GlobalStandard, 150K TPM |
| Chat / retrieval planning model | `gpt-5-mini` | GlobalStandard, capacity 10 |
| File opening service | `func-shareiarag-dl` | Entra-authenticated Azure Function |
| Monitoring | RAG Control Insights | Azure Monitor platform metrics and Function Application Insights; see [Monitoring](#monitoring) |

## Setup

- Install the VS Code **REST Client** extension (`humao.rest-client`).
- Fill in [.env](.env), next to [index-azurefiles.http](index-azurefiles.http).
  Use endpoints without trailing slashes. API keys are disabled on Search and
  Azure OpenAI: set `SEARCH_TOKEN` to a short-lived Entra token
  (`az account get-access-token --resource https://search.azure.com --query accessToken -o tsv`);
  embeddings use the Search managed identity. Only requests 1 and 8 need
  `FILES_CONNECTION_STRING` (Azure Files indexers require an account key): read it
  from Key Vault secret `files-connection-shareiarag` just for that run, then remove it.
  The Azure OpenAI deployment name can differ from the model name; update
  `AOAI_EMBEDDING_DEPLOYMENT` if necessary.
- Use SMB shares containing files with extractable text; NFS is not supported.
  The documentation mentions the Transaction optimized tier, but indexing has
  been verified here on Provisioned v2.
  Allow connectivity from Search to storage and Azure OpenAI.
  Enable the semantic ranker on the Search service for Foundry IQ.

No secrets are stored in the HTTP file: REST Client resolves them through
`{{$dotenv VARIABLE_NAME}}`. [.gitignore](.gitignore) excludes [.env](.env).
Do not share REST Client responses that might contain sensitive information.

## Execution

In [index-azurefiles.http](index-azurefiles.http), click **Send Request** above
each request, individually.

Use requests **1 and 8** (data sources), then **19 through 30**.
The old separate indexes and knowledge bases were deleted because they did not
enforce permissions; their requests were removed from the file.

### One index filtered by Entra group

One agent, one knowledge base, one index. Each chunk contains the GUID of the
Entra group allowed to access its share, in `group_ids`
(`permissionFilter: groupIds`, index `permissionFilterOption: enabled`).

| Share | Entra group | .env variable |
| --- | --- | --- |
| `admin-share` | AAD DC Administrators | `ADMIN_GROUP_ID` |
| `alluser-share` | AVD-GRP2 | `ALLUSER_GROUP_ID` |

| Step | Requests |
| --- | --- |
| Index `af-index-all` | 19 |
| admin skillset, indexer, status, run | 20, 21, 22, 23 |
| alluser skillset, indexer, status, run | 24, 25, 26, 27 |
| Content verification | 28 |
| Knowledge source `af-ks-all`, then knowledge base `af-kb-all` | 29, 30 |

Data sources 1 and 8 are reused. A ConditionalSkill injects the group GUID,
then a SplitSkill converts it to a one-element array.
Both `af-indexer-all-*` indexers run **every five minutes** (`PT5M`).
New files normally wait until the next run, plus extraction, embedding, indexing,
and any scheduling delay. Five minutes is not an end-to-end availability SLA.
Run requests 23 or 27 for an on-demand incremental run; do not reset just to add files.

Check `lastResult.status = "success"`, `lastResult.itemsFailed = 0`, and
`lastResult.errors` / `lastResult.warnings`. On the initial run, expect
`lastResult.itemsProcessed > 0`. An incremental run with no changes can normally
process zero items. Counts refer to source items, not necessarily chunks.
The top-level indexer `status` alone does not establish that a run is in progress.
Run requests normally return HTTP 202; check status afterward.

Verified permission behavior:
- Without a user token (admin key or service identity alone): zero results.
- With a member of AAD DC Administrators: only `admin-share` documents.
  Add `x-ms-query-source-authorization: <user token for https://search.azure.com>`.
  This header takes the raw token, **without** a `Bearer ` prefix.

Project MCP connection: `af-kb-all-mcp` (project managed identity).
Agent: `agent-shareiarag` (`gpt-5-mini`, tool `knowledge_base_retrieve`).
Knowledge-base retrieval reasoning is set to `medium` to expand query planning;
this trades higher LLM usage and latency for broader retrieval and is not an
exhaustiveness guarantee.
The agent must receive the caller's own identity for document-level filtering;
see [user delegation](https://learn.microsoft.com/azure/foundry/agents/concepts/user-delegation#on-behalf-of-user-access).
Verify permissions separately in each published channel rather than assuming
that a successful playground test validates Microsoft 365 Copilot or Teams.

## Local MCP connector prototype

The local VS Code prototype is configured in `.vscode/mcp.json` and implemented
in `mcp-proto/server.py`. It uses the current Azure CLI user token for local
permission-filtered tests.
Use `search` to locate files, then `fetch` for file contents. `fetch` returns up
to 20,000 characters per call; when `next_offset` is present, call it again with
the same `id` and that offset to read the next segment.

The separate hosted entry point, `mcp-proto/cloud_app.py`, requires
`MCP_MODE=hosted`, `SEARCH_ENDPOINT`, `MCP_PUBLIC_HOST`, `AZURE_TENANT_ID`,
`MCP_CLIENT_ID`, `MCP_TOKEN_AUDIENCES`, `MCP_ALLOWED_CLIENT_IDS`,
`MCP_REQUIRED_SCOPE`, and `MCP_FIC_IDENTITY_CLIENT_ID` (user-assigned managed
identity trusted by the Entra app through a federated credential). The deployed
app has no client secret; `MCP_CLIENT_SECRET` is only a fallback when no
federated identity is configured. The hosted service validates
Entra v2 delegated access tokens, exchanges each user's token on behalf of that
user for Search, and uses its managed identity for the Search service
authorization header. It never uses the local Azure CLI fallback or admin key.
Start it with `uvicorn cloud_app:app --host 0.0.0.0 --port 8000` from
`mcp-proto`; `/healthz` is an unauthenticated liveness endpoint.
This hosted flow still requires Entra configuration, Search RBAC, and live
two-user authorization tests before it can be considered deployment-ready.

The separate MCP App Service infrastructure is defined in `infra/mcp/main.bicep`.
Environment-specific values (Entra client IDs, audiences, deployer object ID,
optional traceability tags) go in the Git-ignored
`infra/mcp/main.parameters.local.json`; start from
`infra/mcp/main.parameters.local.example.json`. The Log Analytics workspace
defaults to the subscription's default France Central workspace.
It references the existing Search service, Key Vault and Log Analytics workspace;
the Entra application and its Key Vault secret must already be configured when
deploying. The Search role grants read and retrieval access at the Search
service scope and must be validated before pilot use. Application Insights is
provisioned, but the Python application is not yet instrumented.

## English responses and file links

[agent-instructions.txt](agent-instructions.txt) is the source for the agent's
instructions. Responses, headings, knowledge-base answer instructions, and
Function messages use English, even for French questions and source content.
Source files, their filenames, and group names are not translated.
For broad file-discovery requests, the agent searches with focused variants
across the user's language and English, including common synonyms and product
names. It lists distinct matching files returned by retrieval, but does not
claim the result is exhaustive beyond those returned documents.
The `fr.microsoft` analyzer is intentionally retained for the French documents;
changing the response language does not require rebuilding the index.

When no relevant evidence is found, the agent must respond with exactly:

> I couldn't find this information in the documents you can access.

It must not append a file heading, folder icon, source list, or placeholder.
Retrieval errors must instead be reported explicitly in English, also without
a file section; an operational failure is not evidence that no documents exist.
For a supported answer that refers to documents, the agent starts with a
Markdown table (one row per document: linked filename, key facts, citations).
It adds **Open files** only when at least one valid
source link exists, deduplicating by source document. Each link uses Markdown
(`- [<filename>](<https URL>)`). Keep citations before the file section and put
nothing after a link; this is the format previously confirmed clickable in
Copilot.

The source `metadata_storage_path` URL is also stored in `title`.
Foundry's built-in citations can point to the Search API and return HTTP 401
when opened directly. The separate file links point to
`https://func-shareiarag-dl.azurewebsites.net/api/f/<share>/<path>`.

The Function ([download-func/](download-func/)):
- requires Entra authentication through App Service Authentication;
- compares the user's `groups` claim with the group mapped to the share in
  `SHARE_GROUPS`, returning HTTP 403 if it does not match;
- redirects to a read-only SAS for that file, valid for five minutes;
- uses `Content-Disposition: inline`: text/Markdown displays as plain text,
  PDFs and images use the browser viewer;
- for Office files, returns a page that launches the desktop app in edit mode
  (`ms-word:ofe|u|file://<account>.file.core.windows.net/<share>/<path>`) over
  SMB, with an Office Online read-only fallback (`?mode=view` creates a fresh
  SAS and redirects straight to the viewer). Editing needs desktop Office,
  SMB (port 445) and the user's Entra Kerberos share/NTFS permissions.

The final SAS URL is a bearer link: anyone holding it can use it until expiry.
Office Online receives that temporary URL when opening Office documents.
The Function checks share-level groups, **not NTFS file or folder ACLs**.
Changes to group membership can require refreshing the authentication session.

The [administration console](console/README.md#global-scope-and-additional-shares)
can connect additional SMB shares from other accounts and resource groups in
the configured subscription. These use account-qualified links
`/api/f/accounts/<account>/<share>/<path>` and distinct access-group mappings;
the original two sources retain their existing links. Shares connected on the
default account (`FILES_ACCOUNT_NAME`) also open through the short
`/api/f/<share>/<path>` form, still checked against their own group. Additional connections
require reviewed server-side credentials, a compatible deployed downloader and
the updated agent instructions. The console keeps per-share schedules and
access settings separate from global index maintenance.

For direct SMB access, convert the source prefix
`https://shareiarag.file.core.windows.net/` to
`\\shareiarag.file.core.windows.net\` and replace remaining `/` with `\`.
This access requires SMB connectivity and the caller's SMB/NTFS permissions.

## Deleted-file purge

Files deleted from Azure Files **remain in the index** after incremental runs.
They are purged by a global purge scheduled (or started manually) in the
[administration console](console/README.md#deleted-file-purge-index-rebuild).
By default it deletes only the index chunks of files missing from their share
(a few seconds, no re-embedding; Azure Files files are never deleted). If Azure
refuses that permission, it falls back to a blue/green rebuild: the inactive
slot is rebuilt empty, validated, then the knowledge source is switched to it
and the incremental schedules move with it. The schedule is
disabled until an administrator enables it. There is one global purge schedule:
the console displays that same frequency for each share beside its independent
incremental refresh frequency.

### Deletion cleanup prototype (removed)

The code in [cleanup-func/](cleanup-func/) is an abandoned prototype. Azure AI
Search rejected its managed identity's elevated-read request because elevated
reads require a delegated user token. A separate read-only test also confirmed
that `x-ms-query-source-authorization` rejects a service principal token. Its
Function App and dedicated Flex Consumption plan were deleted; the code is kept
for reference only. The cleanup tests can be run with
`python -m unittest discover -s cleanup-func -p "test_*.py"`, but their mocked
results do not establish end-to-end Azure behavior. [rebuild-func/](rebuild-func/)
was never deployed; its logic now runs in the console worker.

### Isolated rebuild experiment

On 2026-11-11, a one-off test rebuilt both shares in a separate, empty index
with permission filtering enabled. The test indexers processed 31 admin and
21 alluser source items with no failures or warnings. The original and rebuilt
indexes each had 64 chunks once index statistics caught up. For the signed-in
admin-group user, all 31 visible source documents and their 37 chunks matched;
vector search also returned authorized results. Both indexes returned zero
results without a delegated user token, and this user could not read alluser
documents. A positive test with an alluser-group user was **not** performed.

To simulate a previously deleted file without deleting any Azure Files data,
three synthetic orphan chunks were inserted into a separate test index, using
a path that did not exist in the share. All three were absent from the rebuilt
index. This validates the simulated orphan scenario, not an actual
delete-and-rebuild test for a real source file.

A separate test knowledge base successfully retrieved the synthetic chunks,
then its knowledge source was updated to target the rebuilt index. Retrieval
still returned the old chunks 13 seconds after the update; they were absent at
25 and 36 seconds, and a subsequent query retrieved real documents. This is
one observation, not a propagation guarantee. Index statistics also initially
reported zero while documents were already searchable, so an immediate stats
check is not sufficient to validate a build. An [index alias cannot be used
as a knowledge-source target](https://learn.microsoft.com/azure/search/search-how-to-alias#limitations-and-considerations);
the experiment updated the isolated knowledge source directly.

All experiment-created indexes, indexers, skillsets, knowledge source, and
knowledge base were removed after the test. The active index, knowledge base,
five-minute indexer schedules, and source files were unchanged. This
experiment led to the console's scheduled rebuild described above.

## Microsoft 365 Copilot and Teams

The organization-scoped publication of **Document Assistant**, app version
`1.0.2`, was submitted through
`POST {project}/agents/agent-shareiarag/microsoft365/publish?api-version=v1`
and approved in the Microsoft 365 admin center (organization scope). An earlier
individual-scope publication used app version `1.0.1`. The Teams app IDs are
visible in the Microsoft 365 admin center under **Integrated apps**.

The endpoint uses `BotServiceTenant` for Microsoft 365 and Teams, allowing
signed-in users in the tenant to invoke the agent, and retains `Entra` for
direct Foundry clients. App assignments govern distribution; document access
still depends on the signed-in user's group permissions. Verify both invocation
and document filtering in Copilot with users from different groups, including a
user without a Foundry project role, before broader deployment.

The stable endpoint serves the latest agent version (`@latest`). New instructions
therefore go live without another Microsoft 365 approval; pin the active version
if changes must be reviewed before rollout. Updating published display metadata
is separate from changing agent instructions, and store caches or existing
conversations can retain older display text.

## Limitations

- Group mappings, not NTFS ACLs, govern indexed access. Update the group variables,
  skillsets (20/24), and Function settings together when changing a share's group.
  Reset and rerun the indexers to replace existing chunk permissions.
- Members of both groups can retrieve documents from both shares.
- Deleted source files remain in the index until the next full rebuild; incremental
  runs never remove them. Configure the scheduled purge in the
  [administration console](console/README.md#deleted-file-purge-index-rebuild).
- The Search API and permission-filtering features are in preview.

## Common errors

- **403**: check firewalls, the relevant key, and whether storage shared-key access
  is enabled. For the file Function, also check the caller's group.
- **Skillset / embedding error**: check the deployment name, Azure OpenAI endpoint,
  key, and availability of `text-embedding-3-small`.
- **Zero documents on the first run**: empty share, files without extractable text,
  or unsupported share tier; inspect detailed execution status.
- **429** (in Copilot: `tool_user_error` or an endless "Please wait"): Azure
  OpenAI rate limit. One question uses about 10k tokens, so `gpt-5-mini` runs
  at 500k tokens/min (capacity 500); at 10k only one question per minute passed.

The project identity has **Search Index Data Reader** on the Search service.
The Search identity has **Cognitive Services User** to call `gpt-5-mini`.
Because index read access applies across the service, do not add unfiltered
indexes that the project's agents could read.

## Administration console

[RAG Control](console/README.md) provides a French administrative interface for
the existing pipeline: live topology, active-slot discovery, indexing schedules,
source/group associations, platform metrics and a persistent audit. Its Entra
administrator allowlist is separate from document-access groups. Azure API calls
run server-side without exposing storage/Search keys or document contents.

Source/group changes require an explicit maintenance preview and typed
confirmation: both application indexes are recreated, both active sources are
reindexed, downloader permissions are coordinated, and end-user access tests
remain explicitly required. This is disruptive maintenance, not NTFS enforcement
or a guaranteed instantaneous revocation. No source files are deleted.

The console is hosted at
[RAG Control](https://app-shareiarag-console-e8e7.azurewebsites.net) on the
dedicated Linux B2 plan `plan-shareiarag-shared`, shared with the MCP Web App
`app-shareiarag-mcp-dev-68e5`. See
[console/README.md](console/README.md) for authentication, permissions, local
verification and operational limitations.

## Monitoring

The RAG Control **Insights** page queries Azure Monitor platform metrics for
Azure AI Search, Foundry, and the shared App Service plan. File-opening HTTP
request counts, errors, and response times come from the
`func-shareiarag-dl` Application Insights `requests` table. It does not use an
Azure Monitor workbook. Queries omit request URLs because they contain file
paths; no document content, prompts, tokens, or SAS URLs are collected.

Limitations:
- Platform metric retention is 93 days; Application Insights retention depends
  on the component configuration.
- Model time to response is per model call; one question triggers several
  calls, so it is not the end-to-end answer time seen in Teams or Copilot.

## Verification

Run the Function regression tests from [download-func/](download-func/):
`python -m unittest -v test_function_app`.
They verify English errors, denied access without a SAS, and unchanged inline
PDF and Office viewing behavior.

Test agent changes in a new conversation: a found invoice must have an English
answer and a valid file link; an unknown invoice or an inaccessible source must
have the English no-answer sentence with no file section. Also test a no-answer
turn after a prior answer with links to catch stale-link regressions.
For file discovery, test a broad topic using alternate-language terms and
confirm that every distinct matching file returned by retrieval is listed.

## References

- [Azure Files indexer](https://learn.microsoft.com/azure/search/search-file-storage-integration)
- [Index projections](https://learn.microsoft.com/azure/search/search-how-to-define-index-projections)
- [Azure OpenAI vectorizer](https://learn.microsoft.com/azure/search/vector-search-vectorizer-azure-open-ai)
- [Connect an agent to Foundry IQ](https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect)
- [Indexer schedules](https://learn.microsoft.com/azure/search/search-howto-schedule-indexers)
- [Publish to Copilot and Teams](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot)

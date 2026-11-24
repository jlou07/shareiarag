"""End-to-end deployment of one isolated ShareIARAG test environment.

  init   write a parameters file with a random suffix whose global names are all available.
  plan   show the steps and which ones are already done for a parameters file.
  run    execute the remaining steps; stops at the first error and resumes from there on the next run.
  sso    optional second pass for the Microsoft 365 Copilot connector (Teams Developer Portal SSO).

Every step that changes Azure or Entra runs only with --execute, after typing the resource group name once.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import bootstrap_search as test
import seed_test_secrets
import testenv


HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / "runtime.parameters.example.json"
STEPS = (
    ("entra", "Create the three Entra app registrations"),
    ("deploy", "Deploy the Bicep template (what-if guarded)"),
    ("entra-post", "Add redirect URIs, the MCP federated credential and the console group lookup permission"),
    ("secrets", "Create the Files and download secrets in Key Vault"),
    ("deploy-secrets", "Redeploy with access to the secrets"),
    ("publish", "Build and publish the console, MCP and Function packages"),
    ("fixtures", "Upload the two sample documents"),
    ("search", "Create the Search data sources, index, indexers and knowledge base"),
    ("foundry", "Create the Foundry connection and agent"),
    ("smoke", "Check that the three endpoints answer as expected"),
)
MANUAL_STEPS = """Manual follow-up:
  1. Optional Microsoft 365 Copilot connector: create an Entra SSO registration in the Teams Developer Portal
     for {mcp_url}, then run: python -B deploy_test_env.py sso --params {params} --app-id-uri <uri> --execute
  2. Test with both testers in the console ({console_url}) and in the Foundry agent of {project}.
     The Foundry Playground needs the Foundry User role on the project; grant it temporarily and remove it after.
  3. Run verify_mcp_access.py for the cross-access checks."""


def az(*arguments):
    return test._az(*arguments)


def az_json(*arguments):
    output = az(*arguments, "-o", "json")
    return json.loads(output) if output else None


def arm_post(path, body):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(body, handle)
    try:
        return az_json("rest", "--method", "post", "--url", "https://management.azure.com" + path,
                       "--body", "@" + handle.name)
    finally:
        os.unlink(handle.name)


def refresh_key_vault_references(resource_group, site, restart_command):
    subscription = az("account", "show", "--query", "id", "-o", "tsv")
    site_path = (f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
                 f"/providers/Microsoft.Web/sites/{site}")
    base_url = "https://management.azure.com" + site_path
    az("rest", "--method", "post", "--url",
       base_url + "/config/configreferences/appsettings/refresh?api-version=2022-03-01")
    az(*restart_command, "-g", resource_group, "-n", site)

    status_url = base_url + "/config/configreferences/appsettings?api-version=2023-12-01"
    deadline = time.monotonic() + 600
    while True:
        response = az_json("rest", "--method", "get", "--url", status_url)
        references = response.get("value", [])
        if not references:
            raise RuntimeError(f"No Key Vault app setting references were returned for {site}.")
        statuses = {
            item.get("name", "<unnamed>"): item.get("properties", {}).get("status", "Unknown")
            for item in references
        }
        if all(status == "Resolved" for status in statuses.values()):
            print(f"Key Vault references resolved for {site}.")
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Key Vault references for {site} did not resolve: {statuses}")
        print(f"Waiting for Key Vault references on {site}: {statuses}")
        time.sleep(20)


# ---------- init ----------

def names_available(subscription, env):
    checks = (
        ("Microsoft.Storage", "2023-05-01", env.files, "Microsoft.Storage/storageAccounts"),
        ("Microsoft.Storage", "2023-05-01", f"stshragstate{env.suffix}", "Microsoft.Storage/storageAccounts"),
        ("Microsoft.KeyVault", "2023-07-01", env.vault, "Microsoft.KeyVault/vaults"),
        ("Microsoft.Search", "2023-11-01", env.search, "searchServices"),
        ("Microsoft.Web", "2023-12-01", env.console_site, "Microsoft.Web/sites"),
        ("Microsoft.Web", "2023-12-01", env.mcp_site, "Microsoft.Web/sites"),
        ("Microsoft.Web", "2023-12-01", env.download_site, "Microsoft.Web/sites"),
    )
    for provider, version, name, kind in checks:
        result = arm_post(f"/subscriptions/{subscription}/providers/{provider}/checkNameAvailability"
                          f"?api-version={version}", {"name": name, "type": kind})
        if not (result.get("nameAvailable") or result.get("isNameAvailable")):
            return False
    return True


def build_parameters(suffix, args, deployer):
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    values = {
        "suffix": suffix, "resourceGroupName": args.resource_group, "location": args.location,
        "deployerObjectId": deployer, "administratorObjectIds": args.admins or [deployer],
        "testUserObjectIds": [args.tester_a, args.tester_b],
        "consoleClientId": "", "downloadClientId": "", "mcpClientId": "",
        "groupAObjectId": args.group_a, "groupBObjectId": args.group_b, "mcpSsoAudience": "",
    }
    for name, value in values.items():
        data["parameters"][name] = {"value": value}
    return data


def init(args):
    output = Path(args.output)
    if output.exists():
        raise RuntimeError(f"{output} already exists; refusing to overwrite it.")
    if az("group", "exists", "-n", args.resource_group) == "true":
        raise RuntimeError(f"Resource group {args.resource_group} already exists; choose a new one.")
    subscription, _ = test.account()
    deployer = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv")
    for _ in range(10):
        suffix = args.suffix or secrets.token_hex(2)
        env = testenv.TestEnv(suffix=suffix, resource_group=args.resource_group)
        if names_available(subscription, env):
            break
        if args.suffix:
            raise RuntimeError(f"Some global names for suffix {suffix} are already taken.")
    else:
        raise RuntimeError("No available suffix found after 10 attempts.")
    output.write_text(json.dumps(build_parameters(suffix, args, deployer), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output} with suffix {suffix} for {args.resource_group} in subscription {subscription}.")
    print(f"Next: python -B deploy_test_env.py run --params {output} --execute")


# ---------- state ----------

def state_path(params):
    return params.with_name(f".deploy-state-{testenv.load(params).suffix}.json")


def load_state(params):
    path = state_path(params)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(params, state):
    state_path(params).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------- steps ----------

class Runner:
    def __init__(self, params):
        self.params = params
        self.env = testenv.load(params)

    def reload(self):
        self.env = testenv.load(self.params)

    def script(self, *arguments):
        """Run a sibling script, answering its confirmation prompt with the resource group name."""
        variables = {**os.environ, testenv.PARAMS_ENV: str(self.params)}
        subprocess.run([sys.executable, "-B", *arguments], cwd=HERE, env=variables, check=True,
                       input=self.env.resource_group + "\n", text=True)

    def deployment(self, *extra):
        parameters = json.loads(self.params.read_text(encoding="utf-8"))["parameters"]
        common = ("-n", f"shareiarag-test-{self.env.suffix}", "-l", parameters["location"]["value"],
                  "-f", str(HERE / "main.bicep"), "-p", str(self.params), *extra, *preserved_settings(self.env))
        what_if = az_json("deployment", "sub", "what-if", *common, "--no-pretty-print")
        check_what_if(what_if, self.env.resource_group)
        print("What-if: " + ", ".join(f"{k} {v}" for k, v in summarize(what_if).items()))
        state = az("deployment", "sub", "create", *common, "--query", "properties.provisioningState", "-o", "tsv")
        if state != "Succeeded":
            raise RuntimeError(f"Deployment ended in state {state}.")

    def entra(self):
        self.script("create_entra_apps.py", "create", "--execute")
        self.reload()

    def deploy(self):
        self.deployment()

    def entra_post(self):
        self.script("create_entra_apps.py", "post-deploy", "--execute")

    def secrets(self):
        wait_for(lambda: az("keyvault", "secret", "list", "--vault-name", self.env.vault, "--query", "[].name",
                            "-o", "tsv") is not None, "Key Vault access (RBAC propagation)")
        existing = set(az("keyvault", "secret", "list", "--vault-name", self.env.vault,
                          "--query", "[].name", "-o", "tsv").split())
        if seed_test_secrets.files_secret(self.env) not in existing:
            self.script("seed_test_secrets.py", "files", "--execute")
        if seed_test_secrets.DOWNLOAD_SECRET not in existing:
            self.script("seed_test_secrets.py", "download", "--execute")

    def deploy_secrets(self):
        self.deployment("-p", "grantSecretAccess=true")
        refresh_key_vault_references(self.env.resource_group, self.env.console_site, ("webapp", "restart"))
        refresh_key_vault_references(self.env.resource_group, self.env.download_site, ("functionapp", "restart"))

    def publish(self):
        rg = self.env.resource_group
        with tempfile.TemporaryDirectory(prefix=f"shrag-{self.env.suffix}-") as out:
            out = Path(out)
            self.script("build_app_package.py", "console", "--output", str(out / "console.zip"))
            self.script("build_app_package.py", "mcp", "--output", str(out / "mcp.zip"))
            self.script("build_function_package.py", "--output", str(out / "download.zip"))
            for site, package in ((self.env.console_site, "console.zip"), (self.env.mcp_site, "mcp.zip")):
                publish_webapp(rg, site, out / package)
            az("functionapp", "deployment", "source", "config-zip", "-g", rg, "-n", self.env.download_site,
               "--src", str(out / "download.zip"), "--build-remote", "true", "--timeout", "900")
            print(f"Published {self.env.download_site}.")

    def fixtures(self):
        for share, name in (("admin-share", "admin-test.txt"), ("alluser-share", "alluser-test.txt")):
            az("storage", "file", "upload", "--account-name", self.env.files, "--share-name", share,
               "--source", str(HERE / "fixtures" / name), "--auth-mode", "login", "--backup-intent", "-o", "none")
            print(f"Uploaded {name} to {share}.")

    def search(self):
        self.script("bootstrap_search.py", "--execute")

    def foundry(self):
        wait_for(self.indexers_done, "both indexers to finish", timeout=900)
        self.script("bootstrap_foundry.py", "--execute")

    def indexers_done(self):
        token = az("account", "get-access-token", "--resource", "https://search.azure.com",
                   "--query", "accessToken", "-o", "tsv")
        for indexer in ("af-indexer-all-admin", "af-indexer-all-alluser"):
            request = Request(f"{self.env.search_endpoint}/indexers/{indexer}/status?api-version=2024-07-01",
                              headers={"Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=60) as response:
                status = json.load(response)
            runs = [status.get("lastResult") or {}, *status.get("executionHistory", [])]
            # Scheduled reruns process 0 items once the files are indexed; any successful run with items counts.
            if not any(run.get("status") == "success" and run.get("itemsProcessed", 0) >= 1 for run in runs):
                return False
        return True

    def smoke(self):
        checks = (
            (f"https://{self.host(self.env.console_site)}/", 200),
            (f"https://{self.host(self.env.mcp_site)}/mcp", 401),
            (f"https://{self.host(self.env.download_site)}/api/f?x=1", 401),
        )
        for url, expected in checks:
            wait_for(lambda: http_status(url) == expected, f"{url} to answer {expected}")
            print(f"OK {expected} {url}")

    def host(self, site):
        return az("resource", "show", "-g", self.env.resource_group, "-n", site, "--resource-type",
                  "Microsoft.Web/sites", "--query", "properties.defaultHostName", "-o", "tsv")


def select_preserved(console_settings, download_settings, files_account):
    """Return the Bicep parameters that keep sources added by the console across a redeployment."""
    base_connection = f"FILES_CONNECTION_{files_account.upper()}"
    console = {key: value for key, value in console_settings.items()
               if key.startswith("FILES_CONNECTION_") and key != base_connection}
    download = {key: value for key, value in download_settings.items() if key.startswith("FILES_ACCOUNT_KEY_")}
    for key, value in {**console, **download}.items():
        # Deployment parameters are visible in the deployment history: only Key Vault references are allowed.
        if not value.startswith("@Microsoft.KeyVault("):
            raise RuntimeError(f"App setting {key} is not a Key Vault reference; fix it before redeploying.")
    arguments = []
    if download_settings.get("SHARE_GROUPS"):
        arguments += ["-p", f"downloadShareGroups={download_settings['SHARE_GROUPS']}"]
    if download:
        arguments += ["-p", "downloadExtraSettings=" + json.dumps(download)]
    if console:
        arguments += ["-p", "consoleExtraSettings=" + json.dumps(console)]
    return arguments


def preserved_settings(env):
    if az("group", "exists", "-n", env.resource_group) != "true":
        return []
    sites = set(az("resource", "list", "-g", env.resource_group, "--resource-type", "Microsoft.Web/sites",
                   "--query", "[].name", "-o", "tsv").split())

    def settings(kind, site):
        if site not in sites:
            return {}
        rows = az_json(kind, "config", "appsettings", "list", "-g", env.resource_group, "-n", site)
        return {row["name"]: row["value"] or "" for row in rows or []}

    return select_preserved(settings("webapp", env.console_site), settings("functionapp", env.download_site),
                            env.files)


def summarize(what_if):
    counts = {}
    for change in what_if.get("changes", []):
        counts[change["changeType"]] = counts.get(change["changeType"], 0) + 1
    return counts


def check_what_if(what_if, resource_group):
    """Refuse a deployment that fails validation, deletes anything or touches another resource group."""
    if what_if.get("status") != "Succeeded":
        raise RuntimeError(f"What-if failed: {what_if.get('error')}")
    scope = f"/resourcegroups/{resource_group.lower()}"
    for change in what_if.get("changes", []):
        resource = change["resourceId"].lower()
        if change["changeType"] == "Delete":
            raise RuntimeError(f"What-if would delete {change['resourceId']}; stopping.")
        if change["changeType"] not in ("NoChange", "Ignore") and not (
            resource.endswith(scope) or scope + "/" in resource
        ):
            raise RuntimeError(f"What-if would change a resource outside {resource_group}: {change['resourceId']}")


def publish_webapp(resource_group, site, package):
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    # The site may still be restarting after the deploy-secrets pass and reject the first upload.
    for attempt in range(1, 5):
        try:
            az("webapp", "deploy", "-g", resource_group, "-n", site, "--src-path", str(package), "--type", "zip",
               "--async", "true")
            break
        except subprocess.CalledProcessError as error:
            print(f"Upload to {site} failed (attempt {attempt}/4): {(error.stderr or '').strip()}")
            if attempt == 4:
                raise
            time.sleep(60)

    def finished():
        latest = az_json("webapp", "log", "deployment", "list", "-g", resource_group, "-n", site,
                         "--query", "[0].{status:status,complete:complete,end:end_time}")
        if not latest or not latest.get("end") or latest["end"][:19] < started:
            return False
        if latest["status"] == 3:
            raise RuntimeError(f"Deployment of {site} failed; check az webapp log deployment list.")
        return latest["status"] == 4 and latest["complete"]

    wait_for(finished, f"{site} deployment", timeout=1200)
    print(f"Published {site}.")


def http_status(url):
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=60) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except URLError:
        return None


def wait_for(condition, label, timeout=600, interval=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if condition():
                return
        except subprocess.CalledProcessError:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for {label}.")
        print(f"Waiting for {label}...")
        time.sleep(interval)


# ---------- commands ----------

def plan(params):
    state = load_state(params)
    env = testenv.load(params)
    print(f"Environment {env.suffix} in {env.resource_group}:")
    for name, label in STEPS:
        print(f"  [{'x' if name in state else ' '}] {name:15} {label}")


def confirm_once(env):
    subscription, tenant = test.account()
    print(f"Subscription {subscription}, tenant {tenant}, resource group {env.resource_group}.")
    if input(f"Type {env.resource_group} to deploy: ").strip() != env.resource_group:
        raise RuntimeError("Confirmation did not match; nothing was changed.")


def run(params, execute, start):
    plan(params)
    if not execute:
        print("Preview only; add --execute to run the remaining steps.")
        return
    runner = Runner(params)
    confirm_once(runner.env)
    state = load_state(params)
    names = [name for name, _ in STEPS]
    if start:
        for name in names[names.index(start):]:
            state.pop(name, None)
    for name, label in STEPS:
        if name in state:
            continue
        print(f"\n== {name}: {label}")
        getattr(runner, name.replace("-", "_"))()
        state[name] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(params, state)
    env = runner.env
    print("\nAll automated steps are done.\n")
    print(MANUAL_STEPS.format(
        mcp_url=f"https://{runner.host(env.mcp_site)}/mcp", console_url=f"https://{runner.host(env.console_site)}",
        params=params.name, project=env.project,
    ))


def sso(params, app_id_uri, execute):
    runner = Runner(params)
    if not execute:
        runner.script("create_entra_apps.py", "sso", "--app-id-uri", app_id_uri)
        return
    confirm_once(runner.env)
    runner.script("create_entra_apps.py", "sso", "--app-id-uri", app_id_uri, "--execute")
    runner.deployment("-p", "grantSecretAccess=true")
    print("SSO enabled. Create the connector in the Microsoft 365 admin center with the registration ID "
          "as Reference ID and the MCP URL above.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Write a new parameters file.")
    init_parser.add_argument("--resource-group", required=True, help="New resource group name.")
    init_parser.add_argument("--group-a", required=True, help="Object ID of existing security group A.")
    init_parser.add_argument("--group-b", required=True, help="Object ID of existing security group B.")
    init_parser.add_argument("--tester-a", required=True, help="Object ID of a member of A only.")
    init_parser.add_argument("--tester-b", required=True, help="Object ID of a member of B only.")
    init_parser.add_argument("--admins", nargs="*", help="Console administrators (default: signed-in user).")
    init_parser.add_argument("--location", default="francecentral")
    init_parser.add_argument("--suffix", help="Force a suffix instead of a random one.")
    init_parser.add_argument("--output", default=str(HERE / "runtime.parameters.json"))

    for name in ("plan", "run", "sso"):
        sub = commands.add_parser(name)
        sub.add_argument("--params", help=f"Parameters file (default: ${testenv.PARAMS_ENV} or runtime.parameters.json).")
        if name != "plan":
            sub.add_argument("--execute", action="store_true", help="Apply the changes instead of previewing them.")
        if name == "run":
            sub.add_argument("--from", dest="start", choices=[step for step, _ in STEPS],
                             help="Rerun from this step even if it is marked done.")
        if name == "sso":
            sub.add_argument("--app-id-uri", required=True, help="Application ID URI from the Teams Developer Portal.")

    args = parser.parse_args()
    if args.command == "init":
        init(args)
        return
    params = Path(args.params or os.environ.get(testenv.PARAMS_ENV) or testenv.DEFAULT_PARAMS).resolve()
    if args.command == "plan":
        plan(params)
    elif args.command == "run":
        run(params, args.execute, args.start)
    else:
        sso(params, args.app_id_uri, args.execute)


if __name__ == "__main__":
    main()

import base64
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
import mimetypes
from urllib.parse import quote, unquote, urlencode, urlsplit

import azure.functions as func
from azure.storage.fileshare import generate_file_sas, FileSasPermissions

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

def _account_key(value, account):
    """Accept a bare key or a Key Vault-resolved connection string for the same account."""
    if not value or "AccountKey=" not in value:
        return value or None
    parts = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
    return parts.get("AccountKey") if parts.get("AccountName") == account else None


ACCOUNT = os.environ["FILES_ACCOUNT_NAME"]
KEY = _account_key(os.environ["FILES_ACCOUNT_KEY"], ACCOUNT)
# "admin-share=<guid>;alluser-share=<guid>"
TEXT_EXT = {".md", ".txt", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".ps1", ".py", ".sh"}
OFFICE_APPS = {
    ".docx": ("ms-word", "Word"), ".doc": ("ms-word", "Word"),
    ".xlsx": ("ms-excel", "Excel"), ".xls": ("ms-excel", "Excel"),
    ".pptx": ("ms-powerpoint", "PowerPoint"), ".ppt": ("ms-powerpoint", "PowerPoint"),
}
APP_STYLE = {"Word": ("#185abd", "W"), "Excel": ("#107c41", "X"), "PowerPoint": ("#c43e1c", "P")}
# Types the browser may render inline; anything else (html, svg, scripts...) is forced to download.
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
SAS_MINUTES = 5
OFFICE_VIEWER_SAS_MINUTES = 2
SHARE_GROUPS = dict(p.split("=", 1) for p in os.environ["SHARE_GROUPS"].split(";") if p)
# Pinned in code (not app settings) so that a configuration change cannot redirect trust.
# The zero UUIDs are placeholders that reject every caller; infra/test/build_function_package.py
# pins the real tenant and client ID into the deployment package.
TRUSTED_TENANT_ID = "00000000-0000-0000-0000-000000000000"
TRUSTED_AUDIENCES = {"00000000-0000-0000-0000-000000000000", "api://00000000-0000-0000-0000-000000000000"}
TENANT_CLAIMS = ("tid", "http://schemas.microsoft.com/identity/claims/tenantid")


def _platform_auth_enabled() -> bool:
    # App Service strips forged x-ms-client-principal headers only while its authentication is on.
    return os.environ.get("WEBSITE_AUTH_ENABLED", "").lower() == "true"


def _user_groups(req: func.HttpRequest) -> set:
    raw = req.headers.get("x-ms-client-principal")
    if not raw or req.headers.get("x-ms-client-principal-idp", "aad") != "aad":
        return set()
    claims = json.loads(base64.b64decode(raw)).get("claims", [])
    values = lambda *types: {c.get("val") for c in claims if c.get("typ") in types}
    if values(*TENANT_CLAIMS) != {TRUSTED_TENANT_ID} or not values("aud") & TRUSTED_AUDIENCES:
        return set()
    return values("groups")


@app.route(route="f/{*path}", methods=["GET"])
def open_file(req: func.HttpRequest) -> func.HttpResponse:
    if not _platform_auth_enabled():
        return func.HttpResponse("Authentication is not configured.", status_code=503)
    parts = [unquote(p) for p in req.route_params.get("path", "").split("/") if p]
    if len(parts) < 2 or any(p in (".", "..") or "\\" in p for p in parts):
        return func.HttpResponse("Invalid path.", status_code=400)

    account, key = ACCOUNT, KEY
    source_key = parts[0]
    if source_key == "accounts":
        if len(parts) < 4 or not re.fullmatch(r"[a-z0-9]{3,24}", parts[1]):
            return func.HttpResponse("Invalid path.", status_code=400)
        account, share, file_parts = parts[1], parts[2], parts[3:]
        source_key = f"{account}/{share}"
        key = _account_key(os.environ.get(f"FILES_ACCOUNT_KEY_{account.upper()}"), account)
    else:
        share, file_parts = parts[0], parts[1:]
        if share not in SHARE_GROUPS:
            # Agents may emit the short legacy link for shares the console connected on the default account.
            source_key = f"{ACCOUNT}/{share}"
    group = SHARE_GROUPS.get(source_key)
    if not group:
        return func.HttpResponse("Unknown share.", status_code=404)
    if group not in _user_groups(req):
        if req.headers.get("x-ms-client-principal") and req.params.get("relogin") != "1":
            # The Easy Auth session keeps the groups of its sign-in; sign in again once to pick up new memberships.
            params = {**req.params, "relogin": "1"}
            target = urlsplit(req.url).path + "?" + urlencode(params)
            login = "/.auth/login/aad?post_login_redirect_uri=" + quote(target, safe="")
            return func.HttpResponse(status_code=302, headers={"Location": login, "Cache-Control": "no-store"})
        return func.HttpResponse("Access to this share is denied.", status_code=403)
    if not key:
        return func.HttpResponse("Storage credentials for this source are unavailable.", status_code=503)

    name = file_parts[-1]
    ext = os.path.splitext(name)[1].lower()
    if ext in OFFICE_APPS and req.params.get("mode") != "view":
        return _office_page(account, share, file_parts, ext)

    disposition = "inline"
    if ext in TEXT_EXT:
        ctype = "text/plain; charset=utf-8"
    else:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ext not in OFFICE_APPS and ctype not in INLINE_TYPES:
            ctype, disposition = "application/octet-stream", "attachment"

    minutes = OFFICE_VIEWER_SAS_MINUTES if ext in OFFICE_APPS else SAS_MINUTES
    sas = generate_file_sas(
        account_name=account,
        share_name=share,
        file_path=file_parts,
        account_key=key,
        permission=FileSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        protocol="https",
        content_disposition=f"{disposition}; filename*=UTF-8''{quote(name)}",
        content_type=ctype,
    )
    url = f"https://{account}.file.core.windows.net/{quote(share)}/{quote('/'.join(file_parts))}?{sas}"
    if ext in OFFICE_APPS:
        # Office files cannot be rendered natively by the browser: use Office Online viewer.
        url = "https://view.officeapps.live.com/op/view.aspx?src=" + quote(url, safe="")
    return func.HttpResponse(status_code=302, headers={"Location": url, "Cache-Control": "no-store"})


def _doc_content(app_name, color, x, y, w):
    """Mock document body for the thumbnails: text lines, a sheet grid or a slide."""
    if app_name == "Excel":
        cells = "".join(
            f'<rect x="{x + c * w / 5:.1f}" y="{y + r * 11}" width="{w / 5:.1f}" height="11" fill="{"#e6f2ea" if r == 0 else "#fff"}" stroke="#d6d6d6" stroke-width=".6"/>'
            for r in range(6) for c in range(5))
        return cells + f'<rect x="{x + w / 5:.1f}" y="{y + 22}" width="{w / 5:.1f}" height="11" fill="none" stroke="{color}" stroke-width="1.4"/>'
    if app_name == "PowerPoint":
        return (f'<rect x="{x}" y="{y}" width="{w}" height="64" rx="3" fill="#fff" stroke="#d6d6d6"/>'
                f'<rect x="{x + 10}" y="{y + 12}" width="{w * .55:.1f}" height="7" rx="2" fill="{color}"/>'
                f'<rect x="{x + 10}" y="{y + 26}" width="{w * .4:.1f}" height="4" rx="2" fill="#c8c8c8"/>'
                f'<rect x="{x + 10}" y="{y + 34}" width="{w * .45:.1f}" height="4" rx="2" fill="#c8c8c8"/>'
                f'<circle cx="{x + w - 26}" cy="{y + 38}" r="15" fill="{color}" opacity=".25"/>')
    lines = (.9, .75, .85, .6, .8, .7)
    return (f'<rect x="{x}" y="{y}" width="{w}" height="66" rx="3" fill="#fff" stroke="#d6d6d6"/>'
            f'<rect x="{x + 10}" y="{y + 9}" width="{w * .5:.1f}" height="6" rx="2" fill="{color}"/>'
            + "".join(f'<rect x="{x + 10}" y="{y + 21 + i * 7}" width="{(w - 20) * f:.1f}" height="3" rx="1.5" fill="#c8c8c8"/>'
                      for i, f in enumerate(lines)))


def _thumbnails(app_name):
    color, letter = APP_STYLE[app_name]
    desktop = f"""<svg viewBox="0 0 220 132" role="img" aria-label="{app_name} desktop app">
<rect x="1" y="1" width="218" height="130" rx="10" fill="#f3f3f3" stroke="#cfcfcf"/>
<path d="M1 11a10 10 0 0 1 10-10h198a10 10 0 0 1 10 10v11H1z" fill="{color}"/>
<rect x="9" y="6" width="11" height="11" rx="2.5" fill="#fff"/><text x="14.5" y="15" font-size="9" font-weight="700" text-anchor="middle" fill="{color}" font-family="Segoe UI,sans-serif">{letter}</text>
<circle cx="196" cy="11.5" r="2.2" fill="#fff" opacity=".8"/><circle cx="205" cy="11.5" r="2.2" fill="#fff" opacity=".8"/><circle cx="187" cy="11.5" r="2.2" fill="#fff" opacity=".8"/>
<rect x="1" y="22" width="218" height="16" fill="#fafafa"/>
{"".join(f'<rect x="{10 + i * 26}" y="27" width="18" height="6" rx="2" fill="#dadada"/>' for i in range(5))}
<rect x="150" y="26" width="58" height="8" rx="4" fill="{color}" opacity=".18"/><text x="179" y="32.4" font-size="6" font-weight="700" text-anchor="middle" fill="{color}" font-family="Segoe UI,sans-serif">EDITING</text>
{_doc_content(app_name, color, 40, 50, 140)}
<circle cx="190" cy="112" r="12" fill="{color}"/><path d="M184.5 112.5l3.5 3.5 7-7.5" stroke="#fff" stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""
    online = f"""<svg viewBox="0 0 220 132" role="img" aria-label="Read-only online viewer">
<rect x="1" y="1" width="218" height="130" rx="10" fill="#f3f3f3" stroke="#cfcfcf"/>
<path d="M1 11a10 10 0 0 1 10-10h198a10 10 0 0 1 10 10v9H1z" fill="#e4e4e4"/>
<circle cx="11" cy="10.5" r="2.4" fill="#ff5f57"/><circle cx="19" cy="10.5" r="2.4" fill="#febc2e"/><circle cx="27" cy="10.5" r="2.4" fill="#28c840"/>
<rect x="40" y="5" width="140" height="11" rx="5.5" fill="#fff"/><path d="M47 9.5h3.4v4h-3.4z M47.6 9.5v-1.3a1.1 1.1 0 0 1 2.2 0v1.3" stroke="#888" stroke-width=".8" fill="none"/>
<rect x="54" y="9" width="80" height="3" rx="1.5" fill="#cfcfcf"/>
<rect x="1" y="20" width="218" height="14" fill="{color}"/><text x="10" y="29.6" font-size="6.5" font-weight="600" fill="#fff" font-family="Segoe UI,sans-serif">{app_name} for the web</text>
{_doc_content(app_name, color, 40, 46, 140)}
<rect x="160" y="100" width="50" height="20" rx="10" fill="#fff" stroke="#cfcfcf"/>
<path d="M171 108.5h7v6h-7z M172.5 108.5v-2a2 2 0 0 1 4 0v2" stroke="#5c5c5c" stroke-width="1.1" fill="none"/>
<text x="181" y="113.4" font-size="6.5" fill="#5c5c5c" font-family="Segoe UI,sans-serif">Read</text>
</svg>"""
    return desktop, online


def _office_page(account, share, file_parts, ext):
    """Offer editing in the desktop app over SMB; the HTTPS SAS endpoint cannot be saved back to."""
    scheme, app_name = OFFICE_APPS[ext]
    color, letter = APP_STYLE[app_name]
    host = f"{account}.file.core.windows.net"
    unc = "\\\\" + "\\".join([host, share, *file_parts])
    name, e = file_parts[-1], html.escape
    edit_uri = f"{scheme}:ofe|u|file://{host}/{quote(share)}/{quote('/'.join(file_parts))}"
    desktop, online = _thumbnails(app_name)
    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{e(name)} · RAG Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#f7f4ef;--surface:#fff;--soft:#f5f5f5;--border:#dedede;--text:#242424;--muted:#5c5c5c;--accent:#b11f4b;--accent-hover:#9a1a41;--accent-soft:rgba(177,31,75,.08);--accent-fg:#fff;--app:{color}}}
@media(prefers-color-scheme:dark){{:root{{--bg:#3d3b3a;--surface:#292929;--soft:#2e2e2e;--border:#474747;--text:#dedede;--muted:#a0a0a0;--accent:#fd8ea1;--accent-hover:#fb7b91;--accent-soft:rgba(253,142,161,.14);--accent-fg:#1a1a1a}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",Aptos,Calibri,-apple-system,sans-serif}}
.wrap{{max-width:880px;margin:0 auto;padding:40px 20px 56px}}
.brand{{display:flex;gap:12px;align-items:center;margin-bottom:36px}}.mark{{display:grid;place-items:center;width:36px;height:36px;border-radius:10px;background:var(--accent);color:var(--accent-fg);font-weight:800}}
.brand b{{font-size:18px;letter-spacing:-.4px}}.brand b span{{color:var(--accent)}}.brand small{{display:block;color:var(--muted);font-size:11px}}
.file{{display:flex;gap:16px;align-items:center;margin-bottom:8px}}.icon{{display:grid;place-items:center;flex-shrink:0;width:48px;height:48px;border-radius:12px;background:var(--app);color:#fff;font-weight:800;font-size:22px}}
h1{{margin:0;font-size:26px;font-weight:650;letter-spacing:-.6px;overflow-wrap:anywhere}}.eyebrow{{text-transform:uppercase;letter-spacing:1.5px;font-size:11px;font-weight:700;color:var(--muted)}}
.lead{{color:var(--muted);margin:16px 0 28px}}
.modes{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}}
.mode{{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px;transition:border-color .15s,transform .15s}}.mode:hover{{border-color:var(--accent);transform:translateY(-2px)}}
.thumb{{background:var(--soft);border-radius:12px;padding:14px;margin-bottom:18px}}.thumb svg{{display:block;width:100%;height:auto}}
.pill{{align-self:flex-start;display:inline-block;border-radius:20px;font-size:11px;font-weight:600;padding:4px 10px;background:var(--accent-soft);color:var(--accent);margin-bottom:10px}}.pill.ro{{background:var(--soft);color:var(--muted);border:1px solid var(--border)}}
h2{{margin:0 0 6px;font-size:17px;font-weight:650}}ul{{margin:0 0 20px;padding-left:18px;color:var(--muted);font-size:13px}}li{{margin:3px 0}}
.btn{{margin-top:auto;display:flex;justify-content:center;align-items:center;gap:8px;padding:11px 16px;border-radius:10px;font-weight:600;text-decoration:none;border:1px solid var(--border);color:var(--text);background:var(--surface)}}
.btn:hover{{background:var(--soft)}}.btn.primary{{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}}.btn.primary:hover{{background:var(--accent-hover)}}
.btn svg{{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}}
.path{{margin-top:24px;padding:14px 16px;border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:10px;background:var(--surface);font-size:12px;color:var(--muted)}}
code{{font-family:Consolas,monospace;font-size:12px;color:var(--text);overflow-wrap:anywhere}}
@media(max-width:680px){{.modes{{grid-template-columns:1fr}}h1{{font-size:22px}}}}
</style></head>
<body><div class="wrap">
<div class="brand"><div class="mark">R</div><div><b>RAG Control<span>.</span></b><small>Document intelligence platform</small></div></div>
<div class="file"><div class="icon">{letter}</div><div><div class="eyebrow">{app_name} file · {e(share)}</div><h1>{e(name)}</h1></div></div>
<p class="lead">Choose how to open this file.</p>
<div class="modes">
<section class="mode"><div class="thumb">{desktop}</div><span class="pill">Editable</span><h2>Open in the {app_name} app</h2>
<ul><li>Edit and save directly on the network share.</li><li>Requires the desktop {app_name} app and SMB access (port 445) to Azure Files.</li><li>Your share/NTFS permissions apply.</li></ul>
<a class="btn primary" id="edit" href="{e(edit_uri)}"><svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>Open in {app_name}</a></section>
<section class="mode"><div class="thumb">{online}</div><span class="pill ro">Read-only</span><h2>View online</h2>
<ul><li>Opens in a new browser tab with {app_name} for the web.</li><li>No installation or network share access needed.</li><li>Changes cannot be saved.</li></ul>
<a class="btn" href="?mode=view" target="_blank" rel="noopener"><svg viewBox="0 0 24 24"><path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/></svg>View online</a></section>
</div>
<div class="path">Network path: <code>{e(unc)}</code></div>
</div></body></html>"""
    return func.HttpResponse(body, status_code=200, mimetype="text/html", charset="utf-8",
                             headers={"Cache-Control": "no-store"})

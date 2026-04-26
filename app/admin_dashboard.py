# pyright: reportGeneralTypeIssues=false
"""Browser-based admin dashboard.

Routes:

    GET  /admin                    - redirect to login or dashboard
    GET  /admin/dashboard          - HTML dashboard (cookie-protected)
    GET  /admin/login              - HTML login form
    POST /admin/login              - validates ADMIN_TOKEN, sets session cookie
    POST /admin/logout             - clears session
    GET  /admin/data/overview      - JSON: keys + invites + usage totals
                                     (used by the dashboard JS to populate)
    POST /admin/data/invite        - JSON: create new invite
    DELETE /admin/data/key/{id}    - JSON: revoke key

The dashboard is single-page: one HTML file, vanilla JS. After login,
JS calls /admin/data/* endpoints to populate tables and submit actions.
The /admin/* (curl-friendly) endpoints in admin.py remain for scripting.

The /admin/data/* endpoints check the session cookie, not ADMIN_TOKEN.
This means the dashboard can't be probed by someone holding the token
but no session - and conversely a session can issue invites without
ever seeing the token after login.
"""

import logging
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.admin_session import (
    COOKIE_NAME,
    AdminSession,
    create_session,
    destroy_session,
    validate_session,
)
from app.config import settings
from app.invites import get_invite_store
from app.keys import get_keystore
from app.usage import get_totals, get_usage_per_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])


# --- Auth dependency ------------------------------------------------------


async def require_session(
    admin_session: str | None = Cookie(default=None),
) -> AdminSession:
    """FastAPI dependency: read session cookie, validate, return session.

    Raises 401 if no valid session. The dashboard JS catches 401 and
    redirects to /admin/login.
    """
    session = await validate_session(admin_session)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return session


# --- Login + logout -------------------------------------------------------


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post("/login")
async def login(request: LoginRequest, fastapi_request: Request) -> JSONResponse:
    """Validate ADMIN_TOKEN, issue a session cookie."""
    if not settings.admin_token:
        # Admin disabled: no login surface. 404 is consistent with
        # the rest of /admin/*.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    if request.token != settings.admin_token:
        logger.warning(
            "admin_login_failed",
            extra={
                "ip": fastapi_request.client.host if fastapi_request.client else None
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
        )

    ip = fastapi_request.client.host if fastapi_request.client else None
    session_id = await create_session(ip=ip)

    # Cookie security flags:
    # - HttpOnly: JS can't read the cookie (defends against XSS)
    # - Secure: only send over HTTPS (prevents passive sniffing)
    #   We derive this from the request scheme so local HTTP dev works.
    #   In production (HTTPS on Render) this is True; locally it's False.
    # - SameSite=Lax: cookie not sent on cross-origin POSTs (defends CSRF)
    is_https = fastapi_request.url.scheme == "https"

    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        max_age=24 * 60 * 60,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/admin",  # Cookie only sent on /admin/* requests
    )
    return response


@router.post("/logout")
async def logout(
    admin_session: str | None = Cookie(default=None),
) -> JSONResponse:
    """Destroy the session and clear the cookie."""
    await destroy_session(admin_session)
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=COOKIE_NAME, path="/admin")
    return response


# --- Data endpoints (cookie-protected) ------------------------------------


@router.get("/data/overview")
async def overview(
    _session: AdminSession = Depends(require_session),
) -> dict[str, Any]:
    """One call returns everything the dashboard needs to render.

    Combining the lookups into a single endpoint keeps the dashboard's
    initial render to one round trip. As the data grows we can split.
    """
    keystore = get_keystore()
    invite_store = get_invite_store()

    keys = await keystore.list()
    invites = await invite_store.list()
    usage_per_user = await get_usage_per_user(days=7)
    totals = await get_totals()

    # Merge usage into key records so the table can show "Alice's iPhone:
    # 4 today, 18 this week" all in one row.
    usage_by_user_id = {u["user_id"]: u for u in usage_per_user}
    for k in keys:
        u = usage_by_user_id.get(k["user_id"], {})
        k["today"] = u.get("today", 0)
        k["last_7_days"] = u.get("last_7_days", 0)
        k["all_time"] = u.get("all_time", 0)

    return {
        "totals": totals,
        "keys": keys,
        "invites": invites,
        "usage_per_user": usage_per_user,
    }


class CreateInviteForm(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)


@router.post("/data/invite", status_code=201)
async def create_invite_dashboard(
    body: CreateInviteForm,
    fastapi_request: Request,
    _session: AdminSession = Depends(require_session),
) -> dict[str, str]:
    invite = await get_invite_store().create(user_id=body.user_id)
    base_url = str(fastapi_request.base_url).rstrip("/")
    return {
        "code": invite.code,
        "user_id": invite.user_id,
        "created_at": invite.created_at,
        "onboard_url": f"{base_url}/onboard?code={invite.code}",
    }


@router.delete("/data/key/{key_id}")
async def revoke_key_dashboard(
    key_id: int,
    _session: AdminSession = Depends(require_session),
) -> dict[str, Any]:
    revoked = await get_keystore().revoke(key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active key with id {key_id}",
        )
    return {"revoked": True, "key_id": key_id}


# --- HTML pages -----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    """Login form. Public - no auth required."""
    if not settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )
    return HTMLResponse(content=_LOGIN_HTML)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    admin_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Main dashboard. If no session, redirect to login."""
    if not settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    session = await validate_session(admin_session)
    if session is None:
        return RedirectResponse(url="/admin/login", status_code=302)  # type: ignore[return-value]

    return HTMLResponse(content=_DASHBOARD_HTML)


# Convenience: hitting /admin redirects to /admin/dashboard or /admin/login
@router.get("", response_class=HTMLResponse)
async def admin_root(
    admin_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    if not settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )
    session = await validate_session(admin_session)
    target = "/admin/dashboard" if session else "/admin/login"
    return RedirectResponse(url=target, status_code=302)  # type: ignore[return-value]


# --- HTML content (kept inline for single-deploy simplicity) ---------------

# Shared CSS for login + dashboard. Same dark theme as /onboard.
_SHARED_CSS = """
:root {
  --bg: #0d0d0f;
  --surface: #1a1a1d;
  --surface-2: #25252a;
  --border: #2e2e34;
  --text: #e8e8ea;
  --text-dim: #94949c;
  --text-faint: #6b6b73;
  --accent: #7c8cff;
  --accent-hover: #9ba8ff;
  --success: #4ade80;
  --error: #f87171;
  --warning: #fbbf24;
  --radius: 10px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
               "Helvetica Neue", Arial, sans-serif;
  min-height: 100vh;
  line-height: 1.5;
}
button, input, select { font-family: inherit; }
input[type=text], input[type=password] {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  font-size: 14px;
  padding: 10px 14px;
  transition: border-color 0.15s;
  width: 100%;
}
input[type=text]:focus, input[type=password]:focus {
  outline: none;
  border-color: var(--accent);
}
button {
  background: var(--accent);
  color: #0d0d0f;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  padding: 10px 18px;
  cursor: pointer;
  transition: background 0.15s;
}
button:hover:not(:disabled) { background: var(--accent-hover); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
button.secondary {
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--border);
}
button.secondary:hover:not(:disabled) { background: var(--border); }
button.danger {
  background: rgba(248, 113, 113, 0.15);
  color: var(--error);
  border: 1px solid rgba(248, 113, 113, 0.3);
}
button.danger:hover:not(:disabled) { background: rgba(248, 113, 113, 0.25); }
.error {
  color: var(--error);
  background: rgba(248, 113, 113, 0.08);
  border: 1px solid rgba(248, 113, 113, 0.2);
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
}
.hidden { display: none !important; }
"""

_LOGIN_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reel Analyzer — Admin</title>
<style>
{_SHARED_CSS}
.login-wrap {{
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}}
.login-card {{
  width: 100%;
  max-width: 380px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 32px;
}}
.login-card h1 {{
  font-size: 22px;
  font-weight: 600;
  margin-bottom: 6px;
  letter-spacing: -0.01em;
}}
.login-card .subtitle {{
  color: var(--text-dim);
  font-size: 13px;
  margin-bottom: 24px;
}}
.login-card label {{
  display: block;
  font-size: 12px;
  color: var(--text-dim);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.login-card .err {{ margin-top: 16px; }}
.login-card .submit {{ margin-top: 20px; width: 100%; }}
</style>
</head>
<body>
<div class="login-wrap">
  <div class="login-card">
    <h1>Admin login</h1>
    <p class="subtitle">Reel Analyzer dashboard</p>
    <label for="token">Admin token</label>
    <input type="password" id="token" autocomplete="off" autofocus>
    <div id="err" class="error err hidden"></div>
    <button id="submit" class="submit">Sign in</button>
  </div>
</div>
<script>
(function() {{
  const tokenInput = document.getElementById("token");
  const errEl = document.getElementById("err");
  const submitBtn = document.getElementById("submit");

  function showErr(msg) {{ errEl.textContent = msg; errEl.classList.remove("hidden"); }}

  async function login() {{
    errEl.classList.add("hidden");
    const token = tokenInput.value.trim();
    if (!token) {{ showErr("Enter your admin token."); return; }}
    submitBtn.disabled = true;
    submitBtn.textContent = "Signing in...";
    try {{
      const r = await fetch("/admin/login", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ token }})
      }});
      if (!r.ok) {{
        const data = await r.json().catch(() => ({{ detail: "Login failed" }}));
        showErr(data.detail || "Login failed");
        submitBtn.disabled = false;
        submitBtn.textContent = "Sign in";
        return;
      }}
      window.location.href = "/admin/dashboard";
    }} catch (err) {{
      showErr("Network error.");
      submitBtn.disabled = false;
      submitBtn.textContent = "Sign in";
    }}
  }}

  submitBtn.addEventListener("click", login);
  tokenInput.addEventListener("keydown", (e) => {{
    if (e.key === "Enter") login();
  }});
}})();
</script>
</body>
</html>
"""

_DASHBOARD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reel Analyzer — Dashboard</title>
<style>
{_SHARED_CSS}
.shell {{
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 24px 80px;
}}
.topbar {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 28px;
}}
.topbar h1 {{
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.01em;
}}
.topbar .actions {{
  display: flex;
  gap: 8px;
}}
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 32px;
}}
.stat {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
}}
.stat-label {{
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.07em;
  margin-bottom: 6px;
}}
.stat-value {{
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
}}
.section {{
  margin-bottom: 36px;
}}
.section h2 {{
  font-size: 14px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: var(--text-dim);
  margin-bottom: 12px;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  font-size: 13px;
}}
th, td {{
  text-align: left;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}}
th {{
  background: var(--surface-2);
  font-weight: 500;
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--text-dim);
}}
tbody tr:last-child td {{ border-bottom: none; }}
td.mono {{
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 12px;
  color: var(--text-dim);
}}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}}
.badge-active {{ background: rgba(74, 222, 128, 0.15); color: var(--success); }}
.badge-revoked {{ background: rgba(148, 148, 156, 0.15); color: var(--text-faint); }}
.badge-pending {{ background: rgba(251, 191, 36, 0.15); color: var(--warning); }}
.badge-used {{ background: rgba(74, 222, 128, 0.15); color: var(--success); }}
.invite-form {{
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}}
.invite-form input {{ flex: 1; }}
.empty-state {{
  padding: 32px;
  text-align: center;
  color: var(--text-dim);
  font-size: 13px;
}}
.copy-link {{
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  padding: 0;
  font-size: 12px;
  text-decoration: underline;
}}
.copy-link.copied {{ color: var(--success); }}
.toast {{
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 18px;
  font-size: 13px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.4);
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 0.2s, transform 0.2s;
  pointer-events: none;
}}
.toast.show {{ opacity: 1; transform: translateY(0); }}
.toast.ok {{ border-color: rgba(74, 222, 128, 0.4); }}
.toast.err {{ border-color: rgba(248, 113, 113, 0.4); }}
.code-display {{
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  background: var(--surface-2);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}}
</style>
</head>
<body>
<div class="shell">
  <div class="topbar">
    <h1>Dashboard</h1>
    <div class="actions">
      <button id="refresh-btn" class="secondary">Refresh</button>
      <button id="logout-btn" class="secondary">Sign out</button>
    </div>
  </div>

  <div class="stats" id="stats"></div>

  <div class="section">
    <h2>Generate invite</h2>
    <div class="invite-form">
      <input type="text" id="invite-user-id" placeholder="user_id (e.g. alice, bob)">
      <button id="invite-btn">Create invite</button>
    </div>
  </div>

  <div class="section">
    <h2>Keys</h2>
    <div id="keys-table"></div>
  </div>

  <div class="section">
    <h2>Invites</h2>
    <div id="invites-table"></div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
(function() {{
  const statsEl = document.getElementById("stats");
  const keysEl = document.getElementById("keys-table");
  const invitesEl = document.getElementById("invites-table");
  const toastEl = document.getElementById("toast");
  const refreshBtn = document.getElementById("refresh-btn");
  const logoutBtn = document.getElementById("logout-btn");
  const inviteBtn = document.getElementById("invite-btn");
  const inviteInput = document.getElementById("invite-user-id");

  async function api(path, opts) {{
    opts = opts || {{}};
    opts.credentials = "include";
    opts.headers = Object.assign({{ "Content-Type": "application/json" }}, opts.headers || {{}});
    const r = await fetch(path, opts);
    if (r.status === 401) {{
      window.location.href = "/admin/login";
      throw new Error("not authenticated");
    }}
    if (!r.ok) {{
      const data = await r.json().catch(() => ({{ detail: "Request failed" }}));
      throw new Error(data.detail || "Request failed");
    }}
    return r.json();
  }}

  function escape(s) {{
    return String(s ?? "").replace(/[&<>"']/g, c => ({{ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }})[c]);
  }}

  function formatDate(iso) {{
    if (!iso) return "—";
    const d = new Date(iso + "Z");
    const now = new Date();
    const ms = now - d;
    if (ms < 60_000) return "just now";
    if (ms < 3600_000) return Math.floor(ms / 60_000) + " min ago";
    if (ms < 86400_000) return Math.floor(ms / 3600_000) + "h ago";
    if (ms < 7 * 86400_000) return Math.floor(ms / 86400_000) + "d ago";
    return d.toISOString().slice(0, 10);
  }}

  function showToast(msg, kind) {{
    toastEl.textContent = msg;
    toastEl.className = "toast show " + (kind || "ok");
    setTimeout(() => {{ toastEl.classList.remove("show"); }}, 2400);
  }}

  function renderStats(t) {{
    statsEl.innerHTML = `
      <div class="stat"><div class="stat-label">Today</div><div class="stat-value">${{t.today}}</div></div>
      <div class="stat"><div class="stat-label">Last 7 days</div><div class="stat-value">${{t.last_7_days}}</div></div>
      <div class="stat"><div class="stat-label">All time</div><div class="stat-value">${{t.all_time}}</div></div>
      <div class="stat"><div class="stat-label">Active today</div><div class="stat-value">${{t.active_users_today}}</div></div>
      <div class="stat"><div class="stat-label">Total users</div><div class="stat-value">${{t.total_users_ever}}</div></div>
    `;
  }}

  function renderKeys(keys) {{
    if (!keys.length) {{
      keysEl.innerHTML = '<div class="empty-state">No keys issued yet.</div>';
      return;
    }}
    const rows = keys.map(k => `
      <tr>
        <td class="num mono">${{k.id}}</td>
        <td>${{escape(k.user_id)}}</td>
        <td>${{escape(k.name)}}</td>
        <td class="num">${{k.today || 0}}</td>
        <td class="num">${{k.last_7_days || 0}}</td>
        <td class="num">${{k.all_time || 0}}</td>
        <td class="mono">${{formatDate(k.last_used_at)}}</td>
        <td>${{k.active ? '<span class="badge badge-active">Active</span>' : '<span class="badge badge-revoked">Revoked</span>'}}</td>
        <td>${{k.active ? `<button class="danger" data-revoke="${{k.id}}">Revoke</button>` : ''}}</td>
      </tr>
    `).join("");
    keysEl.innerHTML = `
      <table>
        <thead>
          <tr><th>ID</th><th>User</th><th>Name</th><th class="num">Today</th><th class="num">7d</th><th class="num">All</th><th>Last used</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>${{rows}}</tbody>
      </table>
    `;

    keysEl.querySelectorAll("[data-revoke]").forEach(btn => {{
      btn.addEventListener("click", async () => {{
        const id = btn.getAttribute("data-revoke");
        if (!confirm(`Revoke key ${{id}}? This cannot be undone.`)) return;
        try {{
          await api(`/admin/data/key/${{id}}`, {{ method: "DELETE" }});
          showToast(`Key ${{id}} revoked`, "ok");
          load();
        }} catch (e) {{ showToast(e.message, "err"); }}
      }});
    }});
  }}

  function renderInvites(invites) {{
    if (!invites.length) {{
      invitesEl.innerHTML = '<div class="empty-state">No invites yet. Create one above.</div>';
      return;
    }}
    const rows = invites.map(i => `
      <tr>
        <td><span class="code-display">${{escape(i.code)}}</span></td>
        <td>${{escape(i.user_id)}}</td>
        <td class="mono">${{formatDate(i.created_at)}}</td>
        <td>${{i.used ? '<span class="badge badge-used">Redeemed</span>' : '<span class="badge badge-pending">Pending</span>'}}</td>
        <td class="mono">${{i.used ? formatDate(i.redeemed_at) : '—'}}</td>
        <td>${{!i.used ? `<button class="copy-link" data-copy-url="${{escape(i.code)}}">Copy onboard link</button>` : (i.redeemed_by_key_id ? `Key #${{i.redeemed_by_key_id}}` : '')}}</td>
      </tr>
    `).join("");
    invitesEl.innerHTML = `
      <table>
        <thead>
          <tr><th>Code</th><th>For user</th><th>Created</th><th>Status</th><th>Redeemed</th><th></th></tr>
        </thead>
        <tbody>${{rows}}</tbody>
      </table>
    `;

    invitesEl.querySelectorAll("[data-copy-url]").forEach(btn => {{
      btn.addEventListener("click", async () => {{
        const code = btn.getAttribute("data-copy-url");
        const url = `${{window.location.origin}}/onboard?code=${{code}}`;
        try {{
          await navigator.clipboard.writeText(url);
          btn.textContent = "Copied ✓";
          btn.classList.add("copied");
          setTimeout(() => {{ btn.textContent = "Copy onboard link"; btn.classList.remove("copied"); }}, 2000);
        }} catch (e) {{ showToast("Could not copy", "err"); }}
      }});
    }});
  }}

  async function load() {{
    try {{
      const data = await api("/admin/data/overview");
      renderStats(data.totals);
      renderKeys(data.keys);
      renderInvites(data.invites);
    }} catch (e) {{
      if (e.message !== "not authenticated") showToast(e.message, "err");
    }}
  }}

  refreshBtn.addEventListener("click", load);

  logoutBtn.addEventListener("click", async () => {{
    try {{
      await api("/admin/logout", {{ method: "POST" }});
    }} catch (e) {{}}
    window.location.href = "/admin/login";
  }});

  inviteBtn.addEventListener("click", async () => {{
    const user_id = inviteInput.value.trim();
    if (!user_id) {{ showToast("Enter a user_id", "err"); return; }}
    inviteBtn.disabled = true;
    try {{
      const data = await api("/admin/data/invite", {{
        method: "POST",
        body: JSON.stringify({{ user_id }})
      }});
      try {{
        await navigator.clipboard.writeText(data.onboard_url);
        showToast(`Created invite for ${{user_id}}. Onboard URL copied to clipboard.`, "ok");
      }} catch (e) {{
        showToast(`Created invite for ${{user_id}}: ${{data.code}}`, "ok");
      }}
      inviteInput.value = "";
      load();
    }} catch (e) {{ showToast(e.message, "err"); }}
    inviteBtn.disabled = false;
  }});

  inviteInput.addEventListener("keydown", (e) => {{
    if (e.key === "Enter") inviteBtn.click();
  }});

  load();
}})();
</script>
</body>
</html>
"""

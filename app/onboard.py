# pyright: reportGeneralTypeIssues=false
"""Public self-service onboarding.

    GET  /onboard             - HTML page: form to redeem an invite
    GET  /onboard?code=XXXX   - HTML page with invite code pre-filled
    POST /onboard/redeem      - Validates invite, issues API key, returns it

The flow:

    1. Admin issues an invite via POST /admin/invites for a specific user_id.
       Receives a code and an onboard URL.
    2. Admin shares the URL with their friend.
    3. Friend visits the URL, sees a form. The user_id is fixed (tied to
       the invite at issuance time); they only confirm by clicking through.
       Optionally they can name their integration (e.g. "iPhone").
    4. Server validates the invite, mints an ra_live_ key bound to the
       invite's user_id, marks the invite redeemed.
    5. Friend sees their key once. They install the Shortcut, paste in
       the key, and start using the service.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.invites import (
    InviteAlreadyUsedError,
    InviteNotFoundError,
    get_invite_store,
)
from app.keys import get_keystore
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboard"])


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class RedeemResponse(BaseModel):
    user_id: str
    key_name: str
    api_key: str
    warning: str


@router.post("/onboard/redeem", response_model=RedeemResponse)
async def redeem(request: RedeemRequest) -> RedeemResponse:
    """Validate an invite code and issue an API key for the bound user_id."""
    code = request.code.strip().upper()
    invite_store = get_invite_store()
    keystore = get_keystore()

    try:
        client = get_redis()
        record = await client.hgetall(f"invite:{code}")
    except Exception:
        logger.exception("invite_lookup_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not look up invite",
        )

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid invite code",
        )
    if record.get("used") != "0":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invite code has already been used",
        )

    user_id = record["user_id"]

    issued_key = await keystore.create(user_id=user_id, name=request.name)

    try:
        await invite_store.redeem(code=code, key_id=issued_key.key_id)
    except InviteAlreadyUsedError:
        logger.warning(
            "invite_redeem_race",
            extra={"code": code, "key_id": issued_key.key_id},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invite was claimed by another request",
        )
    except InviteNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid invite code",
        )

    logger.info(
        "onboarding_complete",
        extra={
            "user_id": user_id,
            "key_id": issued_key.key_id,
            "key_name": issued_key.name,
        },
    )

    return RedeemResponse(
        user_id=user_id,
        key_name=issued_key.name,
        api_key=issued_key.plaintext,
        warning=(
            "This key will not be shown again. Save it now. "
            "You'll paste it into the Shortcut after install."
        ),
    )


# --- The HTML page --------------------------------------------------------

_ONBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reel Analyzer — Onboard</title>
<style>
  :root {
    --bg: #0d0d0f;
    --surface: #1a1a1d;
    --surface-2: #25252a;
    --border: #2e2e34;
    --text: #e8e8ea;
    --text-dim: #94949c;
    --accent: #7c8cff;
    --accent-hover: #9ba8ff;
    --success: #4ade80;
    --error: #f87171;
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
  .container { max-width: 540px; margin: 0 auto; padding: 48px 24px; }
  h1 { font-size: 28px; font-weight: 600; margin-bottom: 8px; letter-spacing: -0.02em; }
  .subtitle { color: var(--text-dim); margin-bottom: 40px; font-size: 15px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 28px; margin-bottom: 16px; }
  label { display: block; font-size: 13px; font-weight: 500; color: var(--text-dim); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  input[type=text] { width: 100%; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-size: 15px; padding: 12px 14px; font-family: inherit; margin-bottom: 20px; transition: border-color 0.15s; }
  input[type=text]:focus { outline: none; border-color: var(--accent); }
  input[type=text].mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; letter-spacing: 0.02em; }
  button { background: var(--accent); color: #0d0d0f; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; padding: 12px 24px; cursor: pointer; width: 100%; transition: background 0.15s; font-family: inherit; }
  button:hover:not(:disabled) { background: var(--accent-hover); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .error { color: var(--error); background: rgba(248, 113, 113, 0.1); border: 1px solid rgba(248, 113, 113, 0.2); padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }
  .success-banner { color: var(--success); background: rgba(74, 222, 128, 0.08); border: 1px solid rgba(74, 222, 128, 0.2); padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; font-weight: 500; }
  .key-display { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 16px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px; word-break: break-all; position: relative; margin-bottom: 16px; }
  .copy-btn { width: auto; padding: 8px 14px; font-size: 13px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }
  .copy-btn:hover:not(:disabled) { background: var(--border); }
  .copy-btn.copied { color: var(--success); border-color: var(--success); }
  .warning-text { color: var(--text-dim); font-size: 13px; margin-top: 8px; }
  .step { display: flex; gap: 16px; padding: 16px 0; border-bottom: 1px solid var(--border); }
  .step:last-child { border-bottom: none; }
  .step-num { flex-shrink: 0; width: 28px; height: 28px; border-radius: 50%; background: var(--surface-2); border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; color: var(--text-dim); }
  .step-content { flex: 1; padding-top: 2px; }
  .step-title { font-weight: 500; margin-bottom: 4px; }
  .step-desc { color: var(--text-dim); font-size: 14px; }
  a.shortcut-link { display: inline-block; color: var(--accent); text-decoration: none; margin-top: 8px; font-weight: 500; }
  a.shortcut-link:hover { color: var(--accent-hover); }
  .hidden { display: none !important; }
  .footer { text-align: center; color: var(--text-dim); font-size: 12px; margin-top: 40px; }
</style>
</head>
<body>
  <div class="container">
    <h1>Reel Analyzer</h1>
    <p class="subtitle">Share an Instagram reel to your iPhone Shortcut, get an AI summary back. Use your invite code to claim a key.</p>

    <div id="form-view">
      <div class="card">
        <div id="error-msg" class="error hidden"></div>
        <label for="code">Invite code</label>
        <input type="text" id="code" class="mono" placeholder="e.g. ABCD1234EFGH5678" autocomplete="off">
        <label for="name">Name this device</label>
        <input type="text" id="name" placeholder="e.g. iPhone, MacBook" maxlength="128">
        <button id="submit-btn">Get my key</button>
      </div>
    </div>

    <div id="success-view" class="hidden">
      <div class="success-banner">✓ You're set up. Save your key — it won't be shown again.</div>
      <div class="card">
        <label>Your API key</label>
        <div id="key-display" class="key-display"></div>
        <button id="copy-btn" class="copy-btn">Copy key</button>
        <p class="warning-text">If you lose this, you'll need to ask the admin to revoke and re-issue.</p>
      </div>
      <div class="card">
        <label>Setup steps</label>
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-content">
            <div class="step-title">Install the Shortcut</div>
            <div class="step-desc">Tap to add it to your iPhone.</div>
            <a id="shortcut-link" class="shortcut-link" href="SHORTCUT_URL_PLACEHOLDER" target="_blank">Install Shortcut →</a>
          </div>
        </div>
        <div class="step">
          <div class="step-num">2</div>
          <div class="step-content">
            <div class="step-title">Paste your key into the Shortcut</div>
            <div class="step-desc">After install, edit the Shortcut. Find the "Get Contents of URL" action, expand Headers, and replace <code>REPLACE_ME</code> in the Authorization header with your key above.</div>
          </div>
        </div>
        <div class="step">
          <div class="step-num">3</div>
          <div class="step-content">
            <div class="step-title">Share any reel to it</div>
            <div class="step-desc">Open Instagram, tap share on a reel, choose the Shortcut. AI summary comes back in ~30 seconds.</div>
          </div>
        </div>
      </div>
    </div>

    <p class="footer">Reel Analyzer · personal tool · no data stored</p>
  </div>

<script>
(function() {
  const params = new URLSearchParams(window.location.search);
  const codeFromUrl = params.get("code");
  if (codeFromUrl) {
    document.getElementById("code").value = codeFromUrl.trim().toUpperCase();
  }

  const submitBtn = document.getElementById("submit-btn");
  const codeInput = document.getElementById("code");
  const nameInput = document.getElementById("name");
  const errorMsg = document.getElementById("error-msg");
  const formView = document.getElementById("form-view");
  const successView = document.getElementById("success-view");
  const keyDisplay = document.getElementById("key-display");
  const copyBtn = document.getElementById("copy-btn");

  function showError(msg) {
    errorMsg.textContent = msg;
    errorMsg.classList.remove("hidden");
  }
  function clearError() { errorMsg.classList.add("hidden"); }

  submitBtn.addEventListener("click", async () => {
    clearError();
    const code = codeInput.value.trim().toUpperCase();
    const name = nameInput.value.trim();
    if (!code) { showError("Enter your invite code."); return; }
    if (!name) { showError("Enter a name for this device."); return; }

    submitBtn.disabled = true;
    submitBtn.textContent = "Working...";

    try {
      const r = await fetch("/onboard/redeem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, name })
      });
      const data = await r.json();

      if (!r.ok) {
        showError(data.detail || "Something went wrong.");
        submitBtn.disabled = false;
        submitBtn.textContent = "Get my key";
        return;
      }

      keyDisplay.textContent = data.api_key;
      formView.classList.add("hidden");
      successView.classList.remove("hidden");

    } catch (err) {
      showError("Network error. Please retry.");
      submitBtn.disabled = false;
      submitBtn.textContent = "Get my key";
    }
  });

  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(keyDisplay.textContent);
      copyBtn.textContent = "Copied ✓";
      copyBtn.classList.add("copied");
      setTimeout(() => {
        copyBtn.textContent = "Copy key";
        copyBtn.classList.remove("copied");
      }, 2000);
    } catch (err) {
      const range = document.createRange();
      range.selectNode(keyDisplay);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
    }
  });

  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitBtn.click();
  });
})();
</script>
</body>
</html>
"""


@router.get("/onboard", response_class=HTMLResponse)
async def onboard_page(request: Request) -> HTMLResponse:
    """Serve the onboarding HTML page."""
    html = _ONBOARD_HTML.replace(
        "SHORTCUT_URL_PLACEHOLDER",
        settings.shortcut_install_url or "#",
    )
    return HTMLResponse(content=html)

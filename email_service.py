#!/usr/bin/env python3
"""email_service.py — transactional email sending via Resend.

Used for:
- Password reset codes (self-serve "forgot password" flow)
- Purchase receipts (sent after a Stripe webhook fulfills a guide purchase)

Configuration (env vars):
- TRULIFE_RESEND_API_KEY   — Resend API key (required to actually send; if
  unset, sends are logged and skipped rather than raising, so the rest of
  the app keeps working in dev/preview without an email provider configured)
- TRULIFE_EMAIL_FROM       — sender identity, e.g.
  "Tru Life Properties <onboarding@resend.dev>" for testing, or
  "Tru Life Properties <noreply@updates.trulifeproperties.com>" once that
  domain is verified in Resend. Falls back to the Resend shared test sender.
- TRULIFE_ENV=production   — requires both of the above to be set at
  startup, same pattern as the other production secrets in api_server.py.
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("trulife.email")

RESEND_API_KEY = os.environ.get("TRULIFE_RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("TRULIFE_EMAIL_FROM", "Tru Life Properties <onboarding@resend.dev>")
EMAIL_ENABLED = bool(RESEND_API_KEY)

_RESEND_URL = "https://api.resend.com/emails"


def send_email(to: str, subject: str, html: str) -> bool:
    """Send one email via Resend. Returns True if the request succeeded.

    Never raises — a transient email-provider failure must not break the
    request that triggered it (signup, password reset, purchase). Failures
    are logged so they're visible in server logs / can be retried manually.
    """
    if not EMAIL_ENABLED:
        logger.warning("Email not sent (no TRULIFE_RESEND_API_KEY configured): to=%s subject=%r", to, subject)
        return False
    body = json.dumps({
        "from": EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }).encode()
    req = urllib.request.Request(
        _RESEND_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RESEND_API_KEY}",
            # Resend's edge rejects requests with no User-Agent (403 / error
            # code 1010) before the key is even checked — urllib sends none
            # by default, so this must be set explicitly.
            "User-Agent": "trulife-properties-app/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        logger.error("Resend send failed (%s): to=%s subject=%r detail=%s", exc.code, to, subject, detail)
        return False
    except Exception as exc:  # network errors, timeouts, etc.
        logger.error("Resend send error: to=%s subject=%r error=%s", to, subject, exc)
        return False


# ---------- Templates ----------
# Simple, inline-styled HTML — no external CSS/images so it renders
# consistently across email clients. Brand colors match the app: navy
# #031158, gold #daa61f.

_WRAPPER = """\
<div style="background:#f7f6f2;padding:32px 16px;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e2d8;">
    <div style="background:#031158;padding:24px 32px;">
      <span style="color:#daa61f;font-size:20px;font-weight:bold;letter-spacing:0.5px;">Tru Life Properties</span>
    </div>
    <div style="padding:32px;color:#1a1a1a;">
      {content}
    </div>
    <div style="padding:16px 32px 24px;color:#8a8a8a;font-size:12px;border-top:1px solid #ececec;">
      Tru Life Properties &mdash; this is an automated message.
    </div>
  </div>
</div>
"""


def render_reset_code_email(code: str, expires_minutes: int) -> str:
    content = f"""
      <p style="font-size:16px;margin:0 0 16px;">We received a request to reset your Tru Life Properties password.</p>
      <p style="font-size:14px;margin:0 0 8px;color:#4a4a4a;">Your verification code:</p>
      <div style="font-size:32px;font-weight:bold;letter-spacing:6px;color:#031158;background:#fdf6e1;border:1px solid #daa61f;border-radius:8px;padding:16px;text-align:center;margin:0 0 16px;">
        {code}
      </div>
      <p style="font-size:13px;color:#6a6a6a;margin:0 0 16px;">This code expires in {expires_minutes} minutes. If you didn't request this, you can safely ignore this email &mdash; your password will not be changed.</p>
    """
    return _WRAPPER.format(content=content)


def render_receipt_email(guide_name: str, amount_paid: float, purchased_at: str) -> str:
    content = f"""
      <p style="font-size:16px;margin:0 0 16px;">Thanks for your purchase &mdash; here's your receipt.</p>
      <table style="width:100%;font-size:14px;border-collapse:collapse;margin:0 0 16px;">
        <tr>
          <td style="padding:8px 0;color:#6a6a6a;">Item</td>
          <td style="padding:8px 0;text-align:right;font-weight:bold;">{guide_name} Guide</td>
        </tr>
        <tr style="border-top:1px solid #ececec;">
          <td style="padding:8px 0;color:#6a6a6a;">Amount</td>
          <td style="padding:8px 0;text-align:right;font-weight:bold;">${amount_paid:,.2f}</td>
        </tr>
        <tr style="border-top:1px solid #ececec;">
          <td style="padding:8px 0;color:#6a6a6a;">Date</td>
          <td style="padding:8px 0;text-align:right;">{purchased_at}</td>
        </tr>
      </table>
      <p style="font-size:13px;color:#6a6a6a;margin:0;">Your guide is unlocked in your Tru Life Properties account &mdash; log in any time to view it.</p>
    """
    return _WRAPPER.format(content=content)


def render_accounting_notification_email(
    buyer_email: str, guide_name: str, amount_paid: float, purchased_at: str
) -> str:
    """Internal copy sent to the accounting inbox for every completed sale.
    Not customer-facing — plain and information-dense rather than styled
    like the buyer receipt."""
    content = f"""
      <p style="font-size:16px;margin:0 0 16px;">New purchase completed.</p>
      <table style="width:100%;font-size:14px;border-collapse:collapse;margin:0 0 16px;">
        <tr>
          <td style="padding:8px 0;color:#6a6a6a;">Buyer</td>
          <td style="padding:8px 0;text-align:right;font-weight:bold;">{buyer_email}</td>
        </tr>
        <tr style="border-top:1px solid #ececec;">
          <td style="padding:8px 0;color:#6a6a6a;">Item</td>
          <td style="padding:8px 0;text-align:right;font-weight:bold;">{guide_name} Guide</td>
        </tr>
        <tr style="border-top:1px solid #ececec;">
          <td style="padding:8px 0;color:#6a6a6a;">Amount</td>
          <td style="padding:8px 0;text-align:right;font-weight:bold;">${amount_paid:,.2f}</td>
        </tr>
        <tr style="border-top:1px solid #ececec;">
          <td style="padding:8px 0;color:#6a6a6a;">Date</td>
          <td style="padding:8px 0;text-align:right;">{purchased_at}</td>
        </tr>
      </table>
      <p style="font-size:12px;color:#8a8a8a;margin:0;">Automated accounting copy — sent for every completed guide purchase.</p>
    """
    return _WRAPPER.format(content=content)

"""Shared reminder delivery for HTTP requests and background actions."""
import logging

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

# Scheduler reference — set during application setup so dispatch_reminder can
# push a parallel in-app notification (frontend polls the scheduler's queue
# and fires real browser Notification(...) popups). Optional; works without it.
_scheduler_ref = None


async def dispatch_reminder(
    title: str,
    note_body: str,
    note_id: str,
    owner: str = "",
    queue_browser: bool = True,
    settings_override: dict | None = None,
) -> dict:
    """Fire a reminder via the configured channel (browser/email/ntfy/webhook).

    Args:
        title: short headline shown to the user
        note_body: longer body text
        note_id: stable id (used as tag/dedupe in browser notifications)
        owner: the user this reminder belongs to — scopes SMTP config to
               their account so we don't cross-leak credentials

    Returns: {synthesis, email_sent, ntfy_sent}. Browser channel is wired via
    the in-memory notification queue picked up by the frontend poller, so
    nothing is "sent" synchronously for it — the channel just routes there.
    """
    from src.settings import load_settings
    settings = {**load_settings(), **(settings_override or {})}
    channel = settings.get("reminder_channel", "browser")
    llm_on = bool(settings.get("reminder_llm_synthesis", False))
    title = (title or "").strip()
    note_body = (note_body or "").strip()
    cache_key = str(note_id) if note_id else ""
    cache = {}
    cache_path = None
    if cache_key:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            from pathlib import Path as _P
            _slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
            cache_path = _P(DATA_DIR) / f"note_pings_{_slug}.json"
            if cache_path.exists():
                cache = _json.loads(cache_path.read_text(encoding="utf-8"))
            last = cache.get(cache_key)
            if last:
                last_channel = None
                if isinstance(last, dict):
                    last_channel = last.get("channel")
                    last = last.get("at")
                last_dt = _dt.fromisoformat(str(last))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_tz.utc)
                # Legacy cache values were plain timestamps and could be
                # written by the frontend even when the email/ntfy send failed.
                # Treat those as browser-only dedupe so email reminders can be
                # retried by the backend scanner after a failed frontend path.
                should_skip = last_dt >= _dt.now(_tz.utc) - _td(minutes=25)
                if should_skip and channel in ("email", "ntfy", "webhook"):
                    should_skip = last_channel == channel
                if should_skip:
                    return {
                        "synthesis": None,
                        "email_sent": False,
                        "ntfy_sent": False,
                        "webhook_sent": False,
                        "browser_sent": True,
                        "skipped": True,
                    }
        except Exception as _e:
            logger.debug(f"dispatch_reminder: cache read failed: {_e}")

    synthesis = None
    _SYNTH_FAILED_TAG = "[utility model unavailable — no summary generated]"
    if llm_on:
        try:
            from src.endpoint_resolver import resolve_endpoint
            from src.llm_core import llm_call_async
            url, model, headers = resolve_endpoint("utility", owner=owner or None)
            if not url:
                url, model, headers = resolve_endpoint("default", owner=owner or None)
            if url and model:
                raw = await llm_call_async(
                    url=url, model=model,
                    messages=[
                        {"role": "system", "content": "You are a reminder assistant. Write a single short, warm, motivating sentence (max 25 words) reminding the user about the note below. Do not add greetings, preamble, or hashtags. Output only the sentence."},
                        {"role": "user", "content": f"Title: {title}\n\n{note_body}".strip()},
                    ],
                    temperature=0.7, max_tokens=200, headers=headers, timeout=30,
                )
                from src.text_helpers import strip_think as _strip_think
                # prose=True strips untagged "The user wants me to…" chain-of-thought.
                # prompt_echo=True strips Qwen-style "Thinking Process:" / leaked
                # prompt prefixes. Both are safe here because this is a
                # one-sentence LLM-only output, not user-pasted content.
                synthesis = _strip_think(raw or "", prose=True, prompt_echo=True)
                # Reminder synthesis is supposed to be ONE sentence. Strip-think's
                # paragraph-based heuristic misses cases where the model puts
                # reasoning + answer on consecutive lines inside one paragraph
                # (e.g. "I should write... [\n] You have one task waiting...").
                # Walk lines, drop reasoning/prompt-echo lines, then keep the
                # last surviving line — that's the actual warm sentence.
                if synthesis:
                    import re as _re
                    # Tightened: target ACTUAL self-talk (model narrating what
                    # it'll do) rather than any first-person sentence. The old
                    # pattern killed legit warm sentences like "I'll see you
                    # tomorrow" or "I should be done by then". New rules:
                    #  • "I (need|should|have|'ll|will) (write|draft|reply|…)"
                    #    only matches when followed by a TASK verb taking an
                    #    OBJECT (so first-person + intransitive verb passes).
                    #  • Self-instructional patterns the model emits verbatim:
                    #    "I should write something that reminds them…",
                    #    "I need to draft…", "Let me think…".
                    #  • Explicit instructions echoed back from the prompt:
                    #    "Keep it under 25 words", "No greetings".
                    _reasoning = _re.compile(
                        r"^\s*(?:"
                        # "I should write/draft/compose…" with a task-object follow
                        r"i (?:need|should|have|'ll|will|am going|am)\s+to\s+"
                        r"(?:write|draft|compose|craft|generate|produce|create|"
                        r"summarize|answer|provide|note|address|remind|output)"
                        r"\s+(?:a |an |the |something|this|that|here|them|him|her|"
                        r"you|user|reply|response|sentence|message|line|warm)|"
                        # The model literally narrating about the user
                        r"the user (?:wants|is asking|asks|needs|wrote|said|requested) (?:me )?(?:to|for|that|about|something)|"
                        # "Let me [think/write/draft/…] (about/for/the …)"
                        r"let me (?:think|write|draft|consider|note|see|check)\b\s+(?:about|for|the|this|that|if|whether)|"
                        # "Looking at the/this/that …"
                        r"looking at (?:the|this|that)\b|"
                        # "Based on the/this/what …"
                        r"based on (?:the|this|what|context|that)\b|"
                        # Prompt-echo of length / style instructions
                        r"keep it under \d+ words\b|"
                        r"(?:no greetings|no preamble|no hashtags|just output the)\b"
                        r").*",
                        _re.IGNORECASE,
                    )
                    # Echo of the prompt's "Pending:" / "<N> pending" tail.
                    _echo = _re.compile(
                        r"^\s*(?:pending\s*[:.]|(?:\d+|one|two|three|four|five)\s+pending\b)",
                        _re.IGNORECASE,
                    )
                    lines = [ln for ln in synthesis.splitlines() if ln.strip()]
                    cleaned = [ln for ln in lines if not _reasoning.match(ln) and not _echo.match(ln)]
                    if cleaned:
                        # The model's actual answer is normally the LAST surviving
                        # line — reasoning leads, answer trails.
                        synthesis = cleaned[-1].strip()
            else:
                synthesis = _SYNTH_FAILED_TAG
        except Exception as e:
            logger.warning(f"Reminder LLM synthesis failed: {e}")
            synthesis = _SYNTH_FAILED_TAG
        if synthesis:
            _s = synthesis.strip(); _low = _s.lower()
            if (not _s or _low.startswith("error:") or _low.startswith("[error")
                    or "operation failed" in _low
                    or ("upstream" in _low and "failed" in _low)) and synthesis != _SYNTH_FAILED_TAG:
                logger.warning(f"Reminder synthesis looked like an error, replacing: {_s[:120]!r}")
                synthesis = _SYNTH_FAILED_TAG

    email_sent = False
    email_error = ""
    if channel == "email":
        try:
            from routes.email_routes import _get_email_config
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from datetime import datetime as _dt
            # `reminder_email_account_id` lets the user pick WHICH email
            # account to send reminders from (when they have several
            # configured in Integrations). Falls back to the default
            # account when no explicit choice is saved.
            _acc_id = (settings.get("reminder_email_account_id") or "").strip() or None
            cfg = _get_email_config(account_id=_acc_id, owner=owner or "")
            if not (cfg.get("smtp_host") and cfg.get("smtp_user") and cfg.get("smtp_password")):
                try:
                    from core.database import SessionLocal as _SL, EmailAccount as _EA
                    from sqlalchemy import and_, or_
                    db = _SL()
                    try:
                        q = db.query(_EA).filter(_EA.enabled == True)  # noqa: E712
                        if owner:
                            unowned = or_(_EA.owner == None, _EA.owner == "")  # noqa: E711
                            same_mailbox = or_(_EA.imap_user == owner, _EA.from_address == owner)
                            q = q.filter(or_(_EA.owner == owner, and_(unowned, same_mailbox)))
                        for row in q.order_by(_EA.is_default.desc(), _EA.created_at.asc()).all():
                            trial = _get_email_config(account_id=row.id, owner=owner or "")
                            if trial.get("smtp_host") and trial.get("smtp_user") and trial.get("smtp_password"):
                                cfg = trial
                                break
                    finally:
                        db.close()
                except Exception as _fallback_error:
                    logger.debug(f"Reminder SMTP fallback lookup failed: {_fallback_error}")
            from_addr = (cfg.get("from_address") or cfg.get("smtp_user") or "").strip()
            recipient = (settings.get("reminder_email_to") or "").strip() or from_addr
            # Loud diagnostic so we can see WHY a reminder didn't send (the
            # previous "silently no-op when cfg has no smtp_host" was invisible).
            logger.info(
                "dispatch_reminder[email] note_id=%s owner=%r "
                "has_smtp_host=%s has_smtp_user=%s has_from=%s has_recipient=%s",
                note_id, owner,
                bool(cfg.get("smtp_host")), bool(cfg.get("smtp_user")),
                bool(from_addr), bool(recipient),
            )
            missing = []
            if not cfg.get("smtp_host"):
                missing.append("SMTP host")
            if not cfg.get("smtp_user"):
                missing.append("SMTP user")
            if not cfg.get("smtp_password"):
                missing.append("SMTP password")
            if not from_addr:
                missing.append("from address")
            if not recipient:
                missing.append("recipient")
            if missing:
                email_error = "Missing " + ", ".join(missing)
                logger.warning(
                    "Reminder email not sent for note_id=%s account=%r: %s",
                    note_id, cfg.get("account_name"), email_error,
                )
            else:
                msg = MIMEMultipart("alternative")
                msg["From"] = from_addr
                msg["To"] = recipient
                _t = title or 'Note'
                _t = _t[len('Reminder:'):].strip() if _t.lower().startswith('reminder:') else _t
                msg["Subject"] = f"Reminder (Odysseus): {_t}"
                msg["Date"] = _dt.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
                msg["X-Odysseus-Origin"] = "odysseus-ui"
                msg["X-Odysseus-Kind"] = "reminder"
                msg["X-Odysseus-Ref"] = str(note_id)
                # Body shape: synthesis (warm sentence) → blank line → bold
                # title header → note details. The title was previously only
                # in the subject line, so the email read like a faceless
                # to-do list with no anchor to which note triggered it.
                _body_chunks = []
                if synthesis:
                    _body_chunks.append(synthesis)
                if _t:
                    _body_chunks.append(_t)
                if note_body:
                    _body_chunks.append(note_body)
                plain = "\n\n".join(_body_chunks) if _body_chunks else title
                msg.attach(MIMEText(plain, "plain", "utf-8"))

                def _smtp_send():
                    from routes.email_helpers import _send_smtp_message
                    _send_smtp_message(cfg, from_addr, [recipient], msg.as_string())

                import asyncio as _aio
                await _aio.to_thread(_smtp_send)
                email_sent = True
        except Exception as e:
            email_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder email send failed: {e}")

    webhook_sent = False
    webhook_error = ""
    if channel == "webhook":
        try:
            import httpx
            import json as _wjson
            from src.integrations import load_integrations
            # Built-in payload defaults for known presets so users don't have
            # to configure a template just to use a standard service.
            _PRESET_TEMPLATE_DEFAULTS = {
                "discord_webhook": '{"embeds": [{"title": "{{title}}", "description": "{{message}}", "color": 5793266}]}',
            }
            intg_id = settings.get("reminder_webhook_integration_id", "").strip()
            template = settings.get("reminder_webhook_payload_template", "").strip()
            if not intg_id:
                webhook_error = "No webhook integration selected"
            else:
                intg = next(
                    (i for i in load_integrations()
                     if i.get("id") == intg_id and i.get("base_url")),
                    None,
                )
                if not intg:
                    webhook_error = f"Integration {intg_id!r} not found or missing base URL"
                else:
                    # Fall back to a built-in default for known presets so
                    # users don't have to configure a template for standard
                    # services like Discord.
                    if not template:
                        template = _PRESET_TEMPLATE_DEFAULTS.get(intg.get("preset", ""), "")
                    if not template:
                        webhook_error = "No payload template configured"
                    else:
                        # Render template: JSON-escape the values so the result
                        # is always valid JSON regardless of special characters.
                        # dumps() returns `"value"` — strip outer quotes.
                        msg = (synthesis or note_body or title or "Reminder")[:4000]
                        _t = _wjson.dumps(title or "Reminder")[1:-1]
                        _m = _wjson.dumps(msg)[1:-1]
                        rendered = template.replace("{{title}}", _t).replace("{{message}}", _m)
                        hdrs = {"Content-Type": "application/json"}
                        api_key = intg.get("api_key", "")
                        auth_type = (intg.get("auth_type") or "none").lower()
                        if api_key:
                            if auth_type == "bearer":
                                hdrs["Authorization"] = f"Bearer {api_key}"
                            elif auth_type == "header":
                                hdrs[intg.get("auth_header") or "Authorization"] = api_key
                        url = intg["base_url"].rstrip("/")
                        # SSRF guard — matches the pattern used by webhook_routes,
                        # CalDAV, search, and embeddings. Blocks link-local / metadata
                        # addresses (169.254.x.x) by default; set
                        # REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS=true to also block
                        # RFC-1918 ranges for locked-down deployments.
                        import os as _os
                        from src.url_safety import check_outbound_url as _chk
                        _block = _os.getenv("REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS", "false").lower() == "true"
                        _ok, _reason = _chk(url, block_private=_block)
                        if not _ok:
                            webhook_error = f"Webhook URL rejected: {_reason}"
                        else:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                resp = await client.post(url, content=rendered.encode(), headers=hdrs)
                                webhook_sent = resp.is_success
                                if not webhook_sent:
                                    webhook_error = f"Webhook returned HTTP {resp.status_code}"
        except Exception as e:
            webhook_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder webhook send failed: {e}")

    ntfy_sent = False
    ntfy_error = ""
    if channel == "ntfy":
        try:
            from src.integrations import load_integrations
            import httpx
            intg = next(
                (i for i in load_integrations()
                 if i.get("preset") == "ntfy" and i.get("enabled", True) and i.get("base_url")),
                None,
            )
            if intg:
                base = intg["base_url"].rstrip("/")
                topic = settings.get("reminder_ntfy_topic") or "reminders"
                ntfy_body = synthesis or note_body or title
                hdrs = {"Title": title or "Reminder", "Priority": "high", "Tags": "bell"}
                api_key = intg.get("api_key", "")
                if api_key:
                    hdrs["Authorization"] = f"Bearer {api_key}"
                # SSRF guard — same check (and env knob) as the webhook branch
                # above: link-local / metadata addresses are always rejected;
                # REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS=true also blocks RFC-1918
                # so a ntfy base_url can't be pointed at internal services.
                import os as _os
                from src.url_safety import check_outbound_url as _chk
                _block = _os.getenv("REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS", "false").lower() == "true"
                _ok, _reason = _chk(f"{base}/{topic}", block_private=_block)
                if not _ok:
                    ntfy_error = f"ntfy URL rejected: {_reason}"
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(f"{base}/{topic}", content=ntfy_body, headers=hdrs)
                        ntfy_sent = resp.is_success
                        if not ntfy_sent:
                            ntfy_error = f"ntfy returned HTTP {resp.status_code}"
            else:
                ntfy_error = "No enabled ntfy integration"
        except Exception as e:
            ntfy_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder ntfy send failed: {e}")

    # In-app browser notification ALWAYS fires (regardless of channel). The
    # frontend polls `/api/tasks/notifications` and turns any entry with a
    # `body` into a real `Notification(...)` — same surface as task-success
    # popups. Lets the user see reminders inside the app even when the
    # primary channel is email/ntfy and the tab is open.
    browser_sent = False
    local_browser_sent = (not queue_browser and channel == "browser")
    if queue_browser and _scheduler_ref is not None:
        try:
            _scheduler_ref.add_notification(
                task_name=title or "Reminder",
                status="success",
                task_id=f"reminder-{note_id}",
                owner=owner or None,
                body=(synthesis or note_body or title or "").strip()[:500] or "Reminder",
            )
            browser_sent = True
        except Exception as _e:
            logger.debug(f"dispatch_reminder: in-app notif push failed: {_e}")

    # Dedupe across paths: write to the same cache file `action_ping_notes`
    # reads, so the background scanner's REPING_MIN window suppresses a
    # second send for the same note within 25 min. Without this, a note
    # whose due_date fires while the user has the app open got TWO emails
    # (frontend-fired here + background-fired by ping_notes 0–5 min later).
    if (email_sent or ntfy_sent or webhook_sent or browser_sent or local_browser_sent) and note_id:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            from pathlib import Path as _P
            # Per-owner cache so the scanner's prune step on user A's run
            # doesn't drop user B's just-fired entry (review C4).
            _STATE = cache_path
            if _STATE is None:
                _slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
                _STATE = _P(DATA_DIR) / f"note_pings_{_slug}.json"
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            try:
                _cache = cache or (_json.loads(_STATE.read_text(encoding="utf-8")) if _STATE.exists() else {})
            except Exception:
                _cache = {}
            sent_channel = "email" if email_sent else "ntfy" if ntfy_sent else "webhook" if webhook_sent else "browser"
            _cache[cache_key or str(note_id)] = {
                "at": _dt.now(_tz.utc).isoformat(),
                "channel": sent_channel,
            }
            _STATE.write_text(_json.dumps(_cache), encoding="utf-8")
        except Exception as _e:
            logger.debug(f"dispatch_reminder: cache write failed: {_e}")

    return {
        "channel": channel,
        "synthesis": synthesis,
        "email_sent": email_sent,
        "email_error": email_error,
        "ntfy_sent": ntfy_sent,
        "ntfy_error": ntfy_error,
        "webhook_sent": webhook_sent,
        "webhook_error": webhook_error,
        "browser_sent": browser_sent or local_browser_sent,
    }



def set_scheduler(scheduler) -> None:
    """Connect the optional in-app notification queue during application setup."""
    global _scheduler_ref
    _scheduler_ref = scheduler

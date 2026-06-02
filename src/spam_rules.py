"""User-defined email spam rules.

Created from the email client's 3-dot menu ("Mark as spam & block…"). A rule
captures the offending email's signals and blocks future mail by any combination
the user picks: exact SENDER, sender DOMAIN, and/or CONTENT similarity (local
embedding cosine — free, runs on the resident model). The email poller calls
apply_rules_to_inbox() each cycle to silently move matches to the Spam/Junk
folder and log every hit for review. Self-contained: no edits to the fragile
per-email LLM loop in email_pollers.

Storage lives in the email subsystem DB (scheduled_emails.db) alongside the
existing AI spam-classify cache (email_tags). All ops are best-effort and
logged; a failure here must never break mail polling.
"""
from __future__ import annotations
import json
import time
import sqlite3
import logging
import email as _email
import email.utils
from typing import Optional

logger = logging.getLogger(__name__)

# Conservative cosine threshold on L2-normalized embeddings. High on purpose:
# auto-filing a legit lookalike into Spam is worse than missing one spam.
CONTENT_SIM_THRESHOLD = 0.86
SWEEP_LIMIT = 80  # most-recent INBOX messages scanned per sweep


def _db():
    from routes.email_helpers import SCHEDULED_DB
    return sqlite3.connect(str(SCHEDULED_DB))


def init_tables():
    c = _db()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS email_spam_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            block_sender INTEGER DEFAULT 0,
            block_domain INTEGER DEFAULT 0,
            block_content INTEGER DEFAULT 0,
            sender TEXT, domain TEXT, subject TEXT,
            embedding TEXT,
            sample_uid TEXT, sample_message_id TEXT,
            created_at REAL, hits INTEGER DEFAULT 0, active INTEGER DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS email_rule_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER, account_id TEXT, uid TEXT,
            sender TEXT, subject TEXT, matched_on TEXT, score REAL,
            moved_to TEXT, created_at REAL
        )""")
        c.commit()
    finally:
        c.close()


def _addr_domain(sender: str):
    _name, addr = email.utils.parseaddr(sender or "")
    addr = (addr or "").strip().lower()
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    return addr, domain


def plain_body(msg, max_chars: int = 4000) -> str:
    """Best-effort plain-text body for embedding. Prefers text/plain, falls back
    to a crude HTML strip."""
    try:
        if msg.is_multipart():
            text, html = "", ""
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition", ""))
                if "attachment" in disp:
                    continue
                if ctype == "text/plain" and not text:
                    text = _decode_part(part)
                elif ctype == "text/html" and not html:
                    html = _decode_part(part)
            body = text or _strip_html(html)
        else:
            body = _decode_part(msg)
            if msg.get_content_type() == "text/html":
                body = _strip_html(body)
    except Exception:
        body = ""
    return (body or "").strip()[:max_chars]


def _decode_part(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    import re
    if not html:
        return ""
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html


def _embed(text: str) -> Optional[list]:
    if not text or not text.strip():
        return None
    try:
        from src.embeddings import get_embedding_client
        vec = get_embedding_client().encode([text[:4000]])
        if vec is None or getattr(vec, "size", 0) == 0:
            return None
        return [float(x) for x in vec[0]]
    except Exception as e:
        logger.warning(f"spam_rules: embed failed: {e}")
        return None


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    # encode() L2-normalizes, so cosine == dot product
    return float(sum(x * y for x, y in zip(a, b)))


def create_rule(account_id, criteria: dict, sender, subject, body,
                sample_uid="", sample_message_id="") -> int:
    """criteria = {sender:bool, domain:bool, content:bool}. Returns rule id."""
    addr, domain = _addr_domain(sender)
    emb = _embed((subject or "") + "\n\n" + (body or "")) if criteria.get("content") else None
    init_tables()
    c = _db()
    try:
        cur = c.execute(
            """INSERT INTO email_spam_rules
            (account_id, block_sender, block_domain, block_content, sender, domain,
             subject, embedding, sample_uid, sample_message_id, created_at, hits, active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0,1)""",
            (account_id or "",
             1 if criteria.get("sender") else 0,
             1 if criteria.get("domain") else 0,
             1 if criteria.get("content") else 0,
             addr, domain, (subject or "")[:300],
             json.dumps(emb) if emb else None,
             str(sample_uid), sample_message_id or "", time.time()))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def list_rules(account_id=None):
    init_tables()
    c = _db()
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT id,account_id,block_sender,block_domain,block_content,sender,"
            "domain,subject,sample_uid,created_at,hits FROM email_spam_rules "
            "WHERE active=1 ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def delete_rule(rule_id) -> bool:
    c = _db()
    try:
        c.execute("UPDATE email_spam_rules SET active=0 WHERE id=?", (rule_id,))
        c.commit()
        return True
    finally:
        c.close()


def recent_hits(limit=100):
    init_tables()
    c = _db()
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT * FROM email_rule_hits ORDER BY created_at DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def _active_rules():
    c = _db()
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT * FROM email_spam_rules WHERE active=1").fetchall()
    finally:
        c.close()


def match_email(account_id, sender, subject, body) -> Optional[dict]:
    """Return {rule_id, matched_on, score} for the first/best matching rule, else
    None. Cheap sender/domain checks first; embeds only if a content rule exists
    and nothing matched yet."""
    try:
        rules = _active_rules()
    except Exception as e:
        logger.warning(f"spam_rules: rule load failed: {e}")
        return None
    if not rules:
        return None
    addr, domain = _addr_domain(sender)
    content_rules = []
    for r in rules:
        # account scoping: a blank-account rule applies to all accounts
        racc = r["account_id"] or ""
        if racc and account_id and racc != account_id:
            continue
        if r["block_sender"] and r["sender"] and addr and addr == r["sender"]:
            return {"rule_id": r["id"], "matched_on": "sender", "score": 1.0}
        if r["block_domain"] and r["domain"] and domain and domain == r["domain"]:
            return {"rule_id": r["id"], "matched_on": "domain", "score": 1.0}
        if r["block_content"] and r["embedding"]:
            content_rules.append(r)
    if content_rules:
        emb = _embed((subject or "") + "\n\n" + (body or ""))
        if emb:
            best = None
            for r in content_rules:
                try:
                    rvec = json.loads(r["embedding"])
                except Exception:
                    continue
                s = _cosine(emb, rvec)
                if s >= CONTENT_SIM_THRESHOLD and (best is None or s > best["score"]):
                    best = {"rule_id": r["id"], "matched_on": "content", "score": s}
            if best:
                return best
    return None


def log_hit(rule_id, account_id, uid, sender, subject, matched_on, score, moved_to):
    try:
        c = _db()
        c.execute(
            """INSERT INTO email_rule_hits
            (rule_id, account_id, uid, sender, subject, matched_on, score, moved_to, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (rule_id, account_id or "", str(uid), sender or "", (subject or "")[:300],
             matched_on, float(score), moved_to or "", time.time()))
        c.execute("UPDATE email_spam_rules SET hits=hits+1 WHERE id=?", (rule_id,))
        c.commit()
        c.close()
    except Exception as e:
        logger.warning(f"spam_rules: log_hit failed: {e}")


def move_to_spam(uid, account_id=None, owner="", src_folder="INBOX") -> str:
    """Move a single uid to the detected Spam/Junk folder. Returns the dest
    folder name on success, '' on failure."""
    from routes.email_helpers import _imap_connect, _detect_spam_folder, _imap_move
    dest = ""
    try:
        conn = _imap_connect(account_id, owner)
        dest = _detect_spam_folder(conn) or "[Gmail]/Spam"
        try:
            conn.logout()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"spam_rules: spam-folder detect failed: {e}")
        dest = "[Gmail]/Spam"
    uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
    ok = _imap_move(uid_s, dest, src_folder)
    return dest if ok else ""


def fetch_email_fields(uid, folder="INBOX", account_id=None, owner="") -> Optional[dict]:
    """Fetch sender / subject / body / message-id for one uid."""
    from routes.email_helpers import _imap_connect, _decode_header, _q
    try:
        conn = _imap_connect(account_id, owner)
        conn.select(_q(folder))
        uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
        typ, md = conn.fetch(uid_s, "(RFC822)")
        try:
            conn.logout()
        except Exception:
            pass
        if not md or not md[0]:
            return None
        raw = md[0][1]
        msg = _email.message_from_bytes(raw)
        return {
            "sender": _decode_header(msg.get("From", "")),
            "subject": _decode_header(msg.get("Subject", "")),
            "body": plain_body(msg),
            "message_id": (msg.get("Message-ID", "") or "").strip(),
        }
    except Exception as e:
        logger.warning(f"spam_rules: fetch uid={uid} failed: {e}")
        return None


def apply_rules_to_inbox(account_id=None, owner="", limit=SWEEP_LIMIT) -> int:
    """Scan the most-recent INBOX messages; move rule-matches to Spam and log
    each. Returns count moved. Two-phase (collect UIDs, then move) so expunging
    doesn't disturb the live scan. Safe to call repeatedly."""
    from routes.email_helpers import _imap_connect, _decode_header
    try:
        if not _active_rules():
            return 0
    except Exception:
        return 0
    matches = []  # (uid_s, sender, subject, match)
    try:
        conn = _imap_connect(account_id, owner)
        conn.select("INBOX")
        typ, data = conn.search(None, "ALL")
        uids = (data[0].split() if data and data[0] else [])[-int(limit):]
        for uid in uids:
            try:
                typ, md = conn.fetch(uid, "(RFC822)")
                if not md or not md[0]:
                    continue
                msg = _email.message_from_bytes(md[0][1])
                sender = _decode_header(msg.get("From", ""))
                subject = _decode_header(msg.get("Subject", ""))
                m = match_email(account_id, sender, subject, plain_body(msg))
                if m:
                    uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
                    matches.append((uid_s, sender, subject, m))
            except Exception as e:
                logger.debug(f"spam_rules sweep uid={uid}: {e}")
        try:
            conn.logout()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"spam_rules: inbox sweep failed: {e}")
        return 0

    moved = 0
    for uid_s, sender, subject, m in matches:
        dest = move_to_spam(uid_s, account_id, owner)
        if dest:
            log_hit(m["rule_id"], account_id, uid_s, sender, subject,
                    m["matched_on"], m["score"], dest)
            moved += 1
    if moved:
        logger.info(f"spam_rules: swept {moved} email(s) to Spam")
    return moved

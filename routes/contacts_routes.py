"""HTTP API for the shared local/CardDAV contacts backend."""
from fastapi import APIRouter, Query, Depends, Response, HTTPException

from core.middleware import require_admin

from src import contacts as contacts_backend


# ── Routes ──

def setup_contacts_routes():
    router = APIRouter(prefix="/api/contacts", tags=["contacts"])

    @router.get("/list")
    async def list_contacts(_admin: str = Depends(require_admin)):
        """List all contacts_backend."""
        contacts = contacts_backend._fetch_contacts()
        return {"contacts": contacts, "count": len(contacts)}

    @router.get("/search")
    async def search_contacts(q: str = Query(""), _admin: str = Depends(require_admin)):
        """Search contacts by name or email. Returns up to 10 matches."""
        contacts = contacts_backend._fetch_contacts()
        if not q:
            return {"results": []}
        q_lower = q.lower()
        results = []
        for c in contacts:
            if q_lower in c["name"].lower():
                results.append(c)
                continue
            for em in c["emails"]:
                if q_lower in em.lower():
                    results.append(c)
                    break
        return {"results": results[:10]}

    @router.post("/add")
    async def add_contact(data: dict, _admin: str = Depends(require_admin)):
        """Add a new contact."""
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        if not email:
            return {"success": False, "error": "Email required"}
        # Check if already exists
        contacts = contacts_backend._fetch_contacts()
        for c in contacts:
            if email.lower() in [e.lower() for e in c["emails"]]:
                return {"success": True, "message": "Already exists", "contact": c}
        if not name:
            name = email.split("@")[0]
        ok = contacts_backend._create_contact(name, email)
        return {"success": ok}

    @router.post("/import")
    async def import_vcf(data: dict, _admin: str = Depends(require_admin)):
        """Import contacts from .vcf or CSV. Body: {"vcf": "..."} or {"csv": "..."}."""
        text = data.get("vcf") or data.get("text") or ""
        csv_text = data.get("csv") or ""
        if text.strip():
            if "BEGIN:VCARD" not in text.upper():
                return {"success": False, "error": "No vCard data found"}
            result = contacts_backend._import_vcards(text)
        elif csv_text.strip():
            result = contacts_backend._import_csv_contacts(csv_text)
        else:
            return {"success": False, "error": "No contact data found"}
        result["success"] = result.get("imported", 0) > 0
        return result

    @router.get("/export")
    async def export_contacts(
        format: str = Query("vcf", pattern="^(vcf|csv)$"),
        _admin: str = Depends(require_admin),
    ):
        """Export all contacts as vCard or CSV."""
        contacts = contacts_backend._fetch_contacts(force=True)
        if format == "csv":
            content = contacts_backend._contacts_to_csv(contacts)
            media_type = "text/csv; charset=utf-8"
            filename = "odysseus-contacts_backend.csv"
        else:
            content = contacts_backend._contacts_to_vcf(contacts)
            media_type = "text/vcard; charset=utf-8"
            filename = "odysseus-contacts_backend.vcf"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/config")
    async def get_config(_admin: str = Depends(require_admin)):
        cfg = contacts_backend._get_carddav_config()
        # Mask password
        if cfg["password"]:
            cfg["password"] = "***"
        return cfg

    @router.put("/config")
    async def update_config(data: dict, _admin: str = Depends(require_admin)):
        settings = contacts_backend._load_settings()
        for key in ("carddav_url", "carddav_username", "carddav_password"):
            if key in data:
                if key == "carddav_url" and str(data[key] or "").strip():
                    try:
                        settings[key] = contacts_backend._validate_carddav_url(data[key])
                    except ValueError as e:
                        raise HTTPException(400, str(e))
                else:
                    value = data[key]
                    if key == "carddav_password" and value:
                        from src.secret_storage import encrypt
                        value = encrypt(value)
                    settings[key] = value
        contacts_backend._save_settings(settings)
        # Force re-fetch
        contacts_backend._contact_cache["fetched_at"] = None
        return {"success": True}

    @router.delete("/clear")
    async def clear_contacts(_admin: str = Depends(require_admin)):
        """Clear all local contacts_backend. If CardDAV is configured, only clears the local fallback cache."""
        contacts_backend._save_local_contacts([])
        return {"success": True}

    # NOTE: the /{uid} routes are declared LAST so the literal paths above
    # (/list, /search, /add, /config) win — otherwise PUT /config would
    # match PUT /{uid} with uid="config".
    @router.put("/{uid}")
    async def edit_contact(uid: str, data: dict, _admin: str = Depends(require_admin)):
        """Edit an existing contact — name / emails / phones."""
        name = (data.get("name") or "").strip()
        emails = data.get("emails")
        phones = data.get("phones")
        if emails is None and data.get("email"):
            emails = [data["email"]]
        emails = [e.strip() for e in (emails or []) if e and e.strip()]
        phones = [p.strip() for p in (phones or []) if p and p.strip()]
        if not name and not emails:
            return {"success": False, "error": "Name or email required"}
        if not name and emails:
            name = emails[0].split("@")[0]
        ok = contacts_backend._update_contact(uid, name, emails, phones)
        return {"success": ok}

    @router.delete("/{uid}")
    async def delete_contact(uid: str, _admin: str = Depends(require_admin)):
        """Delete a contact by UID."""
        if not uid:
            return {"success": False, "error": "UID required"}
        ok = contacts_backend._delete_contact(uid)
        return {"success": ok}

    return router

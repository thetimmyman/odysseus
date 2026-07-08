"""src/routing_task_io.py — shared task-JSON-to-RoutingTask mapping, used by
both scripts/odysseus-route and scripts/odysseus-run (kept as one function so
a new field only needs updating in one place)."""
import json
import uuid


def task_kwargs_from_json(data: dict) -> dict:
    """`data` matches the spec's OdysseusTask JSON shape. Returns kwargs ready
    for `core.database.RoutingTask(**kwargs)` -- caller decides whether to
    persist it."""
    routing = data.get("routing") or {}
    return dict(
        id=data.get("id") or str(uuid.uuid4()),
        work_item_id=data.get("workItemId"),
        title=data.get("title", ""),
        objective=data.get("objective", ""),
        task_type=data.get("type", "diff_review"),
        repo_path=data.get("repoPath", "."),
        branch_name=data.get("branchName"),
        risk=data.get("risk", "low"),
        constraints=json.dumps(data.get("constraints") or []),
        inputs=json.dumps(data.get("inputs") or {}),
        max_cost_usd=routing.get("maxCostUsd"),
        allow_free_models=routing.get("allowFreeModels", True),
        allow_paid_models=routing.get("allowPaidModels", False),
        allow_premium_models=routing.get("allowPremiumModels", False),
        max_attempts=routing.get("maxAttempts", 3),
        data_sensitivity=data.get("dataSensitivity", "internal"),
        verification_mode=data.get("verificationMode"),
        owner=data.get("owner"),
        status="pending",
    )


def task_payload_from_row(task) -> dict:
    """Inverse of task_kwargs_from_json: serialize a RoutingTask row back to the
    OdysseusTask JSON shape that CoordinatorClient.decide() expects as the model
    prompt. Used by the server-side /coordinator/decide path and the
    odysseus-coordinator CLI so a stored task can be handed to the resident
    coordinator without reconstructing the JSON by hand."""
    def _loads(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 — a corrupt column degrades to the default
            return default

    return {
        "id": task.id,
        "workItemId": getattr(task, "work_item_id", None),
        "title": task.title or "",
        "objective": task.objective or "",
        "type": task.task_type,
        "repoPath": task.repo_path,
        "branchName": getattr(task, "branch_name", None),
        "risk": task.risk,
        "constraints": _loads(getattr(task, "constraints", None), []),
        "inputs": _loads(getattr(task, "inputs", None), {}),
        "dataSensitivity": getattr(task, "data_sensitivity", None) or "internal",
        "verificationMode": getattr(task, "verification_mode", None),
        "owner": getattr(task, "owner", None),
        "routing": {
            "maxCostUsd": getattr(task, "max_cost_usd", None),
            "allowFreeModels": bool(getattr(task, "allow_free_models", True)),
            "allowPaidModels": bool(getattr(task, "allow_paid_models", False)),
            "allowPremiumModels": bool(getattr(task, "allow_premium_models", False)),
            "maxAttempts": getattr(task, "max_attempts", 3),
        },
    }


def load_or_replace_task(db, data: dict, save: bool):
    """Build a RoutingTask row from JSON; if `save`, persist it -- updating an
    existing row with the same id IN PLACE rather than delete-then-recreate.
    RoutingRun.task_id is ON DELETE CASCADE, so a delete+recreate on a
    repeated task id would silently wipe out every prior RoutingRun/
    RoutingModelRun (including Phase 2 scores) for that task -- exactly the
    re-run-to-iterate workflow this function exists to support. Updating in
    place preserves that history."""
    from core.database import RoutingTask

    kwargs = task_kwargs_from_json(data)
    if save:
        existing = db.get(RoutingTask, kwargs["id"])
        if existing:
            for field, value in kwargs.items():
                if field == "id":
                    continue
                setattr(existing, field, value)
            db.commit()
            db.refresh(existing)
            return existing
        row = RoutingTask(**kwargs)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    return RoutingTask(**kwargs)

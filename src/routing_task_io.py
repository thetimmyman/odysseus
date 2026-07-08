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
        owner=data.get("owner"),
        status="pending",
    )


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

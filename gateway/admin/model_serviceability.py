"""Keeping `models.enabled` honest about whether anything can still serve the model.

Deliberately free of other admin imports. Both `control_plane` and the reconcile
flow need this, and `reconcile_guards` -> `reconcile_models` -> `control_plane`
already runs the other way, so anything these two callers share has to sit below
all three.
"""

from collections.abc import Iterable
from typing import Any

# A catalogue model is serviceable exactly when some enabled provider still reaches
# it through an enabled route on an enabled mapping.
_LIVE_ROUTE = """
    select 1 from public.model_routes r
    join public.provider_models pm on pm.id = r.provider_model_id
    join public.providers p on p.id = pm.provider_id
    where r.model_id = m.id and r.enabled and pm.enabled and p.enabled
"""


async def sync_model_serviceability(
    connection: Any, model_ids: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Realign models.enabled with whether a live route still reaches each model.

    Syncing both ways keeps a save from doing either half: leaving an orphan enabled
    would block every later publish as stranded, and leaving a model disabled once its
    route chain is whole again makes it 404 with a message that blames the routes.

    That second half is why this is shared rather than private to the reconcile flow.
    A provider saved disabled strands its own models, and enabling the provider
    afterwards through the plain provider edit used to leave them switched off with
    nothing in the response saying so - the model row was the one link no operator
    thought to check, because they never turned it off.

    Models not named in `model_ids` are not judged here. Returns the ids actually
    changed, as (disabled, enabled).
    """
    candidates = sorted(set(model_ids))
    if not candidates:
        return [], []
    disabled = [
        row["id"]
        for row in await connection.fetch(
            f"""update public.models m set enabled=false,updated_at=now()
                where m.enabled and m.id = any($1::text[])
                  and not exists ({_LIVE_ROUTE})
                returning id""",
            candidates,
        )
    ]
    enabled = [
        row["id"]
        for row in await connection.fetch(
            f"""update public.models m set enabled=true,updated_at=now()
                where not m.enabled and m.id = any($1::text[])
                  and exists ({_LIVE_ROUTE})
                returning id""",
            candidates,
        )
    ]
    return disabled, enabled


async def mapped_model_ids(connection: Any, provider_id: str) -> list[str]:
    """Every catalogue model this provider has a mapping for."""
    return [
        row["model_id"]
        for row in await connection.fetch(
            "select distinct model_id from public.provider_models where provider_id=$1",
            provider_id,
        )
    ]

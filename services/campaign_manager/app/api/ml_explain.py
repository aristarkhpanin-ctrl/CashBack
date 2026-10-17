"""ML-объяснения (фаза 25; этап B — B27, B35). Всё — право ``analytics``.

* ``GET /ml/customers`` — последняя рекомендация на пользователя (её
  ``model_score`` = вероятность принятия) + витринный сегмент: ростер для
  левой панели.
* ``GET /ml/explain/{user_id}`` — SHAP-объяснение: прокси в read-only
  ``GET /recommendations/{user_id}/explain`` recommendation_api с сервисным
  токеном (iss ``campaign-manager``, ``SERVICE_JWT_SECRET``). Раньше фронт
  звал боевой ``GET /recommendations/{user_id}`` напрямую admin-токеном: тот
  пишет показ в ``recommendations``, шлёт ``recommendations.created`` (push
  клиенту) и тратит anti-fatigue — теперь он только для mobile_api (B27).
  Ответ rec_api отдаётся как есть; его 404 (клиента нет в feature store)
  → 404, прочие 4xx/5xx, таймаут и обрыв связи → 502 с коротким detail.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.rbac import require_permission
from app.reference_data import SEGMENT_NAMES
from app.schemas import MlCustomer
from app.security import issue_service_token
from app.segments import bucket_of

log = structlog.get_logger("api.ml_explain")

router = APIRouter(
    prefix="/ml", tags=["ml"],
    dependencies=[Depends(require_permission("analytics"))],
)

_CUSTOMERS_SQL = text(
    """
    SELECT customer_id, external_id, segment_id, prediction FROM (
        SELECT DISTINCT ON (r.user_id)
               r.user_id::text AS customer_id,
               u.external_id   AS external_id,
               u.segment_id    AS segment_id,
               r.model_score   AS prediction
          FROM recommendations r
          JOIN users u USING (user_id)
         ORDER BY r.user_id, r.generated_at DESC
    ) t
    ORDER BY prediction DESC NULLS LAST
    LIMIT :limit
    """
)


@router.get("/customers", response_model=list[MlCustomer])
async def ml_customers(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session_dep),
) -> list[MlCustomer]:
    rows = (await session.execute(_CUSTOMERS_SQL, {"limit": limit})).mappings().all()
    out: list[MlCustomer] = []
    for r in rows:
        bucket = bucket_of(r["segment_id"])
        cid = str(r["customer_id"])
        out.append(MlCustomer(
            customer_id=cid,
            name=r["external_id"] or f"Клиент {cid[:8]}",
            segment=SEGMENT_NAMES.get(bucket, "—") if bucket else "—",
            prediction=round(float(r["prediction"] or 0.0), 4),
        ))
    return out


def _bad_gateway(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


@router.get("/explain/{user_id}", response_model=dict[str, Any])
async def ml_explain(
    request: Request,
    user_id: uuid.UUID,
    top_k: int = Query(default=5, ge=1, le=20),
) -> dict[str, Any]:
    # Клиент с base_url и таймаутом (RECOMMENDATION_API_*) создаёт lifespan.
    client: httpx.AsyncClient = request.app.state.rec_api_client
    try:
        resp = await client.get(
            f"/recommendations/{user_id}/explain",
            params={"top_k": top_k},
            headers={"Authorization": f"Bearer {issue_service_token()}"},
        )
    except httpx.TimeoutException as exc:
        log.warning("explain_upstream_timeout", user_id=str(user_id), error=repr(exc))
        raise _bad_gateway("recommendation service timed out") from exc
    except httpx.HTTPError as exc:
        log.warning("explain_upstream_unavailable", user_id=str(user_id),
                    error=repr(exc))
        raise _bad_gateway("recommendation service unavailable") from exc

    if resp.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=_upstream_detail(resp) or "user not found")
    if resp.status_code != status.HTTP_200_OK:
        # 401/403 здесь — рассинхрон SERVICE_JWT_SECRET между сервисами.
        log.error("explain_upstream_error", user_id=str(user_id),
                  status=resp.status_code, detail=_upstream_detail(resp))
        raise _bad_gateway(f"recommendation service returned {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        log.error("explain_upstream_bad_body", user_id=str(user_id))
        raise _bad_gateway("invalid response from recommendation service")
    return body


def _upstream_detail(resp: httpx.Response) -> str | None:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return None
    return detail if isinstance(detail, str) else None

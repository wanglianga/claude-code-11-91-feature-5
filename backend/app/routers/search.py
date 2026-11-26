"""候选搜索：只读，不产生订单、不占用容量。"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..enums import ReservationMode
from ..models import Vehicle
from ..schemas import ConnectorCandidate, PoolCandidate, SearchRequest, SegmentInfo
from ..services.capacity import evaluate_site

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("")
def search(req: SearchRequest, db: Session = Depends(get_db)):
    vehicle = db.get(Vehicle, req.vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "车辆不存在")
    we = req.start_at + timedelta(minutes=req.duration_minutes)

    evaluation = evaluate_site(
        db, req.site_id, vehicle, req.start_at, we, req.min_power_kw
    )

    connector_candidates: list[ConnectorCandidate] = []
    for ev in evaluation.connector_evals:
        connector_candidates.append(
            ConnectorCandidate(
                connector_id=ev.connector.id,
                connector_code=ev.connector.code,
                eligible=ev.eligible,
                effective_power_kw=ev.min_power,
                locked_by_ref=ev.locked_refs[0] if ev.locked_refs else None,
                segments=[
                    SegmentInfo(
                        start_at=t0,
                        end_at=t1,
                        available_power_kw=p,
                    )
                    for (t0, t1, p) in ev.segment_powers
                ],
                reasons=ev.reasons,
            )
        )

    if req.mode == ReservationMode.CONNECTOR:
        return {
            "mode": ReservationMode.CONNECTOR.value,
            "eligible": any(ev.eligible for ev in evaluation.connector_evals),
            "connector_candidates": [c.model_dump() for c in connector_candidates],
        }

    pool = PoolCandidate(
        site_id=req.site_id,
        mode=ReservationMode.POOL,
        eligible=evaluation.confirmable_pool_slots >= 1,
        confirmable_slots=evaluation.confirmable_pool_slots,
        window_segments=[
            SegmentInfo(
                start_at=s.start_at,
                end_at=s.end_at,
                available_power_kw=float(evaluation.pool_slots_by_segment[i]),
                note=f"该切片可确认名额 {evaluation.pool_slots_by_segment[i]}；池在途预约 {s.pool_demand} 笔",
            )
            for i, s in enumerate(evaluation.segments)
        ],
        connector_candidates=connector_candidates,
    )
    return pool.model_dump()

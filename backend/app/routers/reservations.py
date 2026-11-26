from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import IdempotentConflict, idempotent_endpoint
from ..models import Reservation
from ..schemas import (
    ConfirmRequest,
    ReservationOut,
    TransitionRequest,
)
from ..services import reservation as service

router = APIRouter(prefix="/api/reservations", tags=["reservations"])


@router.get("", response_model=list[ReservationOut])
def list_reservations(db: Session = Depends(get_db)):
    return db.scalars(
        select(Reservation).order_by(Reservation.id.desc())
    ).all()


@router.get("/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    r = db.get(Reservation, reservation_id)
    from fastapi import HTTPException
    if r is None:
        raise HTTPException(404, "预约不存在")
    return r


@router.post("")
def confirm(req: ConfirmRequest, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work() -> dict:
        r = service.confirm_reservation(db, req)
        return ReservationOut.model_validate(r).model_dump(mode="json")

    try:
        return idempotent_endpoint(
            db, req.idempotency_key, request.url.path, payload, work
        )
    except IdempotentConflict as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code, content=exc.body)


def _transition_endpoint(action: str):
    def endpoint(reservation_id: int, req: TransitionRequest, request: Request,
                 db: Session = Depends(get_db)):
        fn = getattr(service, action)
        payload = {"reservation_id": reservation_id, **req.model_dump()}

        def work() -> dict:
            r = fn(db, reservation_id)
            return ReservationOut.model_validate(r).model_dump(mode="json")

        try:
            return idempotent_endpoint(
                db, req.idempotency_key, request.url.path, payload, work
            )
        except IdempotentConflict as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=exc.status_code, content=exc.body)

    return endpoint


router.post("/{reservation_id}/cancel", summary="取消（释放容量）")(
    _transition_endpoint("cancel")
)
router.post("/{reservation_id}/arrive", summary="到站（池模式此时分配枪口）")(
    _transition_endpoint("arrive")
)
router.post("/{reservation_id}/start", summary="开始充电（会话受保护）")(
    _transition_endpoint("start_charging")
)
router.post("/{reservation_id}/no-show", summary="标记迟到失约")(
    _transition_endpoint("mark_no_show")
)
router.post("/{reservation_id}/complete", summary="完成充电")(
    _transition_endpoint("complete")
)

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import IdempotentConflict, idempotent_endpoint
from ..schemas import (
    MaintenanceCreate,
    MaintenanceImpactOut,
    MaintenanceOut,
    MaintenanceRecover,
    ServiceActionCreate,
    ServiceActionOut,
)
from ..services import maintenance as service

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.post("/maintenance", response_model=MaintenanceImpactOut)
def create_maintenance(req: MaintenanceCreate, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work() -> dict:
        mw, impacts = service.create_maintenance(db, req)
        out = MaintenanceImpactOut(
            maintenance=MaintenanceOut.model_validate(mw),
            impacts=impacts,
        )
        return out.model_dump(mode="json")

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


@router.post("/maintenance/{window_id}/recover", response_model=MaintenanceOut)
def recover(window_id: int, req: MaintenanceRecover, request: Request,
            db: Session = Depends(get_db)):
    payload = {"window_id": window_id, **req.model_dump(mode="json")}

    def work() -> dict:
        mw = service.recover_maintenance(db, window_id, req)
        return MaintenanceOut.model_validate(mw).model_dump(mode="json")

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)


@router.post("/service-actions", response_model=ServiceActionOut)
def service_action(req: ServiceActionCreate, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work() -> dict:
        action = service.create_service_action(db, req)
        return ServiceActionOut.model_validate(action).model_dump(mode="json")

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)

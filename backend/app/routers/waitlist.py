"""候补路由：登记/撤销、邀请接受/拒绝/人工保留、站点策略与超时巡检。

所有写操作走统一幂等框架；邀请接受在服务层以站点级咨询锁串行化，
并发接受同一空档只有一笔能确认。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import IdempotentConflict, idempotent_endpoint
from ..enums import MatchPolicy
from ..models import Site, WaitlistInvite, WaitlistRequest
from ..schemas import (
    ManualHoldRequest,
    ReservationOut,
    SitePolicyOut,
    SitePolicyUpdate,
    SweepRequest,
    WaitlistCancel,
    WaitlistCreate,
    WaitlistInviteDecision,
    WaitlistInviteOut,
    WaitlistOut,
)
from ..services import waitlist as service

router = APIRouter(prefix="/api/waitlist", tags=["waitlist"])


def _run(req: Request, db: Session, key: str, payload: dict, work):
    try:
        return idempotent_endpoint(db, key, req.url.path, payload, work)
    except IdempotentConflict as exc:
        # jsonable_encoder 把 detail 内的 datetime 等对象转成 JSON 安全形式
        return JSONResponse(
            status_code=exc.status_code, content=jsonable_encoder(exc.body)
        )


@router.get("", response_model=list[WaitlistOut])
def list_waitlist(db: Session = Depends(get_db)):
    return db.scalars(
        select(WaitlistRequest).order_by(WaitlistRequest.id)
    ).all()


@router.get("/invites", response_model=list[WaitlistInviteOut])
def list_invites(db: Session = Depends(get_db)):
    return db.scalars(select(WaitlistInvite).order_by(WaitlistInvite.id)).all()


@router.get("/invites/{invite_id}", response_model=WaitlistInviteOut)
def get_invite(invite_id: int, db: Session = Depends(get_db)):
    invite = db.get(WaitlistInvite, invite_id)
    if invite is None:
        raise HTTPException(404, "邀请不存在")
    return invite


@router.post("", response_model=WaitlistOut)
def create(req: WaitlistCreate, request: Request, db: Session = Depends(get_db)):
    payload = req.model_dump(mode="json")

    def work() -> dict:
        entry = service.create_waitlist(db, req)
        return WaitlistOut.model_validate(entry).model_dump(mode="json")

    return _run(request, db, req.idempotency_key, payload, work)


@router.get("/{waitlist_id}", response_model=WaitlistOut)
def get_one(waitlist_id: int, db: Session = Depends(get_db)):
    entry = db.get(WaitlistRequest, waitlist_id)
    if entry is None:
        raise HTTPException(404, "候补请求不存在")
    return entry


@router.post("/{waitlist_id}/cancel", response_model=WaitlistOut)
def cancel(waitlist_id: int, req: WaitlistCancel, request: Request,
           db: Session = Depends(get_db)):
    payload = {"waitlist_id": waitlist_id, **req.model_dump()}

    def work() -> dict:
        entry = service.cancel_waitlist(db, waitlist_id)
        return WaitlistOut.model_validate(entry).model_dump(mode="json")

    return _run(request, db, req.idempotency_key, payload, work)


@router.post("/invites/{invite_id}/accept")
def accept(invite_id: int, req: WaitlistInviteDecision, request: Request,
           db: Session = Depends(get_db)):
    payload = {"invite_id": invite_id, **req.model_dump()}

    def work() -> dict:
        invite, reservation = service.accept_invite(db, invite_id)
        return {
            "invite": WaitlistInviteOut.model_validate(invite).model_dump(mode="json"),
            "reservation": ReservationOut.model_validate(reservation).model_dump(mode="json"),
            "note": "邀请已接受并生成确认预约；仅此一笔确认，容量已锁定",
        }

    return _run(request, db, req.idempotency_key, payload, work)


@router.post("/invites/{invite_id}/decline", response_model=WaitlistInviteOut)
def decline(invite_id: int, req: WaitlistInviteDecision, request: Request,
            db: Session = Depends(get_db)):
    payload = {"invite_id": invite_id, **req.model_dump()}

    def work() -> dict:
        invite = service.decline_invite(db, invite_id, req.reason)
        return WaitlistInviteOut.model_validate(invite).model_dump(mode="json")

    return _run(request, db, req.idempotency_key, payload, work)


@router.post("/invites/{invite_id}/hold", response_model=WaitlistInviteOut)
def manual_hold(invite_id: int, req: ManualHoldRequest, request: Request,
                db: Session = Depends(get_db)):
    payload = {"invite_id": invite_id, **req.model_dump()}

    def work() -> dict:
        invite = service.manual_hold_invite(db, invite_id, req.note)
        return WaitlistInviteOut.model_validate(invite).model_dump(mode="json")

    return _run(request, db, req.idempotency_key, payload, work)


@router.post("/sweep")
def sweep(req: SweepRequest, request: Request, db: Session = Depends(get_db)):
    """立即巡检：把已过截止时刻的邀请置 EXPIRED、过期候补关闭（按服务器当前时间）。"""
    payload = req.model_dump()

    def work() -> dict:
        if req.site_id is None:
            site_ids = db.scalars(select(Site.id)).all()
            merged = {"expired_invites": 0, "closed_requests": 0}
            for sid in site_ids:
                r = service.sweep_site(db, sid)
                merged["expired_invites"] += r["expired_invites"]
                merged["closed_requests"] += r["closed_requests"]
            return merged
        return service.sweep_site(db, req.site_id)

    return _run(request, db, req.idempotency_key, payload, work)


# 站点匹配策略（独立前缀，便于运营端调用）
policy_router = APIRouter(prefix="/api/sites", tags=["waitlist"])


@policy_router.post("/{site_id}/policy", response_model=SitePolicyOut)
def update_policy(site_id: int, req: SitePolicyUpdate, request: Request,
                  db: Session = Depends(get_db)):
    payload = {"site_id": site_id, **req.model_dump(mode="json")}

    def work() -> dict:
        site = service.set_site_policy(db, site_id, MatchPolicy(req.match_policy))
        return SitePolicyOut.model_validate(site).model_dump(mode="json")

    try:
        return idempotent_endpoint(db, req.idempotency_key, request.url.path, payload, work)
    except IdempotentConflict as exc:
        return JSONResponse(
            status_code=exc.status_code, content=jsonable_encoder(exc.body)
        )

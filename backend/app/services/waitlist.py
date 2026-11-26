"""候补用例：候补登记、取消释放后的空档匹配、一次性邀请与并发互斥。

两条可配置的分配规则（站点 match_policy）：
- FIFO：严格按候补登记时间（created_at, id）发邀请，公平、顺序可解释；
- COMPATIBILITY：兼容优先，先安排“只有少数枪口能满足”的严格请求，
  同等严格度再按登记时间排队，提高稀缺高功率枪口的利用率。

两条规则共同的硬约束：只能向「车辆接口/无障碍/功率能力匹配，且空档能完整
覆盖所需时长、开始时刻不晚于到站期限」的请求发邀请；邀请只是邀请，不是预约，
只有在截止前接受、并在站点级咨询锁内重新通过容量校验后才生成 CONFIRMED 预约。

并发安全：所有写流程先取站点级 pg_advisory_xact_lock（与确认流程同一把锁），
因此两名司机并发接受同一空档时必然串行：先到者把邀请置为 ACCEPTED 并落单，
后到者看到邀请已非 PENDING 或容量已被占用，只能拿到 409，不可能出现两笔确认。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import settings
from ..enums import (
    WAITLIST_OPEN_STATUSES,
    InviteStatus,
    MatchPolicy,
    ReservationMode,
    ReservationStatus,
    WaitlistStatus,
)
from ..models import (
    Connector,
    Reservation,
    Site,
    Vehicle,
    WaitlistEvent,
    WaitlistInvite,
    WaitlistRequest,
)
from .capacity import build_segments, evaluate_site
from .reservation import LOCK_NS_SITE, _add_event, _new_ref


def _now() -> datetime:
    """统一时钟：测试可 monkeypatch 本函数驱动超时，而不必真实等待。"""
    return datetime.now()


def _new_waitlist_ref() -> str:
    return f"W-{uuid4().hex[:8].upper()}"


def _add_wl_event(db: Session, request: WaitlistRequest,
                  event_type: str, detail: dict) -> None:
    event = WaitlistEvent(
        waitlist_request_id=request.id,
        event_type=event_type,
        detail=json.dumps(detail, ensure_ascii=False, default=str),
    )
    request.events.append(event)
    db.add(event)
    db.flush()


@dataclass
class Slot:
    """一次释放出来的、可用于发邀请的具体枪口空档。"""

    site_id: int
    connector: Connector
    start_at: datetime
    end_at: datetime
    min_power_kw: float

    @property
    def length_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds() // 60)


# ---------- 候补登记 / 撤销 ----------

def create_waitlist(db: Session, req) -> WaitlistRequest:
    vehicle = db.get(Vehicle, req.vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "车辆不存在")
    site = db.get(Site, req.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    if req.duration_minutes <= 0:
        raise HTTPException(422, "所需时长必须为正")
    if req.min_power_kw <= 0:
        raise HTTPException(422, "最低功率必须为正")
    if float(vehicle.max_power_kw) < req.min_power_kw:
        raise HTTPException(
            422,
            f"车辆最大受电 {float(vehicle.max_power_kw):g}kW 低于候补最低功率 "
            f"{req.min_power_kw:g}kW，该车辆无法履约此请求",
        )
    if req.arrive_by <= _now():
        raise HTTPException(422, "到站期限必须晚于当前时间")

    acceptable = list(dict.fromkeys(req.acceptable_site_ids or []))
    if site.id in acceptable:
        acceptable.remove(site.id)
    for sid in acceptable:
        if db.get(Site, sid) is None:
            raise HTTPException(404, f"可接受替代站点 {sid} 不存在")

    entry = WaitlistRequest(
        ref=_new_waitlist_ref(),
        vehicle_id=vehicle.id,
        site_id=site.id,
        status=WaitlistStatus.QUEUED,
        min_power_kw=req.min_power_kw,
        duration_minutes=req.duration_minutes,
        arrive_by=req.arrive_by,
        acceptable_site_ids=json.dumps(acceptable),
    )
    db.add(entry)
    db.flush()
    _add_wl_event(db, entry, "WAITLIST_JOINED", {
        "ref": entry.ref,
        "site_id": site.id,
        "vehicle_id": vehicle.id,
        "min_power_kw": req.min_power_kw,
        "duration_minutes": req.duration_minutes,
        "arrive_by": req.arrive_by,
        "acceptable_site_ids": acceptable,
        "queue_position_note": "按 created_at, id 确定候补顺位",
    })
    return entry


def cancel_waitlist(db: Session, waitlist_id: int) -> WaitlistRequest:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": _site_id_of(db, waitlist_id)},
    )
    entry = db.get(WaitlistRequest, waitlist_id)
    if entry is None:
        raise HTTPException(404, "候补请求不存在")
    if entry.status in (WaitlistStatus.CONFIRMED, WaitlistStatus.CANCELLED,
                        WaitlistStatus.CLOSED):
        raise HTTPException(
            409, f"候补 {entry.ref} 当前状态 {entry.status.value}，不能撤销"
        )
    # 若有待响应邀请，一并使失效并留痕
    for inv in entry.invites:
        if inv.status == InviteStatus.PENDING:
            inv.status = InviteStatus.SUPERSEDED
            inv.responded_at = _now()
            _add_wl_event(db, entry, "INVITE_SUPERSEDED", {
                "invite_id": inv.id,
                "reason": "司机撤销候补，待响应邀请自动失效",
            })
    entry.status = WaitlistStatus.CANCELLED
    _add_wl_event(db, entry, "WAITLIST_CANCELLED", {
        "note": "候补撤销，不再参与任何空档匹配",
    })
    db.flush()
    return entry


def _site_id_of(db: Session, waitlist_id: int) -> int:
    entry = db.get(WaitlistRequest, waitlist_id)
    if entry is None:
        raise HTTPException(404, "候补请求不存在")
    return entry.site_id


# ---------- 取消释放 → 构造空档 ----------

def build_slots_for_released(db: Session, reservation: Reservation,
                             now: datetime) -> tuple[list[Slot], int]:
    """把一笔取消/失约释放出的“未开始”容量翻译成具体枪口空档。

    只释放未开始时段：窗口起点取 max(now, start_at)，已开始部分不出现在空档里。
    返回 (空档列表, 本次最多可发邀请数)：固定枪口最多 1 支枪 1 个邀请；
    资源池释放 1 个池名额，因此同样最多发 1 个邀请。
    """
    ws = max(now, reservation.start_at)
    we = reservation.end_at
    if ws >= we:
        return [], 0

    if reservation.connector_id is not None:
        connector = db.get(Connector, reservation.connector_id)
        segments = build_segments(db, connector.site_id, ws, we)
        min_power = min((s.power.get(connector.id, 0.0) for s in segments), default=0.0)
        return [Slot(connector.site_id, connector, ws, we, min_power)], 1

    # 资源池预约：把当前真正空闲且合格的枪列为候选空档，但总量只算 1 个名额
    segments = build_segments(db, reservation.site_id, ws, we)
    slots: list[Slot] = []
    connectors = db.scalars(
        select(Connector).where(Connector.site_id == reservation.site_id)
    ).all()
    for c in connectors:
        if any(c.id in s.locked_by for s in segments):
            continue  # 已被其他固定预约锁定，不是本次释放的容量
        min_power = min((s.power.get(c.id, 0.0) for s in segments), default=0.0)
        slots.append(Slot(reservation.site_id, c, ws, we, min_power))
    return slots, 1


# ---------- 匹配 ----------

def _acceptable_sites(entry: WaitlistRequest) -> list[int]:
    try:
        alt = json.loads(entry.acceptable_site_ids or "[]")
    except json.JSONDecodeError:
        alt = []
    return [entry.site_id, *alt]


def _request_covers_slot(db: Session, entry: WaitlistRequest, slot: Slot) -> bool:
    """硬约束：能力匹配 + 完整时长覆盖 + 到站期限 + 替代站点范围。"""
    if slot.site_id not in _acceptable_sites(entry):
        return False
    vehicle = db.get(Vehicle, entry.vehicle_id)
    connector = slot.connector
    if connector.connector_type != vehicle.connector_type:
        return False
    if vehicle.accessible and not connector.accessible:
        return False
    if float(vehicle.max_power_kw) < float(entry.min_power_kw):
        return False
    if slot.min_power_kw < float(entry.min_power_kw):
        return False
    # 空档必须能完整覆盖候补所需时长
    if entry.duration_minutes > slot.length_minutes:
        return False
    # 空档开始时刻不得晚于司机的到站期限
    if slot.start_at > entry.arrive_by:
        return False
    return True


def _site_capability_count(db: Session, entry: WaitlistRequest,
                           site_id: int, slot: Slot) -> int:
    """该站点内“设备能力上”能满足此候补的枪口数（不看占用），用于兼容优先排序。

    数值越小说明请求越严格、越依赖稀缺设备，应优先安排。
    """
    vehicle = db.get(Vehicle, entry.vehicle_id)
    connectors = db.scalars(
        select(Connector).where(Connector.site_id == site_id)
    ).all()
    segments = build_segments(db, site_id, slot.start_at, slot.end_at)
    count = 0
    for c in connectors:
        if c.connector_type != vehicle.connector_type:
            continue
        if vehicle.accessible and not c.accessible:
            continue
        min_power = min((s.power.get(c.id, 0.0) for s in segments), default=0.0)
        if min_power >= float(entry.min_power_kw):
            count += 1
    return count


def _order_candidates(db: Session, entries: list[WaitlistRequest],
                      site: Site, slot: Slot) -> list[WaitlistRequest]:
    if site.match_policy == MatchPolicy.COMPATIBILITY:
        scored = [
            (_site_capability_count(db, e, slot.site_id, slot), e.created_at, e.id, e)
            for e in entries
        ]
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        return [x[3] for x in scored]
    # FIFO：严格登记先后
    return sorted(entries, key=lambda e: (e.created_at, e.id))


def sweep_site(db: Session, site_id: int, now: datetime | None = None) -> dict:
    """惰性巡检：超时邀请置 EXPIRED；超过到站期限仍未成交的候补关闭。"""
    now = now or _now()
    expired, closed = 0, 0
    pending = db.scalars(
        select(WaitlistInvite).where(
            WaitlistInvite.site_id == site_id,
            WaitlistInvite.status == InviteStatus.PENDING,
        )
    ).all()
    for inv in pending:
        if now > inv.expires_at:
            inv.status = InviteStatus.EXPIRED
            inv.responded_at = now
            entry = inv.waitlist_request
            entry.status = WaitlistStatus.EXPIRED
            _add_wl_event(db, entry, "INVITE_EXPIRED", {
                "invite_id": inv.id,
                "expires_at": inv.expires_at,
                "resolved_at": now,
                "note": "超过截止时刻未响应，邀请失效；候补仍按原登记顺位留在队列",
            })
            expired += 1

    open_entries = db.scalars(
        select(WaitlistRequest).where(
            WaitlistRequest.site_id == site_id,
            WaitlistRequest.status.in_(WAITLIST_OPEN_STATUSES),
        )
    ).all()
    for entry in open_entries:
        if entry.arrive_by < now:
            entry.status = WaitlistStatus.CLOSED
            _add_wl_event(db, entry, "WAITLIST_CLOSED", {
                "arrive_by": entry.arrive_by,
                "closed_at": now,
                "reason": "已超过到站期限仍未成交",
            })
            closed += 1
    db.flush()
    return {"expired_invites": expired, "closed_requests": closed}


def dispatch_for_slots(
    db: Session,
    site_id: int,
    slots: list[Slot],
    source_reservation_id: int | None,
    ttl_seconds: int,
    now: datetime | None = None,
    max_offers: int | None = None,
) -> list[WaitlistInvite]:
    """在站点锁内把空档逐个匹配给候补队列，生成带截止时刻的一次性邀请。

    max_offers 限制本次最多发出的邀请数（资源池释放 1 个名额时传 1），
    达到上限即停止，避免把同一个释放容量发成多份邀请。
    """
    now = now or _now()
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": site_id},
    )
    sweep_site(db, site_id, now)

    site = db.get(Site, site_id)
    entries = db.scalars(
        select(WaitlistRequest).where(WaitlistRequest.status.in_(WAITLIST_OPEN_STATUSES))
    ).all()
    # 只保留本站点或把本站点列为可接受替代站点的候补
    entries = [e for e in entries if site_id in _acceptable_sites(e)]

    invites: list[WaitlistInvite] = []
    offered_request_ids: set[int] = set()
    for slot in slots:
        if max_offers is not None and len(invites) >= max_offers:
            break
        candidates = [
            e for e in entries
            if e.id not in offered_request_ids
            and e.status in WAITLIST_OPEN_STATUSES
            and _request_covers_slot(db, e, slot)
        ]
        if not candidates:
            continue
        chosen = _order_candidates(db, candidates, site, slot)[0]
        invite = WaitlistInvite(
            waitlist_request_id=chosen.id,
            site_id=site_id,
            status=InviteStatus.PENDING,
            connector_id=slot.connector.id,
            slot_start=slot.start_at,
            slot_end=min(slot.end_at,
                         slot.start_at + timedelta(minutes=chosen.duration_minutes)),
            offered_power_kw=slot.min_power_kw,
            expires_at=now + timedelta(seconds=ttl_seconds),
            source_reservation_id=source_reservation_id,
        )
        db.add(invite)
        db.flush()
        chosen.status = WaitlistStatus.INVITED
        offered_request_ids.add(chosen.id)
        _add_wl_event(db, chosen, "INVITE_ISSUED", {
            "invite_id": invite.id,
            "site_id": site_id,
            "connector_id": slot.connector.id,
            "connector_code": slot.connector.code,
            "slot": [slot.start_at, slot.start_at + timedelta(minutes=chosen.duration_minutes)],
            "offered_power_kw": slot.min_power_kw,
            "expires_at": invite.expires_at,
            "policy": site.match_policy.value,
            "source_reservation_id": source_reservation_id,
            "note": "这是一次性邀请，不是已确认预约；须在截止时刻前接受",
        })
        invites.append(invite)
    db.flush()
    return invites


def dispatch_for_cancellation(db: Session, reservation: Reservation,
                              ttl_seconds: int | None = None) -> list[WaitlistInvite]:
    """预约取消/失约后调用：只把未开始时段翻译成空档并触发一轮匹配。"""
    ttl = ttl_seconds or settings.waitlist_invite_ttl_seconds
    # 先把取消/失约状态落库（仍在同一事务内），确保容量切片看到的是释放后的状态
    db.flush()
    now = _now()
    slots, max_offers = build_slots_for_released(db, reservation, now)
    if not slots:
        return []
    return dispatch_for_slots(
        db, slots[0].site_id, slots, reservation.id, ttl, now,
        max_offers=max_offers,
    )


# ---------- 邀请响应 ----------

def _load_pending_invite(db: Session, invite_id: int) -> WaitlistInvite:
    invite = db.get(WaitlistInvite, invite_id)
    if invite is None:
        raise HTTPException(404, "邀请不存在")
    return invite


def accept_invite(db: Session, invite_id: int) -> tuple[WaitlistInvite, Reservation]:
    invite = _load_pending_invite(db, invite_id)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": invite.site_id},
    )
    db.refresh(invite)
    entry = db.get(WaitlistRequest, invite.waitlist_request_id)
    now = _now()

    if invite.status != InviteStatus.PENDING:
        raise HTTPException(
            409, f"邀请 #{invite.id} 已处于 {invite.status.value}，不能重复接受"
        )
    if now > invite.expires_at:        # 迟到的接受：先把超时落账，再明确拒绝，绝不能写成成功确认
        invite.status = InviteStatus.EXPIRED
        invite.responded_at = now
        entry.status = WaitlistStatus.EXPIRED
        _add_wl_event(db, entry, "INVITE_EXPIRED", {
            "invite_id": invite.id,
            "expires_at": invite.expires_at,
            "resolved_at": now,
            "late_accept": True,
            "note": "接受请求越过截止时刻到达，按超时处理，未生成预约",
        })
        db.flush()
        raise HTTPException(
            409,
            {"message": "邀请已超过截止时刻，迟到的接受不能确认",
             "invite_id": invite.id, "expires_at": invite.expires_at},
        )

    # 截止前接受：在当前真实容量下重新校验该枪该时段仍合格且未被占用
    vehicle = db.get(Vehicle, entry.vehicle_id)
    evaluation = evaluate_site(
        db, invite.site_id, vehicle,
        invite.slot_start, invite.slot_end, float(entry.min_power_kw),
    )
    target = next(
        (ev for ev in evaluation.connector_evals if ev.connector.id == invite.connector_id),
        None,
    )
    capacity_ok = target is not None and target.eligible
    if entry.arrive_by < invite.slot_start:
        capacity_ok = False

    if not capacity_ok:
        invite.status = InviteStatus.SUPERSEDED
        invite.responded_at = now
        entry.status = WaitlistStatus.QUEUED
        reasons = target.reasons if target is not None else ["目标枪口不存在"]
        _add_wl_event(db, entry, "INVITE_SUPERSEDED", {
            "invite_id": invite.id,
            "reason": "接受时空档已被其他确认占用或设备状态变化",
            "connector_reasons": reasons,
        })
        db.flush()
        raise HTTPException(
            409,
            {"message": "接受时该空档已不可用，邀请失效，候补回到队列",
             "invite_id": invite.id, "connector_reasons": reasons},
        )

    # 通过校验：生成真正的 CONFIRMED 预约（固定枪口），邀请一次性消耗
    reservation = Reservation(
        ref=_new_ref(),
        vehicle_id=vehicle.id,
        site_id=invite.site_id,
        mode=ReservationMode.CONNECTOR,
        status=ReservationStatus.CONFIRMED,
        start_at=invite.slot_start,
        end_at=invite.slot_end,
        min_power_kw=entry.min_power_kw,
        connector_id=invite.connector_id,
        confirmation_snapshot=json.dumps({
            "mode": ReservationMode.CONNECTOR.value,
            "connector_id": invite.connector_id,
            "connector_code": target.connector.code,
            "connector_type": target.connector.connector_type,
            "min_power_kw": float(entry.min_power_kw),
            "effective_power_kw": target.min_power,
            "accessible": target.connector.accessible,
            "source": "WAITLIST_INVITE",
            "invite_id": invite.id,
            "waitlist_ref": entry.ref,
            "promise": f"候补成交：锁定枪口 {target.connector.code}，"
                       f"{invite.slot_start:%H:%M}-{invite.slot_end:%H:%M}",
        }, ensure_ascii=False, default=str),
    )
    db.add(reservation)
    db.flush()
    _add_event(db, reservation, "CONFIRMED", {
        "source": "WAITLIST_INVITE",
        "invite_id": invite.id,
        "waitlist_ref": entry.ref,
    })

    invite.status = InviteStatus.ACCEPTED
    invite.responded_at = now
    entry.status = WaitlistStatus.CONFIRMED
    entry.reservation_id = reservation.id
    _add_wl_event(db, entry, "INVITE_ACCEPTED", {
        "invite_id": invite.id,
        "reservation_id": reservation.id,
        "reservation_ref": reservation.ref,
        "connector_id": invite.connector_id,
        "resolved_at": now,
        "note": "一次性邀请已接受并确认，容量在同一事务内锁定",
    })
    db.flush()
    return invite, reservation


def decline_invite(db: Session, invite_id: int, reason: str = "") -> WaitlistInvite:
    invite = _load_pending_invite(db, invite_id)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": invite.site_id},
    )
    db.refresh(invite)
    entry = db.get(WaitlistRequest, invite.waitlist_request_id)
    now = _now()
    if invite.status != InviteStatus.PENDING:
        raise HTTPException(
            409, f"邀请 #{invite.id} 已处于 {invite.status.value}，不能拒绝"
        )
    if now > invite.expires_at:
        invite.status = InviteStatus.EXPIRED
        invite.responded_at = now
        entry.status = WaitlistStatus.EXPIRED
        _add_wl_event(db, entry, "INVITE_EXPIRED", {
            "invite_id": invite.id, "expires_at": invite.expires_at,
            "resolved_at": now, "late_decline": True,
        })
        db.flush()
        raise HTTPException(409, "邀请已超过截止时刻，按超时处理而非拒绝")

    invite.status = InviteStatus.DECLINED
    invite.responded_at = now
    entry.status = WaitlistStatus.DECLINED
    _add_wl_event(db, entry, "INVITE_DECLINED", {
        "invite_id": invite.id,
        "reason": reason or "司机主动拒绝",
        "resolved_at": now,
        "note": "拒绝已留痕；候补保持原登记顺位，继续参与后续空档匹配",
    })
    db.flush()
    return invite


def manual_hold_invite(db: Session, invite_id: int, note: str = "") -> WaitlistInvite:
    """运营人工保留：让当前待响应邀请失效，候补回到开放队列，全过程留痕。"""
    invite = _load_pending_invite(db, invite_id)
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": invite.site_id},
    )
    db.refresh(invite)
    entry = db.get(WaitlistRequest, invite.waitlist_request_id)
    if invite.status != InviteStatus.PENDING:
        raise HTTPException(
            409, f"邀请 #{invite.id} 已处于 {invite.status.value}，无需人工保留"
        )
    now = _now()
    invite.status = InviteStatus.SUPERSEDED
    invite.responded_at = now
    entry.status = WaitlistStatus.QUEUED
    _add_wl_event(db, entry, "INVITE_MANUAL_HOLD", {
        "invite_id": invite.id,
        "note": note or "运营人工保留，邀请失效",
        "resolved_at": now,
    })
    db.flush()
    return invite


def set_site_policy(db: Session, site_id: int, policy: MatchPolicy) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
        {"ns": LOCK_NS_SITE, "sid": site_id},
    )
    previous = site.match_policy
    site.match_policy = policy
    db.flush()
    return site

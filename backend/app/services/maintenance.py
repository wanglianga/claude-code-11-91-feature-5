"""运营用例：降额/停用/恢复窗口及其对既有预约的影响分析。

影响判定原则：
- 只处理与维护时段存在真实时间交集的预约（半开区间 [start, end)），
  18:10 结束的预约与 18:10 开始的降额无交集，不会被撤销；
- 交集之外已经履约完成的区间不受影响，判定只针对仍待履约的切片；
- 充电中的会话受保护，预约管理不能直接释放；
- 恢复设备只结束维护窗口并留痕，不改写历史确认快照，也不自动回滚订单状态。
"""

import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..enums import (
    HOLDING_STATUSES,
    ImpactDecision,
    MaintenanceKind,
    ReservationStatus,
    ServiceActionKind,
)
from ..models import (
    Connector,
    MaintenanceWindow,
    Reservation,
    ServiceAction,
    Vehicle,
)
from ..schemas import MaintenanceCreate, MaintenanceRecover, ServiceActionCreate
from .capacity import _overlap, _window_end, build_segments
from .reservation import _add_event

LOCK_NS_CONNECTOR = 91003
LOCK_NS_SITE = 91001


def connector_power_at(connector: Connector, maintenances: list[MaintenanceWindow],
                       t0: datetime, t1: datetime) -> float:
    """计算枪口在 [t0,t1) 切片内的有效功率（不考虑预约占用）。"""
    power = float(connector.rated_power_kw)
    for m in maintenances:
        if m.connector_id != connector.id:
            continue
        m_end = _window_end(m)
        if m.start_at <= t0 < m_end and _overlap(m.start_at, m_end, t0, t1):
            if m.kind == MaintenanceKind.OUTAGE:
                return 0.0
            power = min(power, float(m.effective_power_kw))
    return power


def _connector_satisfies(connector: Connector, vehicle: Vehicle,
                         maintenances: list[MaintenanceWindow],
                         ws: datetime, we: datetime, min_power_kw: float) -> bool:
    if connector.connector_type != vehicle.connector_type:
        return False
    if vehicle.accessible and not connector.accessible:
        return False
    # 以维护窗口边界切片，逐段验证功率
    bounds = {ws, we}
    for m in maintenances:
        if m.connector_id != connector.id:
            continue
        m_end = _window_end(m)
        if m.start_at < we and m_end > ws:
            bounds.add(max(m.start_at, ws))
            bounds.add(min(m_end, we))
    bounds = sorted(bounds)
    for t0, t1 in zip(bounds, bounds[1:]):
        if connector_power_at(connector, maintenances, t0, t1) < min_power_kw:
            return False
    return True


def create_maintenance(db: Session, req: MaintenanceCreate) -> tuple[MaintenanceWindow, list[dict]]:
    connector = db.get(Connector, req.connector_id)
    if connector is None:
        raise HTTPException(404, "枪口不存在")
    if req.start_at >= req.planned_end_at:
        raise HTTPException(422, "维护开始时间必须早于计划结束时间")
    if req.kind == MaintenanceKind.DERATE:
        if req.effective_power_kw is None:
            raise HTTPException(422, "降额窗口必须提供 effective_power_kw")
        if req.effective_power_kw <= 0:
            raise HTTPException(422, "降额功率必须为正；完全停用请使用 OUTAGE")
    if req.kind == MaintenanceKind.OUTAGE and req.effective_power_kw:
        raise HTTPException(422, "停用窗口不应指定降额功率")

    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :cid)"),
               {"ns": LOCK_NS_CONNECTOR, "cid": connector.id})

    active = db.scalars(
        select(MaintenanceWindow).where(
            MaintenanceWindow.connector_id == connector.id,
            MaintenanceWindow.ended_at.is_(None),
        )
    ).all()
    for m in active:
        if _overlap(m.start_at, m.planned_end_at, req.start_at, req.planned_end_at):
            raise HTTPException(409, f"枪口 {connector.code} 已有重叠的生效维护窗口 #{m.id}")

    mw = MaintenanceWindow(
        connector_id=connector.id,
        kind=req.kind,
        start_at=req.start_at,
        planned_end_at=req.planned_end_at,
        effective_power_kw=req.effective_power_kw,
        reason=req.reason,
    )
    db.add(mw)
    db.flush()
    impacts = _assess_impacts(db, mw)
    return mw, impacts


def _assess_impacts(db: Session, mw: MaintenanceWindow) -> list[dict]:
    """新维护窗口落库后，重算站点内在途预约，返回影响清单并改写可改写的状态。"""
    connector = db.get(Connector, mw.connector_id)
    site_id = connector.site_id
    m_start, m_end = mw.start_at, _window_end(mw)

    reservations = db.scalars(
        select(Reservation).where(
            Reservation.site_id == site_id,
            Reservation.status.in_(HOLDING_STATUSES),
        )
    ).all()
    all_maintenance = db.scalars(select(MaintenanceWindow)).all()
    connectors = {c.id: c for c in db.scalars(
        select(Connector).where(Connector.site_id == site_id)).all()}

    impacts: list[dict] = []
    for r in reservations:
        if not _overlap(r.start_at, r.end_at, m_start, m_end):
            continue  # 无真实交集：例如 18:10 已结束的预约不受 18:10 降额影响
        overlap_start = max(r.start_at, m_start)
        overlap_end = min(r.end_at, m_end)
        vehicle = db.get(Vehicle, r.vehicle_id)
        reason_parts = [
            f"维护交集 {overlap_start:%H:%M}-{overlap_end:%H:%M}"
            + (f"（预约 {r.start_at:%H:%M}-{overlap_start:%H:%M} 区间已在维护前，不受影响）"
               if r.start_at < overlap_start else "")
        ]

        if r.status == ReservationStatus.CHARGING:
            decision = ImpactDecision.PROTECTED_CHARGING
            reason_parts.append("充电会话进行中，预约管理无权释放，保持履约并转现场处理")
        elif r.connector_id is not None:
            target = connectors[r.connector_id]
            ok = _connector_satisfies(target, vehicle, all_maintenance,
                                      r.start_at, r.end_at, float(r.min_power_kw))
            if ok:
                decision = ImpactDecision.UNCHANGED
                reason_parts.append(f"枪口 {target.code} 维护后仍满足车辆全部需求，继续履约")
            else:
                reason_parts.append(f"枪口 {target.code} 在待履约区间不再满足 "
                                    f"{float(r.min_power_kw):g}kW/接口/无障碍要求")
                if r.status == ReservationStatus.ARRIVED:
                    decision = ImpactDecision.MANUAL_CONTACT
                    r.status = ReservationStatus.MANUAL_CONTACT
                    reason_parts.append("车辆已到站，进入人工联系")
                else:
                    decision = ImpactDecision.NEEDS_RESELECTION
                    r.status = ReservationStatus.NEEDS_RESELECTION
                    reason_parts.append("释放原容量，进入改选流程")
        else:
            # 资源池在途预约：重算池内可同时履约名额（扣除本单自身的池需求）
            segments = build_segments(db, site_id, r.start_at, r.end_at)
            short_segments = []
            for seg in segments:
                free_eligible = 0
                self_demand = 1 if _overlap(r.start_at, r.end_at, seg.start_at, seg.end_at) else 0
                for cid, c in connectors.items():
                    if cid in seg.locked_by:
                        continue
                    p = seg.power.get(cid, 0.0)
                    if (c.connector_type == vehicle.connector_type
                            and (not vehicle.accessible or c.accessible)
                            and p >= float(r.min_power_kw)):
                        free_eligible += 1
                if free_eligible - (seg.pool_demand - self_demand) <= 0:
                    short_segments.append(seg)
            if not short_segments:
                decision = ImpactDecision.UNCHANGED
                reason_parts.append("资源池在待履约区间仍有名额，继续履约")
            else:
                reason_parts.append(
                    "资源池在 "
                    + ", ".join(f"{s.start_at:%H:%M}-{s.end_at:%H:%M}" for s in short_segments)
                    + " 已无可同时履约名额"
                )
                if r.status == ReservationStatus.ARRIVED:
                    decision = ImpactDecision.MANUAL_CONTACT
                    r.status = ReservationStatus.MANUAL_CONTACT
                else:
                    decision = ImpactDecision.NEEDS_RESELECTION
                    r.status = ReservationStatus.NEEDS_RESELECTION

        impacts.append({
            "reservation_id": r.id,
            "ref": r.ref,
            "status": r.status.value,
            "mode": r.mode.value,
            "connector_id": r.connector_id,
            "decision": decision.value,
            "reason": "；".join(reason_parts),
            "overlap_start": overlap_start,
            "overlap_end": overlap_end,
        })
        _add_event(db, r, "MAINTENANCE_IMPACT", {
            "window_id": mw.id,
            "decision": decision.value,
            "overlap": [overlap_start, overlap_end],
            "reason": reason_parts,
        })
    db.flush()
    return impacts


def recover_maintenance(db: Session, window_id: int, req: MaintenanceRecover) -> MaintenanceWindow:
    mw = db.get(MaintenanceWindow, window_id)
    if mw is None:
        raise HTTPException(404, "维护窗口不存在")
    if mw.ended_at is not None:
        raise HTTPException(409, f"窗口 #{mw.id} 已恢复，不能重复恢复")
    if not (mw.start_at <= req.ended_at <= mw.planned_end_at):
        raise HTTPException(422, "恢复时刻必须落在维护窗口的开始与计划结束之间")

    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :cid)"),
               {"ns": LOCK_NS_CONNECTOR, "cid": mw.connector_id})
    mw.ended_at = req.ended_at
    db.flush()

    # 容量已恢复：通知此前受影响的订单，但绝不自动回滚状态、不改写确认快照
    site_id = db.get(Connector, mw.connector_id).site_id
    affected = db.scalars(
        select(Reservation).where(
            Reservation.site_id == site_id,
            Reservation.status.in_(
                [ReservationStatus.NEEDS_RESELECTION, ReservationStatus.MANUAL_CONTACT]
            ),
        )
    ).all()
    for r in affected:
        _add_event(db, r, "DEVICE_RECOVERED", {
            "window_id": mw.id,
            "note": "设备已恢复，新预约可使用该容量；原确认快照保持不变，本单状态不自动回滚",
        })
    return mw


# ---------- 客服动作 ----------

def _connector_state_snapshot(db: Session, connector: Connector | None) -> str:
    if connector is None:
        return json.dumps({"connector": None}, ensure_ascii=False)
    active_windows = [
        {
            "window_id": m.id,
            "kind": m.kind.value,
            "effective_power_kw": float(m.effective_power_kw) if m.effective_power_kw else None,
            "start_at": m.start_at,
            "planned_end_at": m.planned_end_at,
            "reason": m.reason,
        }
        for m in connector.maintenance_windows
        if m.ended_at is None
    ]
    return json.dumps({
        "connector_id": connector.id,
        "code": connector.code,
        "rated_power_kw": float(connector.rated_power_kw),
        "accessible": connector.accessible,
        "active_maintenance": active_windows,
        "captured_at": datetime.now(),
    }, ensure_ascii=False, default=str)


def create_service_action(db: Session, req: ServiceActionCreate) -> ServiceAction:
    reservation = db.get(Reservation, req.reservation_id)
    if reservation is None:
        raise HTTPException(404, "预约不存在")

    if req.kind == ServiceActionKind.FEE_ADJUSTMENT and req.amount is None:
        raise HTTPException(422, "费用调整必须提供 amount（负数为补偿、正数为追收）")
    if req.kind == ServiceActionKind.ALTERNATIVE_CONNECTOR and req.target_connector_id is None:
        raise HTTPException(422, "替代枪口必须指定 target_connector_id")

    target = (
        db.get(Connector, req.target_connector_id)
        if req.target_connector_id is not None else None
    )
    if req.kind == ServiceActionKind.ALTERNATIVE_CONNECTOR:
        if target is None:
            raise HTTPException(404, "替代枪口不存在")
        if target.site_id != reservation.site_id:
            raise HTTPException(422, "替代枪口必须与预约站点一致")
        if reservation.status == ReservationStatus.CHARGING:
            raise HTTPException(409, "充电中的会话不能由预约管理迁移，请走现场处置")

        db.execute(text("SELECT pg_advisory_xact_lock(:ns, :sid)"),
                   {"ns": LOCK_NS_SITE, "sid": reservation.site_id})
        db.refresh(reservation)
        vehicle = db.get(Vehicle, reservation.vehicle_id)
        all_maintenance = db.scalars(select(MaintenanceWindow)).all()
        if not _connector_satisfies(target, vehicle, all_maintenance,
                                    reservation.start_at, reservation.end_at,
                                    float(reservation.min_power_kw)):
            raise HTTPException(409, "替代枪口在当前设备状态下不满足该预约的接口/功率/无障碍要求")
        # 占用检查：目标枪不能被其他在途预约锁定
        blockers = db.scalars(
            select(Reservation).where(
                Reservation.connector_id == target.id,
                Reservation.status.in_(HOLDING_STATUSES),
                Reservation.id != reservation.id,
            )
        ).all()
        for b in blockers:
            if _overlap(b.start_at, b.end_at, reservation.start_at, reservation.end_at):
                raise HTTPException(409, f"替代枪口 {target.code} 时段已被 {b.ref} 占用")

        previous_connector_id = reservation.connector_id
        previous_state = _connector_state_snapshot(
            db,
            db.get(Connector, previous_connector_id) if previous_connector_id else None,
        )
        reservation.connector_id = target.id
        if reservation.status in (
            ReservationStatus.NEEDS_RESELECTION,
            ReservationStatus.MANUAL_CONTACT,
        ):
            reservation.status = ReservationStatus.CONFIRMED
        target_state = _connector_state_snapshot(db, target)
        snapshot_state = json.dumps({
            "from_connector_state": json.loads(previous_state),
            "to_connector_state": json.loads(target_state),
        }, ensure_ascii=False)
        _add_event(db, reservation, "MIGRATED_CONNECTOR", {
            "from_connector_id": previous_connector_id,
            "to_connector_id": target.id,
            "order_status": reservation.status.value,
            "note": req.note,
        })
    else:
        snapshot_state = _connector_state_snapshot(
            db, db.get(Connector, reservation.connector_id) if reservation.connector_id else None
        )

    action = ServiceAction(
        reservation_id=reservation.id,
        kind=req.kind,
        order_status_at_action=reservation.status,
        connector_state_snapshot=snapshot_state,
        target_connector_id=req.target_connector_id,
        amount=req.amount,
        note=req.note,
    )
    db.add(action)
    db.flush()
    _add_event(db, reservation, f"SERVICE_{req.kind.value}", {
        "action_id": action.id,
        "amount": req.amount,
        "target_connector_id": req.target_connector_id,
        "note": req.note,
    })
    return action

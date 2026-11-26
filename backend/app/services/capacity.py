"""容量引擎：在给定时间窗内逐切片计算每支枪口的有效功率与占用情况。

设计要点：
- 维护窗口（降额/停用）与既有预约的起止时刻共同构成时间切片边界，
  每个切片内功率与占用状态恒定，因此结论可按“真实时间交集”核验。
- 固定枪口模式与资源池模式共用同一份切片数据：
  * 固定枪口：候选枪必须在所有切片合格且未被其他锁定预约占用；
  * 资源池：每个切片的可确认名额 = 合格且未锁定的枪数 − 池模式在途预约数，
    全程取最小值，保证同一份容量不会因两支枪“各自空闲”而重复卖出。
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import HOLDING_STATUSES, MaintenanceKind, ReservationMode
from ..models import Connector, MaintenanceWindow, Reservation, Vehicle


@dataclass
class Segment:
    start_at: datetime
    end_at: datetime
    # connector_id -> 该切片内有效功率（停用为 0）
    power: dict[int, float] = field(default_factory=dict)
    # connector_id -> 占用该枪的预约 ref（仅固定/已分配枪口的持有预约）
    locked_by: dict[int, str] = field(default_factory=dict)
    # 该切片内占用池名额但尚未分配枪口的池模式预约数
    pool_demand: int = 0


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _window_end(m: MaintenanceWindow) -> datetime:
    return m.ended_at or m.planned_end_at


def build_segments(db: Session, site_id: int, ws: datetime, we: datetime) -> list[Segment]:
    """构造 [ws, we) 内功率/占用状态恒定的时间切片。"""
    connectors = db.scalars(
        select(Connector).where(Connector.site_id == site_id)
    ).all()
    connector_ids = [c.id for c in connectors]

    maintenances = db.scalars(
        select(MaintenanceWindow).where(
            MaintenanceWindow.connector_id.in_(connector_ids)
        )
    ).all() if connector_ids else []

    holding = db.scalars(
        select(Reservation).where(
            Reservation.site_id == site_id,
            Reservation.status.in_(HOLDING_STATUSES),
            Reservation.start_at < we,
            Reservation.end_at > ws,
        )
    ).all()

    boundaries = {ws, we}
    for m in maintenances:
        m_end = _window_end(m)
        if m.start_at < we and m_end > ws:
            boundaries.add(max(m.start_at, ws))
            boundaries.add(min(m_end, we))
    for r in holding:
        boundaries.add(max(r.start_at, ws))
        boundaries.add(min(r.end_at, we))
    bounds = sorted(b for b in boundaries if ws <= b <= we)
    # 去重保序
    bounds = sorted(set(bounds))

    segments: list[Segment] = []
    for t0, t1 in zip(bounds, bounds[1:]):
        if t0 == t1:
            continue
        seg = Segment(start_at=t0, end_at=t1)
        for c in connectors:
            power = float(c.rated_power_kw)
            for m in maintenances:
                if m.connector_id != c.id:
                    continue
                m_end = _window_end(m)
                if m.start_at <= t0 < m_end and _overlap(m.start_at, m_end, t0, t1):
                    if m.kind == MaintenanceKind.OUTAGE:
                        power = 0.0
                    else:
                        power = min(power, float(m.effective_power_kw))
            seg.power[c.id] = power
        for r in holding:
            if r.connector_id is not None and _overlap(r.start_at, r.end_at, t0, t1):
                # 同一枪理论上不会有重叠的两笔持有预约（确认事务保证）
                seg.locked_by[r.connector_id] = r.ref
            elif (
                r.mode == ReservationMode.POOL
                and r.connector_id is None
                and _overlap(r.start_at, r.end_at, t0, t1)
            ):
                seg.pool_demand += 1
        segments.append(seg)
    return segments


@dataclass
class ConnectorEval:
    connector: Connector
    eligible: bool
    min_power: float
    reasons: list[str]
    locked_refs: list[str]
    segment_powers: list[tuple[datetime, datetime, float]]


@dataclass
class SiteEval:
    segments: list[Segment]
    connector_evals: list[ConnectorEval]
    # 每个切片的池可确认名额
    pool_slots_by_segment: list[int]
    confirmable_pool_slots: int


def evaluate_site(
    db: Session,
    site_id: int,
    vehicle: Vehicle,
    ws: datetime,
    we: datetime,
    min_power_kw: float,
) -> SiteEval:
    connectors = db.scalars(
        select(Connector).where(Connector.site_id == site_id)
    ).all()
    segments = build_segments(db, site_id, ws, we)

    evals: list[ConnectorEval] = []
    for c in connectors:
        reasons: list[str] = []
        locked: list[str] = []
        powers: list[tuple[datetime, datetime, float]] = []

        if c.connector_type != vehicle.connector_type:
            reasons.append(
                f"接口不兼容：枪口 {c.code} 为 {c.connector_type}，车辆需要 {vehicle.connector_type}"
            )
        if vehicle.accessible and not c.accessible:
            reasons.append(f"枪口 {c.code} 不满足无障碍接入要求")
        if float(vehicle.max_power_kw) < min_power_kw:
            reasons.append(
                f"车辆最大受电功率 {float(vehicle.max_power_kw):g}kW 低于请求的 {min_power_kw:g}kW"
            )

        for seg in segments:
            p = seg.power.get(c.id, 0.0)
            powers.append((seg.start_at, seg.end_at, p))
            if p <= 0:
                reasons.append(
                    f"{seg.start_at:%H:%M}-{seg.end_at:%H:%M} 枪口 {c.code} 处于停用维护"
                )
            elif p < min_power_kw:
                reasons.append(
                    f"{seg.start_at:%H:%M}-{seg.end_at:%H:%M} 枪口 {c.code} 有效功率 "
                    f"{p:g}kW 低于要求 {min_power_kw:g}kW（降额或额定不足）"
                )
            ref = seg.locked_by.get(c.id)
            if ref:
                locked.append(ref)
                reasons.append(
                    f"{seg.start_at:%H:%M}-{seg.end_at:%H:%M} 枪口 {c.code} "
                    f"已被有效预约 {ref} 占用"
                )

        min_power = min((p for _, _, p in powers), default=0.0)
        eligible = not reasons
        evals.append(
            ConnectorEval(
                connector=c,
                eligible=eligible,
                min_power=min_power,
                reasons=reasons,
                locked_refs=sorted(set(locked)),
                segment_powers=powers,
            )
        )

    # 资源池名额：逐切片统计合格且未被锁定的枪，再扣除在途池预约
    pool_slots: list[int] = []
    for idx, seg in enumerate(segments):
        free_eligible = 0
        for ev in evals:
            cid = ev.connector.id
            plug_ok = ev.connector.connector_type == vehicle.connector_type
            access_ok = (not vehicle.accessible) or ev.connector.accessible
            power_ok = ev.segment_powers[idx][2] >= min_power_kw
            if plug_ok and access_ok and power_ok and cid not in seg.locked_by:
                free_eligible += 1
        pool_slots.append(max(0, free_eligible - seg.pool_demand))

    return SiteEval(
        segments=segments,
        connector_evals=evals,
        pool_slots_by_segment=pool_slots,
        confirmable_pool_slots=min(pool_slots, default=0),
    )


def find_connector_eval(evals: list[ConnectorEval], connector_id: int) -> ConnectorEval | None:
    for ev in evals:
        if ev.connector.id == connector_id:
            return ev
    return None

"""本地样例数据：
站点 1（东站）A/B/C/D 四枪 + 四辆车 + A 枪 18:00-18:30 有效预约（保留原演示）；
站点 2（西站·候补演示站）E=120kW / F=60kW 两支枪，18:00-18:30 均被预约占用，
并预置两条候补：Q2 先登记（仅需 60kW，两枪皆可）、Q1 后登记（需 ≥90kW，只能用 E）。

运行：python -m app.seed
幂等策略：直接 drop/create 全部表后重灌，仅用于本地演示与重置。
"""

import json
from datetime import datetime

from .database import engine, SessionLocal
from .enums import MatchPolicy, ReservationMode, ReservationStatus, WaitlistStatus
from .models import (
    Base,
    Connector,
    Reservation,
    ReservationEvent,
    Site,
    Vehicle,
    WaitlistEvent,
    WaitlistRequest,
)

# 固定在“周五”这一天（2026-11-27 为周五），演示统一使用朴素本地时间
FRIDAY = "2026-11-27"


def _dt(hm: str) -> datetime:
    return datetime.fromisoformat(f"{FRIDAY}T{hm}:00")


def _confirmed_reservation(db, ref, vehicle_id, site_id, connector, start, end,
                           min_power, snapshot_extra=None):
    snapshot = {
        "mode": ReservationMode.CONNECTOR.value,
        "connector_id": connector.id,
        "connector_code": connector.code,
        "connector_type": connector.connector_type,
        "min_power_kw": min_power,
        "effective_power_kw": float(connector.rated_power_kw),
        "accessible": connector.accessible,
        "promise": f"锁定枪口 {connector.code}，全程有效功率不低于 {connector.rated_power_kw:g}kW",
    }
    if snapshot_extra:
        snapshot.update(snapshot_extra)
    reservation = Reservation(
        ref=ref,
        vehicle_id=vehicle_id,
        site_id=site_id,
        mode=ReservationMode.CONNECTOR,
        status=ReservationStatus.CONFIRMED,
        start_at=start,
        end_at=end,
        min_power_kw=min_power,
        connector_id=connector.id,
        confirmation_snapshot=json.dumps(snapshot, ensure_ascii=False),
    )
    db.add(reservation)
    db.flush()
    db.add(ReservationEvent(
        reservation_id=reservation.id,
        event_type="CONFIRMED",
        detail=json.dumps({"snapshot": snapshot}, ensure_ascii=False),
    ))
    return reservation


def seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        # ---------- 车辆 ----------
        v1 = Vehicle(
            name="沪A·11111 家用轿车（已持有 A 枪预约）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        v2 = Vehicle(
            name="沪B·22222 小型车（最大受电仅 60kW）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        v3 = Vehicle(
            name="沪C·33333 无障碍车辆（需无障碍车位）",
            connector_type="CCS", max_power_kw=100, accessible=True,
        )
        v4 = Vehicle(
            name="沪D·44444 长续航 SUV（请求 90kW 的第二位车主）",
            connector_type="CCS", max_power_kw=150, accessible=False,
        )
        # 候补演示专用车辆
        v5 = Vehicle(
            name="沪E·55555 候补 Q2 车（最大受电仅 60kW，宽松需求）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        v6 = Vehicle(
            name="沪F·66666 候补 Q1 车（需 ≥90kW，只能用高功率枪）",
            connector_type="CCS", max_power_kw=150, accessible=False,
        )
        db.add_all([v1, v2, v3, v4, v5, v6])
        db.flush()

        # ---------- 站点 1：东站（原演示数据，保持不变） ----------
        east = Site(name="东站充电站", location="东站地下停车场 B1 层西侧",
                    match_policy=MatchPolicy.FIFO)
        db.add(east)
        db.flush()

        east_guns = [
            Connector(site_id=east.id, code="A", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 1 号位，靠通道，无障碍车位"),
            Connector(site_id=east.id, code="B", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 2 号位，靠通道，无障碍车位"),
            Connector(site_id=east.id, code="C", connector_type="CCS",
                      rated_power_kw=60, accessible=False,
                      position_note="东侧靠墙，普通车位，额定仅 60kW"),
            Connector(site_id=east.id, code="D", connector_type="CHAdeMO",
                      rated_power_kw=120, accessible=False,
                      position_note="东侧柱后，CHAdeMO 接口，与 CCS 车辆不兼容"),
        ]
        db.add_all(east_guns)
        db.flush()
        gun_a = east_guns[0]

        existing = _confirmed_reservation(
            db, "R-SEEDA01", v1.id, east.id, gun_a,
            _dt("18:00"), _dt("18:30"), 90,
        )

        # ---------- 站点 2：西站·候补演示站 ----------
        # 题面样例：同时空出 18:00-18:30 的一支 120kW 枪 E 和一支 60kW 枪 F
        west = Site(name="西站·候补演示站",
                    location="西站地面停车场 P2 区",
                    match_policy=MatchPolicy.FIFO)
        db.add(west)
        db.flush()

        gun_e = Connector(site_id=west.id, code="E", connector_type="CCS",
                          rated_power_kw=120, accessible=True,
                          position_note="P2-01 高功率枪 120kW")
        gun_f = Connector(site_id=west.id, code="F", connector_type="CCS",
                          rated_power_kw=60, accessible=False,
                          position_note="P2-02 普通枪 60kW")
        db.add_all([gun_e, gun_f])
        db.flush()

        # 两支枪 18:00-18:30 均被有效预约占用；演示时取消它们即释放题面空档
        _confirmed_reservation(
            db, "R-SEEDH01", v1.id, west.id, gun_e,
            _dt("18:00"), _dt("18:30"), 90,
        )
        _confirmed_reservation(
            db, "R-SEEDL01", v2.id, west.id, gun_f,
            _dt("18:00"), _dt("18:30"), 60,
        )

        # ---------- 候补队列：Q2 先登记（宽松 60kW），Q1 后登记（严格 ≥90kW） ----------
        q2 = WaitlistRequest(
            ref="W-SEEDQ02",
            vehicle_id=v5.id,
            site_id=west.id,
            status=WaitlistStatus.QUEUED,
            min_power_kw=60,
            duration_minutes=30,
            arrive_by=_dt("18:00"),
            acceptable_site_ids="[]",
            created_at=_dt("12:00"),
        )
        q1 = WaitlistRequest(
            ref="W-SEEDQ01",
            vehicle_id=v6.id,
            site_id=west.id,
            status=WaitlistStatus.QUEUED,
            min_power_kw=90,
            duration_minutes=30,
            arrive_by=_dt("18:00"),
            acceptable_site_ids="[]",
            created_at=_dt("12:05"),
        )
        db.add_all([q2, q1])
        db.flush()
        for q, label, power in ((q2, "Q2", 60), (q1, "Q1", 90)):
            db.add(WaitlistEvent(
                waitlist_request_id=q.id,
                event_type="WAITLIST_JOINED",
                detail=json.dumps({
                    "ref": q.ref,
                    "site_id": west.id,
                    "vehicle_id": q.vehicle_id,
                    "min_power_kw": power,
                    "duration_minutes": 30,
                    "arrive_by": q.arrive_by,
                    "acceptable_site_ids": [],
                    "queue_position_note": "按 created_at, id 确定候补顺位",
                    "demo_label": label,
                }, ensure_ascii=False, default=str),
                created_at=q.created_at,
            ))
        db.commit()

        print("种子数据完成：")
        print(f"  东站(id=1)：A/B=120kW CCS，C=60kW CCS，D=120kW CHAdeMO；{existing.ref} 锁定 A 18:00-18:30")
        print("  西站·候补演示站(id=2)：E=120kW CCS、F=60kW CCS，18:00-18:30 分别被 R-SEEDH01/R-SEEDL01 占用")
        print("  候补：W-SEEDQ02(Q2) 12:00 先登记，仅需 60kW（E/F 皆可）；"
              "W-SEEDQ01(Q1) 12:05 后登记，需 ≥90kW（只能 E）")
        print("  => 取消西站两笔预约释放空档：FIFO 先给 Q2；兼容优先让 Q1 得 E、Q2 得 F")
    finally:
        db.close()


if __name__ == "__main__":
    seed()

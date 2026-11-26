"""题面场景端到端验证（FastAPI TestClient + 真实 PostgreSQL）。"""

import json
import threading

from tests.conftest import dt


# 种子固定 id：车辆 1..4；站点 1；枪口 A=1 B=2 C=3 D=4；种子预约 id=1
SITE = 1
V1, V2, V3, V4 = 1, 2, 3, 4
GUN_A, GUN_B, GUN_C, GUN_D = 1, 2, 3, 4


def _confirm(client, vehicle, start, minutes, power, mode="CONNECTOR",
             preferred=None, key="k-default-0001"):
    body = {
        "vehicle_id": vehicle,
        "site_id": SITE,
        "start_at": dt(start),
        "duration_minutes": minutes,
        "min_power_kw": power,
        "mode": mode,
        "idempotency_key": key,
    }
    if preferred is not None:
        body["preferred_connector_id"] = preferred
    return client.post("/api/reservations", json=body)


def _transition(client, rid, action, key):
    return client.post(f"/api/reservations/{rid}/{action}",
                       json={"idempotency_key": key})


# ---------- 1. 候选搜索：限制原因逐项可核验 ----------

def test_search_lists_every_gun_with_reasons(client):
    resp = client.post("/api/search", json={
        "vehicle_id": V4, "site_id": SITE,
        "start_at": dt("18:00"), "duration_minutes": 10,
        "min_power_kw": 90, "mode": "CONNECTOR",
    })
    assert resp.status_code == 200
    data = resp.json()
    by_code = {c["connector_code"]: c for c in data["connector_candidates"]}

    assert by_code["A"]["eligible"] is False
    assert any("R-SEEDA01" in r for r in by_code["A"]["reasons"])

    assert by_code["B"]["eligible"] is True
    assert by_code["B"]["effective_power_kw"] == 120.0

    assert by_code["C"]["eligible"] is False
    assert any("60" in r and "低于" in r for r in by_code["C"]["reasons"])

    assert by_code["D"]["eligible"] is False
    assert any("接口不兼容" in r for r in by_code["D"]["reasons"])


def test_pool_search_shows_single_remaining_slot(client):
    resp = client.post("/api/search", json={
        "vehicle_id": V4, "site_id": SITE,
        "start_at": dt("18:00"), "duration_minutes": 10,
        "min_power_kw": 90, "mode": "POOL",
    })
    data = resp.json()
    assert data["mode"] == "POOL"
    assert data["eligible"] is True
    assert data["confirmable_slots"] == 1  # A 被锁定，池内只剩 B 一个名额


def test_search_does_not_create_order(client):
    client.post("/api/search", json={
        "vehicle_id": V4, "site_id": SITE,
        "start_at": dt("18:00"), "duration_minutes": 10,
        "min_power_kw": 90,
    })
    reservations = client.get("/api/reservations").json()
    # 东站种子预约仍只有 R-SEEDA01（西站候补演示数据不计入本断言）
    east = [r for r in reservations if r["site_id"] == SITE]
    assert len(east) == 1


# ---------- 2. 并发：最后一个合格时段只能卖出一次 ----------

def _parallel_confirm(mode):
    from fastapi.testclient import TestClient
    from app.main import app

    barrier = threading.Barrier(2)
    results = []

    def worker(key):
        with TestClient(app) as c:
            barrier.wait()
            r = _confirm(c, V4, "18:00", 10, 90, mode=mode, key=key)
            results.append((r.status_code, r.json()))

    t1 = threading.Thread(target=worker, args=("key-conn-aaaa-001",))
    t2 = threading.Thread(target=worker, args=("key-conn-bbbb-002",))
    t1.start(); t2.start(); t1.join(); t2.join()
    return results


def test_concurrent_connector_mode_one_confirmed_one_rejected(client):
    results = _parallel_confirm("CONNECTOR")
    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 409]
    ok = next(b for s, b in results if s == 200)
    assert ok["connector_id"] == GUN_B  # 固定枪口路线返回 B
    fail = next(b for s, b in results if s == 409)
    assert "没有可确认的合格枪口" in fail["detail"]["message"]

    listing = client.get("/api/reservations").json()
    east = [r for r in listing if r["site_id"] == SITE]
    assert len(east) == 2  # 东站种子 + 唯一一笔新单


def test_concurrent_pool_mode_one_slot_one_winner(client):
    results = _parallel_confirm("POOL")
    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 409]
    ok = next(b for s, b in results if s == 200)
    assert ok["connector_id"] is None  # 池模式不指定枪号
    assert "资源池名额" in ok["confirmation_snapshot"]

    # 池内名额已耗尽：再来一笔必拒
    again = _confirm(client, V4, "18:00", 10, 90, mode="POOL", key="key-pool-cccc-003")
    assert again.status_code == 409
    assert again.json()["detail"]["confirmable_slots"] == 0


# ---------- 3. 幂等：重复提交只返回原结果 ----------

def test_idempotent_success_replay(client):
    r1 = _confirm(client, V4, "18:00", 10, 90, key="key-idem-aaaa-001")
    assert r1.status_code == 200
    ref = r1.json()["ref"]

    r2 = _confirm(client, V4, "18:00", 10, 90, key="key-idem-aaaa-001")
    assert r2.status_code == 200
    assert r2.json()["ref"] == ref  # 同一笔订单
    east = [r for r in client.get("/api/reservations").json() if r["site_id"] == SITE]
    assert len(east) == 2


def test_idempotent_failure_replay(client):
    # 先占掉 B
    _confirm(client, V4, "18:00", 10, 90, key="key-idem-bbbb-001")
    # 同参数新请求首次失败
    f1 = _confirm(client, V4, "18:00", 10, 90, key="key-idem-cccc-002")
    assert f1.status_code == 409
    first_body = f1.json()
    # 重复提交：返回同一个失败结果，而不是重新判定
    f2 = _confirm(client, V4, "18:00", 10, 90, key="key-idem-cccc-002")
    assert f2.status_code == 409
    assert f2.json() == first_body


def test_same_key_different_payload_rejected(client):
    _confirm(client, V4, "18:00", 10, 90, key="key-idem-dddd-003")
    r = _confirm(client, V4, "18:00", 20, 90, key="key-idem-dddd-003")
    assert r.status_code == 409
    assert "不同的请求参数" in r.json()["detail"]


# ---------- 4. 18:10 降额：按真实交集判定 ----------

def _derate_b(client, start, end, power, key):
    return client.post("/api/operations/maintenance", json={
        "connector_id": GUN_B, "kind": "DERATE",
        "start_at": dt(start), "planned_end_at": dt(end),
        "effective_power_kw": power, "reason": "模块高温降额",
        "idempotency_key": key,
    })


def test_derate_at_1810_spares_ended_reservation(client):
    # B 上的预约 18:00-18:10（与种子 A 预约并存）
    book = _confirm(client, V4, "18:00", 10, 90, key="key-der-aaaa-001")
    assert book.status_code == 200
    b_ref = book.json()["ref"]

    impact = _derate_b(client, "18:10", "19:00", 60, "key-der-bbbb-002")
    assert impact.status_code == 200
    refs = {i["ref"]: i for i in impact.json()["impacts"]}
    # 18:10 已结束的预约与降额无交集，不在影响清单内，更不会被撤销
    assert b_ref not in refs
    status = client.get(f"/api/reservations/{book.json()['id']}").json()["status"]
    assert status == "CONFIRMED"


def test_after_derate_90kw_rejected_but_60kw_uses_b(client):
    _derate_b(client, "18:10", "19:00", 60, "key-der-cccc-003")

    # 18:15 起的 90kW 请求：A 占用、B 降额 60、C 额定 60、D 接口不符
    search = client.post("/api/search", json={
        "vehicle_id": V4, "site_id": SITE,
        "start_at": dt("18:15"), "duration_minutes": 10,
        "min_power_kw": 90,
    }).json()
    assert search["eligible"] is False
    by_code = {c["connector_code"]: c for c in search["connector_candidates"]}
    assert any("60" in r for r in by_code["B"]["reasons"])

    denied = _confirm(client, V4, "18:15", 10, 90, key="key-der-dddd-004")
    assert denied.status_code == 409

    # 只接受 60kW 的车（V2）仍可使用 B（降额后 60kW 恰好满足）
    ok = _confirm(client, V2, "18:15", 10, 60, key="key-der-eeee-005")
    assert ok.status_code == 200
    assert ok.json()["connector_id"] == GUN_B


def test_mid_window_derate_uses_real_intersection(client):
    # 整晚预约 18:00-18:30 锁定 B
    book = _confirm(client, V4, "18:00", 30, 90, key="key-mid-aaaa-001")
    rid = book.json()["id"]

    impact = _derate_b(client, "18:10", "18:30", 60, "key-mid-bbbb-002").json()
    item = next(i for i in impact["impacts"] if i["reservation_id"] == rid)
    assert item["decision"] == "NEEDS_RESELECTION"
    assert item["overlap_start"] == dt("18:10")
    assert "18:00-18:10" in item["reason"]  # 已完成区间与待履约区间区别说明

    refreshed = client.get(f"/api/reservations/{rid}").json()
    assert refreshed["status"] == "NEEDS_RESELECTION"
    # 确认快照不被改写
    assert '"connector_code": "B"' in refreshed["confirmation_snapshot"]


def test_derate_still_satisfying_keeps_fulfilling(client):
    # V2 只要求 60kW：B 降到 60kW 仍满足，继续履约
    book = _confirm(client, V2, "18:15", 15, 60, key="key-mid-cccc-003")
    rid = book.json()["id"]
    impact = _derate_b(client, "18:10", "19:00", 60, "key-mid-dddd-004").json()
    item = next(i for i in impact["impacts"] if i["reservation_id"] == rid)
    assert item["decision"] == "UNCHANGED"
    assert client.get(f"/api/reservations/{rid}").json()["status"] == "CONFIRMED"


# ---------- 5. 取消释放 / 迟到 / 状态机 ----------

def test_cancel_releases_capacity_and_is_idempotent(client):
    book = _confirm(client, V4, "18:00", 10, 90, key="key-can-aaaa-001")
    rid = book.json()["id"]

    c1 = _transition(client, rid, "cancel", "key-can-bbbb-002")
    assert c1.status_code == 200 and c1.json()["status"] == "CANCELLED"
    # 重复提交取消：返回原结果
    c2 = _transition(client, rid, "cancel", "key-can-bbbb-002")
    assert c2.status_code == 200 and c2.json()["status"] == "CANCELLED"

    # B 容量释放，新预约可以进入
    again = _confirm(client, V4, "18:00", 10, 90, key="key-can-cccc-003")
    assert again.status_code == 200
    assert again.json()["connector_id"] == GUN_B

    # 已取消订单不能再次取消（新幂等键回放同一个 409）
    x1 = _transition(client, rid, "cancel", "key-can-dddd-004")
    x2 = _transition(client, rid, "cancel", "key-can-dddd-004")
    assert x1.status_code == 409 and x2.json() == x1.json()


def test_no_show_only_after_start_and_releases(client):
    book = _confirm(client, V4, "18:00", 10, 90, key="key-ns-aaaa-001")
    rid = book.json()["id"]
    early = _transition(client, rid, "no-show", "key-ns-bbbb-002")
    assert early.status_code == 409  # 未到开始时间不能判迟到


# ---------- 6. 池模式到站分配 ----------

def test_pool_arrival_assigns_connector(client):
    book = _confirm(client, V4, "18:00", 10, 90, mode="POOL", key="key-arr-aaaa-001")
    rid = book.json()["id"]
    assert book.json()["connector_id"] is None

    arr = _transition(client, rid, "arrive", "key-arr-bbbb-002")
    assert arr.status_code == 200
    assert arr.json()["status"] == "ARRIVED"
    assert arr.json()["connector_id"] == GUN_B  # A 占用，分配 B


def test_pool_arrival_fails_when_pool_degraded(client):
    book = _confirm(client, V4, "18:00", 10, 90, mode="POOL", key="key-arr-cccc-003")
    rid = book.json()["id"]
    # B 在到站前停用，池内已无合格枪：维护创建时即把该池预约判为改选
    impact = client.post("/api/operations/maintenance", json={
        "connector_id": GUN_B, "kind": "OUTAGE",
        "start_at": dt("17:55"), "planned_end_at": dt("19:00"),
        "reason": "紧急检修", "idempotency_key": "key-arr-dddd-004",
    }).json()
    item = next(i for i in impact["impacts"] if i["reservation_id"] == rid)
    assert item["decision"] == "NEEDS_RESELECTION"
    assert client.get(f"/api/reservations/{rid}").json()["status"] == "NEEDS_RESELECTION"
    # 改选状态的订单不允许直接到站
    arr = _transition(client, rid, "arrive", "key-arr-eeee-005")
    assert arr.status_code == 409


# ---------- 7. 开始充电后故障保护 ----------

def test_charging_session_protected_from_maintenance_and_cancel(client):
    book = _confirm(client, V4, "18:00", 30, 90, key="key-chg-aaaa-001")
    rid = book.json()["id"]
    _transition(client, rid, "arrive", "key-chg-bbbb-002")
    _transition(client, rid, "start", "key-chg-cccc-003")

    impact = client.post("/api/operations/maintenance", json={
        "connector_id": GUN_B, "kind": "OUTAGE",
        "start_at": dt("18:10"), "planned_end_at": dt("18:30"),
        "reason": "枪口故障", "idempotency_key": "key-chg-dddd-004",
    }).json()
    item = next(i for i in impact["impacts"] if i["reservation_id"] == rid)
    assert item["decision"] == "PROTECTED_CHARGING"
    assert client.get(f"/api/reservations/{rid}").json()["status"] == "CHARGING"

    # 预约管理不能直接释放充电会话
    cancel = _transition(client, rid, "cancel", "key-chg-eeee-005")
    assert cancel.status_code == 409
    # 客服也不能通过替代枪口流程迁移充电会话
    migrate = client.post("/api/operations/service-actions", json={
        "reservation_id": rid, "kind": "ALTERNATIVE_CONNECTOR",
        "target_connector_id": GUN_A, "idempotency_key": "key-chg-ffff-006",
    })
    assert migrate.status_code == 409

    # 完成充电后释放
    done = _transition(client, rid, "complete", "key-chg-gggg-007")
    assert done.json()["status"] == "COMPLETED"


# ---------- 8. 客服替代枪口 / 费用调整与快照 ----------

def test_service_actions_snapshots_and_migration(client):
    # V2（60kW）预约 B，随后 B 停用进入改选，C 同为 60kW CCS 可替代
    book = _confirm(client, V2, "18:15", 15, 60, key="key-svc-aaaa-001")
    rid = book.json()["id"]
    client.post("/api/operations/maintenance", json={
        "connector_id": GUN_B, "kind": "OUTAGE",
        "start_at": dt("18:10"), "planned_end_at": dt("19:00"),
        "reason": "B 枪故障", "idempotency_key": "key-svc-bbbb-002",
    })
    assert client.get(f"/api/reservations/{rid}").json()["status"] == "NEEDS_RESELECTION"

    migrate = client.post("/api/operations/service-actions", json={
        "reservation_id": rid, "kind": "ALTERNATIVE_CONNECTOR",
        "target_connector_id": GUN_C, "note": "客服改派 C 枪",
        "idempotency_key": "key-svc-cccc-003",
    })
    assert migrate.status_code == 200
    moved = client.get(f"/api/reservations/{rid}").json()
    assert moved["connector_id"] == GUN_C
    assert moved["status"] == "CONFIRMED"
    assert "B 枪故障" in migrate.json()["connector_state_snapshot"]
    snap = json.loads(migrate.json()["connector_state_snapshot"])
    assert snap["from_connector_state"]["code"] == "B"
    assert snap["to_connector_state"]["code"] == "C"
    # 原确认快照保持不变
    assert '"connector_code": "B"' in moved["confirmation_snapshot"]

    fee = client.post("/api/operations/service-actions", json={
        "reservation_id": rid, "kind": "FEE_ADJUSTMENT",
        "amount": -20.5, "note": "等待补偿券",
        "idempotency_key": "key-svc-dddd-004",
    })
    assert fee.status_code == 200
    assert float(fee.json()["amount"]) == -20.5
    assert fee.json()["order_status_at_action"] == "CONFIRMED"

    # 替代到不兼容/不满足的枪必须被拒绝
    bad = client.post("/api/operations/service-actions", json={
        "reservation_id": rid, "kind": "ALTERNATIVE_CONNECTOR",
        "target_connector_id": GUN_D,
        "idempotency_key": "key-svc-eeee-005",
    })
    assert bad.status_code == 409


def test_recovery_does_not_rewrite_history(client):
    book = _confirm(client, V2, "18:15", 15, 60, key="key-rec-aaaa-001")
    rid = book.json()["id"]
    opened = client.post("/api/operations/maintenance", json={
        "connector_id": GUN_B, "kind": "OUTAGE",
        "start_at": dt("18:10"), "planned_end_at": dt("19:00"),
        "reason": "检修", "idempotency_key": "key-rec-bbbb-002",
    }).json()
    wid = opened["maintenance"]["id"]

    rec = client.post(f"/api/operations/maintenance/{wid}/recover", json={
        "ended_at": dt("18:20"), "idempotency_key": "key-rec-cccc-003",
    })
    assert rec.status_code == 200
    assert rec.json()["ended_at"] == dt("18:20")

    refreshed = client.get(f"/api/reservations/{rid}").json()
    # 设备恢复不自动回滚订单状态
    assert refreshed["status"] == "NEEDS_RESELECTION"
    event_types = {e["event_type"] for e in refreshed["events"]}
    assert "DEVICE_RECOVERED" in event_types
    # 确认内容原封不动
    assert '"connector_code": "B"' in refreshed["confirmation_snapshot"]


# ---------- 9. 无障碍车辆 ----------

def test_accessible_vehicle_requirements(client):
    ok = client.post("/api/search", json={
        "vehicle_id": V3, "site_id": SITE,
        "start_at": dt("18:00"), "duration_minutes": 10,
        "min_power_kw": 90,
    }).json()
    by_code = {c["connector_code"]: c for c in ok["connector_candidates"]}
    assert by_code["B"]["eligible"] is True
    assert any("无障碍" in r for r in by_code["C"]["reasons"])

    # B 停用后，无障碍 90kW 无合格枪
    client.post("/api/operations/maintenance", json={
        "connector_id": GUN_B, "kind": "OUTAGE",
        "start_at": dt("17:55"), "planned_end_at": dt("19:00"),
        "reason": "围挡施工", "idempotency_key": "key-acc-aaaa-001",
    })
    denied = client.post("/api/search", json={
        "vehicle_id": V3, "site_id": SITE,
        "start_at": dt("18:00"), "duration_minutes": 10,
        "min_power_kw": 90,
    }).json()
    assert denied["eligible"] is False

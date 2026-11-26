"""候补（waitlist）题面场景验证：两条公平规则、能力/时长硬约束、
一次性邀请、并发接受互斥、超时与迟到接受、拒绝/再拒绝、人工保留、替代站点。

种子固定 id：车辆 1..6（V5=60kW 宽松车，V6=150kW 严格车）；
站点 1=东站，2=西站；西站枪口 E=5(120kW)、F=6(60kW)；
西站预约 R-SEEDH01(id=2，占 E)、R-SEEDL01(id=3，占 F)；
种子候补 W-SEEDQ02(id=1，Q2，60kW，12:00 先登记)、
W-SEEDQ01(id=2，Q1，90kW，12:05 后登记)。
"""

import threading
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services import waitlist as wl_service
from tests.conftest import dt

EAST, WEST = 1, 2
V5, V6 = 5, 6
GUN_E, GUN_F = 5, 6
RES_E, RES_F = 2, 3
Q2, Q1 = 1, 2


def _key(tag):
    return f"wl-{tag}-{threading.get_ident()}"


def _join(client, site, vehicle, power, minutes, arrive_by, alts=None, tag="x"):
    body = {
        "vehicle_id": vehicle,
        "site_id": site,
        "min_power_kw": power,
        "duration_minutes": minutes,
        "arrive_by": dt(arrive_by),
        "acceptable_site_ids": alts or [],
        "idempotency_key": _key(tag),
    }
    return client.post("/api/waitlist", json=body)


def _cancel_res(client, rid, tag):
    return client.post(f"/api/reservations/{rid}/cancel",
                       json={"idempotency_key": _key(tag)})


def _cancel_wl(client, wid, tag):
    return client.post(f"/api/waitlist/{wid}/cancel",
                       json={"idempotency_key": _key(tag)})


def _clear_seed_queue(client):
    """把种子自带的 Q1/Q2 候补撤销，得到干净队列（失败可忽略）。"""
    for wid in (Q2, Q1):
        client.post(f"/api/waitlist/{wid}/cancel",
                    json={"idempotency_key": _key(f"clear-{wid}")})


def _wl_by_ref(client, ref):
    return next(w for w in client.get("/api/waitlist").json() if w["ref"] == ref)


def _pending_invites(client):
    return [i for i in client.get("/api/waitlist/invites").json()
            if i["status"] == "PENDING"]


# ---------- 1. 登记校验 ----------

def test_join_rejects_vehicle_below_min_power(client):
    # V5 最大受电仅 60kW，候补却要求 90kW
    r = _join(client, WEST, V5, 90, 30, "18:00", tag="jp")
    assert r.status_code == 422
    assert "最大受电" in r.json()["detail"]


def test_join_persists_constraints_and_queue_order(client):
    _clear_seed_queue(client)
    r = _join(client, WEST, V5, 60, 30, "18:00", alts=[EAST], tag="j1")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "QUEUED"
    assert data["min_power_kw"] == 60.0
    assert data["duration_minutes"] == 30
    assert data["events"][0]["event_type"] == "WAITLIST_JOINED"
    # 登记本身不产生任何预约
    assert data["reservation_id"] is None


# ---------- 2. FIFO：严格先后，早登记的宽松需求占走稀缺高功率枪 ----------

def test_fifo_q2_takes_high_power_gun_leaving_q1_unmatched(client):
    # 种子策略即 FIFO；Q2(12:00,60kW) 早于 Q1(12:05,90kW)
    policy = client.post(f"/api/sites/{WEST}/policy", json={
        "match_policy": "FIFO", "idempotency_key": _key("pol-fifo"),
    })
    assert policy.status_code == 200 and policy.json()["match_policy"] == "FIFO"

    # 先释放高功率枪 E
    _cancel_res(client, RES_E, "fifo-e")
    invites = _pending_invites(client)
    assert len(invites) == 1
    assert invites[0]["waitlist_request_id"] == Q2   # 先登记的 Q2 拿到 120kW 的 E
    assert invites[0]["connector_id"] == GUN_E

    # 再释放 60kW 枪 F：Q1 需要 ≥90kW，F 不满足；Q2 已在邀请中，不能重复发
    _cancel_res(client, RES_F, "fifo-f")
    pending = _pending_invites(client)
    assert len(pending) == 1  # 没有新增邀请
    q1 = next(w for w in client.get("/api/waitlist").json() if w["id"] == Q1)
    assert q1["status"] == "QUEUED" and q1["invites"] == []


# ---------- 3. 兼容优先：严格请求先用稀缺枪，两人都能接单 ----------

def test_compatibility_policy_serves_both_drivers(client):
    pol = client.post(f"/api/sites/{WEST}/policy", json={
        "match_policy": "COMPATIBILITY", "idempotency_key": _key("pol-comp"),
    })
    assert pol.status_code == 200

    _cancel_res(client, RES_E, "comp-e")  # 释放 120kW 枪 E
    invites_e = _pending_invites(client)
    assert len(invites_e) == 1
    assert invites_e[0]["waitlist_request_id"] == Q1   # 只有 E 能满足的 Q1 优先
    assert invites_e[0]["connector_id"] == GUN_E

    _cancel_res(client, RES_F, "comp-f")  # 释放 60kW 枪 F
    by_wl = {i["waitlist_request_id"]: i for i in _pending_invites(client)}
    assert set(by_wl) == {Q1, Q2}
    assert by_wl[Q2]["connector_id"] == GUN_F          # 宽松的 Q2 用 60kW 枪
    for w in client.get("/api/waitlist").json():
        if w["id"] in (Q1, Q2):
            assert w["status"] == "INVITED"


# ---------- 4. 邀请不是预约，接受后才确认 ----------

def test_invite_is_not_reservation_until_accepted(client):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="inv").json()
    west_rows_before = [r for r in client.get("/api/reservations").json()
                        if r["site_id"] == WEST]

    resp = _cancel_res(client, RES_E, "inv-rel")
    # 取消事件里说明候补邀请不等于预约
    assert any(e["event_type"] == "WAITLIST_DISPATCH" for e in resp.json()["events"])

    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])
    assert "不是已确认预约" in next(
        e["detail"] for w in client.get("/api/waitlist").json()
        if w["id"] == entry["id"] for e in w["events"]
        if e["event_type"] == "INVITE_ISSUED")

    # 接受前：候补是 INVITED，但西站没有新增预约
    west_rows_after_cancel = [r for r in client.get("/api/reservations").json()
                              if r["site_id"] == WEST]
    assert len(west_rows_after_cancel) == len(west_rows_before)
    wl = next(w for w in client.get("/api/waitlist").json() if w["id"] == entry["id"])
    assert wl["status"] == "INVITED"

    accepted = client.post(f"/api/waitlist/invites/{invite['id']}/accept", json={
        "idempotency_key": _key("accept"),
    })
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["invite"]["status"] == "ACCEPTED"
    assert body["reservation"]["connector_id"] == GUN_E
    assert body["reservation"]["status"] == "CONFIRMED"
    snap = body["reservation"]["confirmation_snapshot"]
    assert "WAITLIST_INVITE" in snap

    wl = next(w for w in client.get("/api/waitlist").json() if w["id"] == entry["id"])
    assert wl["status"] == "CONFIRMED" and wl["reservation_id"] == body["reservation"]["id"]


# ---------- 5. 并发接受同一空档：只能一笔确认 ----------

def test_concurrent_accept_only_one_confirmation(client):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="conc").json()
    _cancel_res(client, RES_E, "conc-rel")
    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])

    barrier = threading.Barrier(2)
    results = []

    def worker(tag):
        with TestClient(app) as c:
            barrier.wait()
            r = c.post(f"/api/waitlist/invites/{invite['id']}/accept", json={
                "idempotency_key": _key(tag),
            })
            results.append((r.status_code, r.json()))

    t1 = threading.Thread(target=worker, args=("conc-a",))
    t2 = threading.Thread(target=worker, args=("conc-b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(s for s, _ in results) == [200, 409]
    loser = next(b for s, b in results if s == 409)
    assert "ACCEPTED" in str(loser) or "不能重复接受" in str(loser)

    # 全局只有一笔由该邀请产生的确认预约
    invite_rows = [
        r for r in client.get("/api/reservations").json()
        if "WAITLIST_INVITE" in r["confirmation_snapshot"]
    ]
    assert len(invite_rows) == 1
    refreshed = client.get(f"/api/waitlist/invites/{invite['id']}").json()
    assert refreshed["status"] == "ACCEPTED"


def test_accept_is_idempotent_on_replay(client):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="idem").json()
    _cancel_res(client, RES_E, "idem-rel")
    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])
    key = _key("idem-accept")
    r1 = client.post(f"/api/waitlist/invites/{invite['id']}/accept",
                     json={"idempotency_key": key})
    r2 = client.post(f"/api/waitlist/invites/{invite['id']}/accept",
                     json={"idempotency_key": key})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["reservation"]["id"] == r2.json()["reservation"]["id"]


# ---------- 6. 超时与迟到接受：过期确认不能写成成功 ----------

def test_late_accept_after_deadline_is_rejected_not_confirmed(client, monkeypatch):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="late").json()
    _cancel_res(client, RES_E, "late-rel")
    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])
    ttl = invite["expires_at"]

    # 时钟拨到截止时刻之后
    monkeypatch.setattr(wl_service, "_now",
                        lambda: datetime.fromisoformat(ttl) + timedelta(seconds=30))

    late = client.post(f"/api/waitlist/invites/{invite['id']}/accept", json={
        "idempotency_key": _key("late-accept"),
    })
    assert late.status_code == 409
    assert "超过截止时刻" in late.json()["detail"]["message"]

    # 迟到接受是业务失败，按幂等框架约定不产生部分写入、可原样重放
    replay = client.post(f"/api/waitlist/invites/{invite['id']}/accept", json={
        "idempotency_key": _key("late-accept"),
    })
    assert replay.status_code == 409 and replay.json() == late.json()
    assert not any("WAITLIST_INVITE" in r["confirmation_snapshot"]
                   for r in client.get("/api/reservations").json())

    # 超时状态由巡检持久化（同样在截止时刻之后），状态记录可核验
    client.post("/api/waitlist/sweep", json={
        "site_id": WEST, "idempotency_key": _key("late-sweep"),
    })
    refreshed = client.get(f"/api/waitlist/invites/{invite['id']}").json()
    assert refreshed["status"] == "EXPIRED"
    wl = next(w for w in client.get("/api/waitlist").json() if w["id"] == entry["id"])
    assert wl["status"] == "EXPIRED" and wl["reservation_id"] is None

    # 巡检后再次接受仍是失败，不会补确认
    again = client.post(f"/api/waitlist/invites/{invite['id']}/accept", json={
        "idempotency_key": _key("late-again"),
    })
    assert again.status_code == 409


def test_sweep_expires_invites_and_closes_stale_requests(client, monkeypatch):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="swp").json()
    _cancel_res(client, RES_E, "swp-rel")
    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])

    future = datetime.fromisoformat(invite["expires_at"]) + timedelta(seconds=10)
    monkeypatch.setattr(wl_service, "_now", lambda: future)
    sweep = client.post("/api/waitlist/sweep", json={
        "site_id": WEST, "idempotency_key": _key("sweep"),
    })
    assert sweep.status_code == 200
    assert sweep.json()["expired_invites"] >= 1

    assert client.get(f"/api/waitlist/invites/{invite['id']}").json()["status"] == "EXPIRED"
    wl = next(w for w in client.get("/api/waitlist").json() if w["id"] == entry["id"])
    assert wl["status"] == "EXPIRED"
    assert any(e["event_type"] == "INVITE_EXPIRED" for e in wl["events"])


# ---------- 7. 拒绝、再次拒绝与重新邀请 ----------

def test_decline_reinvite_and_decline_again_keeps_queue_place(client):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V5, 60, 30, "18:00", tag="dec").json()

    _cancel_res(client, RES_E, "dec-e")
    invite1 = next(i for i in _pending_invites(client)
                   if i["waitlist_request_id"] == entry["id"])
    d1 = client.post(f"/api/waitlist/invites/{invite1['id']}/decline", json={
        "reason": "临时来不了", "idempotency_key": _key("dec-1"),
    })
    assert d1.status_code == 200 and d1.json()["status"] == "DECLINED"

    # 回到开放队列后，第二个空档应再次发邀请
    _cancel_res(client, RES_F, "dec-f")
    invite2 = next(i for i in client.get("/api/waitlist/invites").json()
                   if i["waitlist_request_id"] == entry["id"] and i["id"] != invite1["id"])
    assert invite2["connector_id"] == GUN_F
    d2 = client.post(f"/api/waitlist/invites/{invite2['id']}/decline", json={
        "idempotency_key": _key("dec-2"),
    })
    assert d2.json()["status"] == "DECLINED"

    wl = next(w for w in client.get("/api/waitlist").json() if w["id"] == entry["id"])
    declined = [i for i in wl["invites"] if i["status"] == "DECLINED"]
    assert len(declined) == 2
    assert sum(1 for e in wl["events"] if e["event_type"] == "INVITE_DECLINED") == 2
    assert wl["status"] == "DECLINED"  # 仍在开放队列，顺位不变


# ---------- 8. 人工保留 ----------

def test_manual_hold_supersedes_pending_invite(client):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="hold").json()
    _cancel_res(client, RES_E, "hold-rel")
    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])

    held = client.post(f"/api/waitlist/invites/{invite['id']}/hold", json={
        "note": "站长人工保留该枪", "idempotency_key": _key("hold-1"),
    })
    assert held.status_code == 200 and held.json()["status"] == "SUPERSEDED"
    wl = next(w for w in client.get("/api/waitlist").json() if w["id"] == entry["id"])
    assert wl["status"] == "QUEUED"
    assert any(e["event_type"] == "INVITE_MANUAL_HOLD" for e in wl["events"])

    # 失效邀请不能再被接受
    late = client.post(f"/api/waitlist/invites/{invite['id']}/accept", json={
        "idempotency_key": _key("hold-accept"),
    })
    assert late.status_code == 409


def test_cancel_waitlist_supersedes_its_pending_invite(client):
    _clear_seed_queue(client)
    entry = _join(client, WEST, V6, 90, 30, "18:00", tag="wlc").json()
    _cancel_res(client, RES_E, "wlc-rel")
    invite = next(i for i in _pending_invites(client)
                  if i["waitlist_request_id"] == entry["id"])

    r = _cancel_wl(client, entry["id"], "wlc-go")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert client.get(f"/api/waitlist/invites/{invite['id']}").json()["status"] == "SUPERSEDED"


# ---------- 9. 硬约束：功率 / 完整时长 / 到站期限 ----------

def test_matching_requires_power_duration_and_deadline_fit(client):
    _clear_seed_queue(client)
    # 三条都不该被 60kW / 30 分钟的 F 枪空档选中
    _join(client, WEST, V6, 90, 30, "18:00", tag="g-power")    # 功率不足
    _join(client, WEST, V5, 60, 45, "18:00", tag="g-dur")      # 时长盖不住
    _join(client, WEST, V5, 60, 30, "17:59", tag="g-deadline") # 晚于到站期限
    # 唯一合格者
    good = _join(client, WEST, V5, 60, 30, "18:00", tag="g-good").json()

    _cancel_res(client, RES_F, "g-rel")  # 只释放 60kW 的 F
    pending = _pending_invites(client)
    assert len(pending) == 1
    assert pending[0]["waitlist_request_id"] == good["id"]
    assert pending[0]["connector_id"] == GUN_F


# ---------- 10. 可接受替代站点 ----------

def test_invite_can_go_to_acceptable_alternative_site(client):
    _clear_seed_queue(client)
    # 在东站登记，但把西站列为可接受替代站点
    entry = _join(client, EAST, V5, 60, 30, "18:00", alts=[WEST], tag="alt").json()
    _cancel_res(client, RES_F, "alt-rel")  # 西站释放 F 枪
    pending = _pending_invites(client)
    assert len(pending) == 1
    inv = pending[0]
    assert inv["waitlist_request_id"] == entry["id"]
    assert inv["site_id"] == WEST and inv["connector_id"] == GUN_F


# ---------- 11. 服务层：只释放未开始时段、接口不兼容不发邀请、池名额只发一个邀请 ----------

def test_only_unstarted_part_is_released(db):
    reservation = db.get(wl_service.Reservation, RES_E)  # 18:00-18:30
    now = datetime.fromisoformat(dt("18:10"))
    slots, max_offers = wl_service.build_slots_for_released(db, reservation, now)
    assert max_offers == 1
    assert slots[0].start_at == now                 # 已开始的 18:00-18:10 不释放
    assert slots[0].end_at == datetime.fromisoformat(dt("18:30"))


def test_pool_release_emits_only_one_invite(db):
    """资源池取消只释放 1 个名额：即便多支枪空闲，也最多发 1 个邀请。"""
    from app.enums import ReservationMode, ReservationStatus, WaitlistStatus
    from app.models import Reservation, Vehicle, WaitlistRequest

    # 西站两笔固定占位先撤销，让 E/F 都空闲，再放一笔池模式在途预约
    for rid in (RES_E, RES_F):
        r = db.get(Reservation, rid)
        r.status = ReservationStatus.CANCELLED
    pool_res = Reservation(
        ref="R-POOLTEST", vehicle_id=V6, site_id=WEST,
        mode=ReservationMode.POOL, status=ReservationStatus.CONFIRMED,
        start_at=datetime.fromisoformat(dt("18:00")),
        end_at=datetime.fromisoformat(dt("18:30")),
        min_power_kw=60, connector_id=None, confirmation_snapshot="{}",
    )
    db.add(pool_res)
    db.flush()
    # 两条都合格的候补
    for idx, vid in ((1, V5), (2, V6)):
        db.add(WaitlistRequest(
            ref=f"W-POOL{idx}", vehicle_id=vid, site_id=WEST,
            status=WaitlistStatus.QUEUED, min_power_kw=60, duration_minutes=30,
            arrive_by=datetime.fromisoformat(dt("18:00")),
            acceptable_site_ids="[]",
        ))
    db.flush()
    pool_res.status = ReservationStatus.CANCELLED  # 释放 1 个池名额
    invites = wl_service.dispatch_for_cancellation(
        db, pool_res, ttl_seconds=120,
    )
    db.flush()
    assert len(invites) == 1  # 只有一个名额，绝不发两份邀请
    db.rollback()


def test_connector_interface_mismatch_never_invited(db):
    from app.enums import WaitlistStatus
    from app.models import Connector, Vehicle, WaitlistRequest

    site = db.get(wl_service.Site, WEST)
    chademo = Vehicle(name="测试-CHAdeMO 车", connector_type="CHAdeMO",
                      max_power_kw=120, accessible=False)
    ccs = Vehicle(name="测试-CCS 车", connector_type="CCS",
                  max_power_kw=120, accessible=False)
    db.add_all([chademo, ccs])
    db.flush()
    wl_bad = WaitlistRequest(
        ref="W-TESTCHA", vehicle_id=chademo.id, site_id=WEST,
        status=WaitlistStatus.QUEUED, min_power_kw=60, duration_minutes=30,
        arrive_by=datetime.fromisoformat(dt("18:00")), acceptable_site_ids="[]",
    )
    wl_good = WaitlistRequest(
        ref="W-TESTCCS", vehicle_id=ccs.id, site_id=WEST,
        status=WaitlistStatus.QUEUED, min_power_kw=60, duration_minutes=30,
        arrive_by=datetime.fromisoformat(dt("18:00")), acceptable_site_ids="[]",
    )
    db.add_all([wl_bad, wl_good])
    db.flush()

    gun_f = db.get(Connector, GUN_F)
    slot = wl_service.Slot(WEST, gun_f,
                           datetime.fromisoformat(dt("18:00")),
                           datetime.fromisoformat(dt("18:30")), 60.0)
    invites = wl_service.dispatch_for_slots(
        db, WEST, [slot], RES_F, ttl_seconds=120,
        now=datetime.fromisoformat(dt("12:30")),
    )
    assert len(invites) == 1
    assert invites[0].waitlist_request_id == wl_good.id  # CHAdeMO 车拿不到 CCS 枪
    db.rollback()

#!/usr/bin/env bash
# 题面场景手工 E2E：针对运行中的 API（默认 127.0.0.1:8000）逐步验证。
# 前置：数据库已重置为种子状态（python -m app.seed）。
# 用法：bash e2e_check.sh
set -u
BASE="${BASE:-http://127.0.0.1:8000}"
PY="${PY:-python3}"
F="2026-11-27T18"
pass=0; fail=0

check() { # desc expected actual
  if [ "$2" = "$3" ]; then echo "  ✅ $1 ($3)"; pass=$((pass+1));
  else echo "  ❌ $1：期望 [$2] 实际 [$3]"; fail=$((fail+1)); fi
}
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
code() { curl -s -o /tmp/e2e_body.json -w '%{http_code}' -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }

echo "== 0. 健康检查 =="
check "health" "ok" "$(curl -s $BASE/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 1. 候选搜索（18:00-18:10，90kW，固定枪口）：A 占用 / B 唯一合格 / C 功率不足 / D 接口不符 =="
post /api/search "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"CONNECTOR\"}" | python3 -c '
import json,sys
d=json.load(sys.stdin); by={c["connector_code"]:c for c in d["connector_candidates"]}
assert by["A"]["eligible"] is False and any("R-SEEDA01" in r for r in by["A"]["reasons"])
assert by["B"]["eligible"] is True
assert by["C"]["eligible"] is False and any("低于" in r for r in by["C"]["reasons"])
assert by["D"]["eligible"] is False and any("接口不兼容" in r for r in by["D"]["reasons"])
print("  ✅ 四枪判定与限制原因齐备")
' || fail=$((fail+1))

echo "== 2. 资源池搜索：池内只剩 1 个可确认名额 =="
check "池内可确认名额" "1" "$(post /api/search "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"POOL\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["confirmable_slots"])')"

echo "== 3. 并发抢 B（固定枪口）：一笔 200 锁定 B，一笔 409 =="
rm -f /tmp/c1.json /tmp/c2.json /tmp/c1.code /tmp/c2.code
curl -s -o /tmp/c1.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"CONNECTOR\",\"idempotency_key\":\"e2e-conn-aaaa-1\"}" > /tmp/c1.code &
curl -s -o /tmp/c2.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"CONNECTOR\",\"idempotency_key\":\"e2e-conn-bbbb-2\"}" > /tmp/c2.code &
wait
check "并发结果码集合" "200,409," "$(sort /tmp/c1.code /tmp/c2.code | tr '\n' ',')"
check "胜出订单锁定 B 枪" "2" "$(python3 -c '
import json
if open("/tmp/c1.code").read()=="200": w=json.load(open("/tmp/c1.json"))
else: w=json.load(open("/tmp/c2.json"))
print(w["connector_id"])')"

echo "== 4. 幂等：同键重放只返回原订单（放在 20:00 避免与后续场景交叉）=="
R1=$(post /api/reservations "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"2026-11-27T20:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"idempotency_key\":\"e2e-idem-aaaa-1\"}")
REF1=$(echo "$R1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ref"])')
R2=$(post /api/reservations "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"2026-11-27T20:00:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"idempotency_key\":\"e2e-idem-aaaa-1\"}")
check "重放返回同一 ref" "$REF1" "$(echo "$R2" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ref"])')"

echo "== 5. 18:10 起 B 降额 60kW：18:10 已结束的预约不在影响清单 =="
SHORT=$(post /api/reservations "{\"vehicle_id\":2,\"site_id\":1,\"start_at\":\"$F:00:00\",\"duration_minutes\":10,\"min_power_kw\":60,\"idempotency_key\":\"e2e-short-aaaa-1\"}")
SHORT_ID=$(echo "$SHORT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
DERATE=$(post /api/operations/maintenance "{\"connector_id\":2,\"kind\":\"DERATE\",\"start_at\":\"$F:10:00\",\"planned_end_at\":\"2026-11-27T19:00:00\",\"effective_power_kw\":60,\"reason\":\"模块高温\",\"idempotency_key\":\"e2e-maint-aaaa-1\"}")
check "短单与降额无交集、未被撤销" "0" "$(echo "$DERATE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(sum(1 for i in d['impacts'] if i['reservation_id']==$SHORT_ID))")"
check "短单状态仍为 CONFIRMED" "CONFIRMED" "$(curl -s $BASE/api/reservations/$SHORT_ID | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 6. 降额后：18:15 的 90kW 被拒，60kW 仍可使用 B =="
check "90kW 请求被拒" "409" "$(code /api/reservations "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:15:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"idempotency_key\":\"e2e-deny-aaaa-1\"}")"
check "60kW 请求成功" "200" "$(code /api/reservations "{\"vehicle_id\":2,\"site_id\":1,\"start_at\":\"$F:15:00\",\"duration_minutes\":10,\"min_power_kw\":60,\"idempotency_key\":\"e2e-ok60-aaaa-1\"}")"
check "60kW 订单分配到 B 枪" "2" "$(python3 -c 'import json;print(json.load(open("/tmp/e2e_body.json"))["connector_id"])')"

echo "== 7. 池模式并发（18:30 后仅 A 满足 90kW，池内恰剩 1 名额）：一赢一拒且不指定枪号 =="
rm -f /tmp/p1.json /tmp/p2.json /tmp/p1.code /tmp/p2.code
curl -s -o /tmp/p1.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:30:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"POOL\",\"idempotency_key\":\"e2e-pool-aaaa-1\"}" > /tmp/p1.code &
curl -s -o /tmp/p2.json -w '%{http_code}' -X POST "$BASE/api/reservations" -H 'Content-Type: application/json' \
  -d "{\"vehicle_id\":4,\"site_id\":1,\"start_at\":\"$F:30:00\",\"duration_minutes\":10,\"min_power_kw\":90,\"mode\":\"POOL\",\"idempotency_key\":\"e2e-pool-bbbb-2\"}" > /tmp/p2.code &
wait
check "池并发结果码集合" "200,409," "$(sort /tmp/p1.code /tmp/p2.code | tr '\n' ',')"
check "池赢家暂不指定枪号" "None" "$(python3 -c '
import json
w=json.load(open("/tmp/p1.json")) if open("/tmp/p1.code").read()=="200" else json.load(open("/tmp/p2.json"))
print(w["connector_id"])')"

echo "== 8. 开始充电后故障：会话受保护，取消被拒，维护判定为 PROTECTED_CHARGING =="
CHG=$(post /api/reservations "{\"vehicle_id\":2,\"site_id\":1,\"start_at\":\"$F:40:00\",\"duration_minutes\":20,\"min_power_kw\":60,\"preferred_connector_id\":3,\"idempotency_key\":\"e2e-chg-aaaa-1\"}")
CID=$(echo "$CHG" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
check "充电前固定锁定 C 枪" "3" "$(echo "$CHG" | python3 -c 'import json,sys;print(json.load(sys.stdin)["connector_id"])')"
post "/api/reservations/$CID/arrive" '{"idempotency_key":"e2e-chg-bbbb-2"}' > /dev/null
post "/api/reservations/$CID/start" '{"idempotency_key":"e2e-chg-cccc-3"}' > /dev/null
IMP=$(post /api/operations/maintenance "{\"connector_id\":3,\"kind\":\"OUTAGE\",\"start_at\":\"$F:45:00\",\"planned_end_at\":\"$F:50:00\",\"reason\":\"枪口故障\",\"idempotency_key\":\"e2e-chg-dddd-4\"}")
check "充电单判定受保护" "PROTECTED_CHARGING" "$(echo "$IMP" | python3 -c "import json,sys;print(next(i['decision'] for i in json.load(sys.stdin)['impacts'] if i['reservation_id']==$CID))")"
check "充电中取消被拒" "409" "$(code "/api/reservations/$CID/cancel" '{"idempotency_key":"e2e-chg-eeee-5"}')"
check "订单状态仍是 CHARGING" "CHARGING" "$(curl -s $BASE/api/reservations/$CID | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

# ========== 候补（waitlist）专项：西站 id=2，枪 E=5(120kW)/F=6(60kW)，预约 id 2/3，候补 Q2=1/Q1=2 ==========
wl_pending() { curl -s "$BASE/api/waitlist/invites" | python3 -c 'import json,sys;print(json.dumps([i for i in json.load(sys.stdin) if i["status"]=="PENDING"]))'; }

echo "== 9. FIFO：先登记的 Q2(60kW) 拿走高功率 E，Q1(90kW) 在 F 上无合格时段 =="
$PY -m app.seed >/dev/null
check "西站当前为 FIFO" "FIFO" "$(curl -s $BASE/api/sites/2 | python3 -c 'import json,sys;print(json.load(sys.stdin)["match_policy"])')"
post /api/reservations/2/cancel '{"idempotency_key":"e2e-wl-fifo-e-1"}' > /dev/null
check "FIFO 首个邀请发给 Q2" "1" "$(wl_pending | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d)) if d and d[0]["waitlist_request_id"]==1 and d[0]["connector_id"]==5 else print(0)')"
post /api/reservations/3/cancel '{"idempotency_key":"e2e-wl-fifo-f-2"}' > /dev/null
check "FIFO 下 Q1 无新邀请（仍只 1 笔待响应）" "1" "$(wl_pending | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
check "Q1 仍在排队且无邀请" "QUEUED" "$(curl -s $BASE/api/waitlist/2 | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["status"]) if not d["invites"] else print("HAS_INVITE")')"

echo "== 10. 兼容优先：Q1 用 120kW 的 E、Q2 用 60kW 的 F，两人都能接单 =="
$PY -m app.seed >/dev/null
post /api/sites/2/policy '{"match_policy":"COMPATIBILITY","idempotency_key":"e2e-wl-pol-1"}' > /dev/null
post /api/reservations/2/cancel '{"idempotency_key":"e2e-wl-comp-e-1"}' > /dev/null
check "兼容优先首个邀请发给严格的 Q1 且锁定 E" "1" "$(wl_pending | python3 -c 'import json,sys;d=json.load(sys.stdin);print(1) if d and d[0]["waitlist_request_id"]==2 and d[0]["connector_id"]==5 else print(0)')"
post /api/reservations/3/cancel '{"idempotency_key":"e2e-wl-comp-f-2"}' > /dev/null
check "第二笔邀请发给 Q2 且锁定 F（两人都接单）" "OK" "$(wl_pending | python3 -c '
import json,sys
d=json.load(sys.stdin); m={i["waitlist_request_id"]:i["connector_id"] for i in d}
print("OK") if m.get(2)==5 and m.get(1)==6 else print("NO")')"

echo "== 11. 两名司机并发接受同一空档：只一笔确认 =="
$PY -m app.seed > /dev/null
post /api/reservations/2/cancel '{"idempotency_key":"e2e-wl-cc-rel-1"}' > /dev/null
INV=$(wl_pending | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
rm -f /tmp/w1.json /tmp/w2.json /tmp/w1.code /tmp/w2.code
curl -s -o /tmp/w1.json -w '%{http_code}' -X POST "$BASE/api/waitlist/invites/$INV/accept" -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"e2e-wl-cc-a-1"}' > /tmp/w1.code &
curl -s -o /tmp/w2.json -w '%{http_code}' -X POST "$BASE/api/waitlist/invites/$INV/accept" -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"e2e-wl-cc-b-2"}' > /tmp/w2.code &
wait
check "并发接受结果码集合" "200,409," "$(sort /tmp/w1.code /tmp/w2.code | tr '\n' ',')"
check "全局仅一笔候补成交预约" "1" "$(curl -s $BASE/api/reservations | python3 -c 'import json,sys;print(sum(1 for r in json.load(sys.stdin) if "WAITLIST_INVITE" in r["confirmation_snapshot"]))')"
check "邀请最终为 ACCEPTED" "ACCEPTED" "$(curl -s $BASE/api/waitlist/invites/$INV | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 12. 拒绝留痕 + 人工保留使邀请失效 =="
$PY -m app.seed > /dev/null
post /api/reservations/2/cancel '{"idempotency_key":"e2e-wl-dec-rel-1"}' > /dev/null
INV2=$(wl_pending | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
check "拒绝邀请" "DECLINED" "$(post /api/waitlist/invites/$INV2/decline '{"reason":"来不了","idempotency_key":"e2e-wl-dec-1"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
post /api/reservations/3/cancel '{"idempotency_key":"e2e-wl-hold-rel-2"}' > /dev/null
INV3=$(wl_pending | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
check "人工保留后邀请失效" "SUPERSEDED" "$(post /api/waitlist/invites/$INV3/hold '{"note":"站长保留","idempotency_key":"e2e-wl-hold-1"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
check "对失效邀请再接受被拒" "409" "$(code /api/waitlist/invites/$INV3/accept '{"idempotency_key":"e2e-wl-hold-acc-2"}')"

echo "== 13. 越过截止的迟到接受：超时落账，不能写成成功（短 TTL 服务器才实跑，否则跳过）=="
$PY -m app.seed > /dev/null
post /api/reservations/2/cancel '{"idempotency_key":"e2e-wl-exp-rel-1"}' > /dev/null
INV4=$(wl_pending | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
TTL=$(curl -s $BASE/api/waitlist/invites/$INV4 | python3 -c '
import json,sys,datetime
i=json.load(sys.stdin)
ex=datetime.datetime.fromisoformat(i["expires_at"]); cr=datetime.datetime.fromisoformat(i["created_at"])
print(int((ex-cr).total_seconds()))')
if [ "$TTL" -le 8 ]; then
  sleep $((TTL+1))
  check "巡检把邀请置为 EXPIRED" "EXPIRED" "$(post /api/waitlist/sweep '{"site_id":2,"idempotency_key":"e2e-wl-exp-sweep-1"}' >/dev/null; curl -s $BASE/api/waitlist/invites/$INV4 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
  check "迟到接受被拒（非成功确认）" "409" "$(code /api/waitlist/invites/$INV4/accept '{"idempotency_key":"e2e-wl-exp-acc-2"}')"
  check "没有任何候补成交预约" "0" "$(curl -s $BASE/api/reservations | python3 -c 'import json,sys;print(sum(1 for r in json.load(sys.stdin) if "WAITLIST_INVITE" in r["confirmation_snapshot"]))')"
else
  echo "  ⏭️  当前邀请 TTL=${TTL}s 较长，跳过实时超时（pytest 已用可控时钟覆盖该场景）"
fi

echo
echo "通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ]

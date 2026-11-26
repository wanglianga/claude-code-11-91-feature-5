<template>
  <div class="grid two">
    <!-- 左：维护操作 -->
    <section class="stack">
      <div class="panel">
        <h2>① 降额 / 停用维护窗口</h2>
        <label class="field">
          <span>枪口</span>
          <select v-model="mw.connector_id">
            <template v-for="s in sites" :key="s.id">
              <option v-for="c in s.connectors" :key="c.id" :value="c.id">
                {{ s.name }} · {{ c.code }}（{{ c.connector_type }} / {{ c.rated_power_kw }}kW{{ c.accessible ? ' / 无障碍' : '' }}）
              </option>
            </template>
          </select>
        </label>
        <div class="row">
          <label class="field">
            <span>类型</span>
            <select v-model="mw.kind">
              <option value="DERATE">降额（仍可用，功率下调）</option>
              <option value="OUTAGE">停用（窗口内不可用）</option>
            </select>
          </label>
          <label class="field" v-if="mw.kind === 'DERATE'">
            <span>降额后功率 kW</span>
            <input type="number" v-model.number="mw.effective_power_kw" />
          </label>
        </div>
        <div class="row">
          <label class="field">
            <span>生效开始</span>
            <input type="datetime-local" v-model="mw.start_at" />
          </label>
          <label class="field">
            <span>计划结束</span>
            <input type="datetime-local" v-model="mw.planned_end_at" />
          </label>
        </div>
        <label class="field">
          <span>原因</span>
          <input v-model="mw.reason" placeholder="如：模块高温降额 / 紧急检修" />
        </label>
        <button @click="openMaintenance">提交并评估受影响预约</button>

        <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px">
          {{ notice.text }}
        </div>

        <div v-if="lastImpact" style="margin-top:14px">
          <h3>本次维护影响判定（按真实时间交集）</h3>
          <p class="muted" v-if="!lastImpact.impacts.length">
            没有与维护时段重叠的在途预约——已结束的预约不会被撤销。
          </p>
          <table v-else>
            <thead><tr><th>订单</th><th>交集</th><th>判定</th><th>原因</th></tr></thead>
            <tbody>
              <tr v-for="i in lastImpact.impacts" :key="i.reservation_id">
                <td class="mono">{{ i.ref }}<br />
                  <span class="badge info">{{ MODE_LABELS[i.mode] }}</span></td>
                <td class="mono">{{ fmt(i.overlap_start) }}–{{ fmt(i.overlap_end) }}</td>
                <td><span :class="['badge', impactBadge(i.decision)]">{{ IMPACT_LABELS[i.decision] }}</span></td>
                <td class="muted" style="max-width:320px">{{ i.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h2>② 恢复设备（不改写历史确认）</h2>
        <table v-if="activeWindows.length">
          <thead><tr><th>#</th><th>枪口</th><th>窗口</th><th>恢复时刻</th><th></th></tr></thead>
          <tbody>
            <tr v-for="w in activeWindows" :key="w.id">
              <td>{{ w.id }}</td>
              <td>{{ gunCode(w.connector_id) }} · {{ w.kind === 'DERATE' ? `降额 ${w.effective_power_kw}kW` : '停用' }}</td>
              <td class="mono">{{ fmt(w.start_at) }}–{{ fmt(w.planned_end_at) }}</td>
              <td><input type="datetime-local" v-model="recoverAt[w.id]" /></td>
              <td><button class="small secondary" @click="recover(w)">恢复</button></td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">当前没有生效中的维护窗口。</p>
      </div>
    </section>

    <!-- 右：预约与客服动作 -->
    <section class="stack">
      <div class="panel">
        <h2>③ 在途预约与维护状态</h2>
        <table>
          <thead>
            <tr><th>订单</th><th>状态</th><th>时间窗</th><th>枪/池</th><th>事件流</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in reservations" :key="r.id">
              <td class="mono">{{ r.ref }}<br /><span class="muted">≥{{ r.min_power_kw }}kW</span></td>
              <td><span :class="['badge', statusBadge(r.status)]">{{ STATUS_LABELS[r.status] }}</span></td>
              <td class="mono">{{ fmt(r.start_at) }}–{{ fmt(r.end_at) }}</td>
              <td>
                <span v-if="r.connector_id">{{ gunCode(r.connector_id) }}</span>
                <span v-else class="muted">池名额</span>
              </td>
              <td>
                <details>
                  <summary>{{ r.events.length }} 条</summary>
                  <ul class="timeline">
                    <li v-for="e in r.events" :key="e.id">
                      <strong>{{ e.event_type }}</strong>
                      <pre class="mono" style="white-space:pre-wrap">{{ pretty(e.detail) }}</pre>
                    </li>
                  </ul>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h2>④ 客服处置（指向当时订单与设备状态）</h2>
        <div class="row">
          <label class="field">
            <span>预约</span>
            <select v-model="svc.reservation_id">
              <option v-for="r in reservations" :key="r.id" :value="r.id">
                {{ r.ref }} — {{ STATUS_LABELS[r.status] }}
              </option>
            </select>
          </label>
          <label class="field">
            <span>动作类型</span>
            <select v-model="svc.kind">
              <option value="ALTERNATIVE_CONNECTOR">替代枪口</option>
              <option value="FEE_ADJUSTMENT">费用调整（负=补偿）</option>
              <option value="NOTE">备注</option>
            </select>
          </label>
        </div>
        <div class="row">
          <label class="field" v-if="svc.kind === 'ALTERNATIVE_CONNECTOR'">
            <span>目标枪口</span>
            <select v-model="svc.target_connector_id">
              <template v-for="s in sites" :key="s.id">
                <option v-for="c in s.connectors" :key="c.id" :value="c.id">
                  {{ c.code }}（{{ c.connector_type }} / {{ c.rated_power_kw }}kW{{ c.accessible ? ' / 无障碍' : '' }}）
                </option>
              </template>
            </select>
          </label>
          <label class="field" v-if="svc.kind === 'FEE_ADJUSTMENT'">
            <span>金额</span>
            <input type="number" step="0.01" v-model.number="svc.amount" />
          </label>
        </div>
        <label class="field">
          <span>说明</span>
          <input v-model="svc.note" />
        </label>
        <button @click="submitService">提交客服动作</button>
        <p class="muted">
          动作会固化当时的订单状态与枪口维护快照；充电中的会话不能迁移，恢复设备也不会回滚历史。
        </p>
      </div>

      <div class="panel">
        <h2>⑤ 维护窗口台账</h2>
        <table>
          <thead><tr><th>#</th><th>枪口</th><th>类型</th><th>计划窗口</th><th>实际恢复</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="w in windows" :key="w.id">
              <td>{{ w.id }}</td>
              <td>{{ gunCode(w.connector_id) }}</td>
              <td>{{ w.kind === 'DERATE' ? `降额 ${w.effective_power_kw}kW` : '停用' }}</td>
              <td class="mono">{{ fmt(w.start_at) }}–{{ fmt(w.planned_end_at) }}</td>
              <td class="mono">{{ w.ended_at ? fmt(w.ended_at) : '—' }}</td>
              <td class="muted">{{ w.reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h2>⑥ 候补空档分配规则</h2>
        <div class="row">
          <label class="field">
            <span>站点</span>
            <select v-model="policy.site_id" @change="onPolicySite">
              <option v-for="s in sites" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
          </label>
          <label class="field">
            <span>规则</span>
            <select v-model="policy.match_policy">
              <option value="FIFO">严格先后（FIFO，公平易解释）</option>
              <option value="COMPATIBILITY">兼容优先（稀缺能力优先，提高利用率）</option>
            </select>
          </label>
        </div>
        <button @click="savePolicy">保存规则</button>
        <button class="secondary" @click="runSweep">立即巡检超时</button>
        <p class="muted">
          两条规则都只向「能力匹配且能覆盖完整时长、不晚于到站期限」的候补发邀请；
          巡检会把过截止时刻的邀请置超时、把超到站期限的候补关闭，全部写事件流。
        </p>
      </div>

      <div class="panel">
        <h2>⑦ 候补队列（按登记顺位）</h2>
        <table v-if="waitlist.length">
          <thead><tr><th>候补号</th><th>登记时间</th><th>车辆/需求</th><th>站点/替代</th><th>到站期限</th><th>状态</th><th>事件</th></tr></thead>
          <tbody>
            <tr v-for="w in waitlist" :key="w.id">
              <td class="mono">{{ w.ref }}</td>
              <td class="mono">{{ fmt(w.created_at) }}</td>
              <td>V{{ w.vehicle_id }}<br /><span class="muted">≥{{ w.min_power_kw }}kW / {{ w.duration_minutes }}分</span></td>
              <td>{{ siteName(w.site_id) }}<br /><span class="muted">{{ altText(w) }}</span></td>
              <td class="mono">{{ fmt(w.arrive_by) }}</td>
              <td><span :class="['badge', wlBadge(w.status)]">{{ WAITLIST_LABELS[w.status] }}</span></td>
              <td>
                <details>
                  <summary>{{ w.events.length }} 条</summary>
                  <ul class="timeline">
                    <li v-for="e in w.events" :key="e.id">
                      <strong>{{ e.event_type }}</strong>
                      <pre class="mono" style="white-space:pre-wrap">{{ pretty(e.detail) }}</pre>
                    </li>
                  </ul>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">暂无候补请求。</p>
      </div>

      <div class="panel">
        <h2>⑧ 空档邀请台账与人工保留</h2>
        <table v-if="invites.length">
          <thead><tr><th>#</th><th>候补</th><th>站点/枪</th><th>空档</th><th>截止时刻</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="i in invites" :key="i.id">
              <td>{{ i.id }}</td>
              <td class="mono">{{ wlRef(i.waitlist_request_id) }}</td>
              <td>{{ siteName(i.site_id) }} · {{ gunCode(i.connector_id) }}（{{ i.offered_power_kw }}kW）</td>
              <td class="mono">{{ fmt(i.slot_start) }}–{{ fmt(i.slot_end) }}</td>
              <td class="mono">{{ fmt(i.expires_at) }}</td>
              <td><span :class="['badge', inviteBadge(i.status)]">{{ INVITE_LABELS[i.status] }}</span></td>
              <td>
                <button v-if="i.status === 'PENDING'" class="small ghost" @click="hold(i)">人工保留</button>
                <span v-else class="muted">已了结</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">暂无邀请。取消一笔未开始的预约即会触发匹配。</p>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, computed } from 'vue'
import {
  api, IMPACT_LABELS, INVITE_LABELS, MODE_LABELS, newIdempotencyKey,
  STATUS_LABELS, WAITLIST_LABELS,
} from '../api.js'

const sites = ref([])
const reservations = ref([])
const windows = ref([])
const waitlist = ref([])
const invites = ref([])
const lastImpact = ref(null)
const notice = ref(null)
const recoverAt = reactive({})

const mw = reactive({
  connector_id: 2,
  kind: 'DERATE',
  start_at: '2026-11-27T18:10',
  planned_end_at: '2026-11-27T19:00',
  effective_power_kw: 60,
  reason: '模块高温降额',
})
const svc = reactive({
  reservation_id: null,
  kind: 'ALTERNATIVE_CONNECTOR',
  target_connector_id: 3,
  amount: -20,
  note: '',
})
const policy = reactive({ site_id: 2, match_policy: 'FIFO' })

const activeWindows = computed(() => windows.value.filter((w) => !w.ended_at))

onMounted(loadAll)

async function loadAll() {
  sites.value = await api.get('/api/sites')
  reservations.value = await api.get('/api/reservations')
  windows.value = await api.get('/api/maintenance')
  waitlist.value = await api.get('/api/waitlist')
  invites.value = await api.get('/api/waitlist/invites')
  for (const w of windows.value) {
    if (!recoverAt[w.id]) recoverAt[w.id] = w.planned_end_at.slice(0, 16)
  }
  if (!svc.reservation_id && reservations.value.length) {
    svc.reservation_id = reservations.value[0].id
  }
  onPolicySite()
}

function onPolicySite() {
  const s = sites.value.find((x) => x.id === policy.site_id)
  if (s) policy.match_policy = s.match_policy
}

async function savePolicy() {
  notice.value = null
  try {
    const r = await api.post(`/api/sites/${policy.site_id}/policy`, {
      match_policy: policy.match_policy,
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'ok', text: `${r.name} 分配规则已切换为：${r.match_policy}` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function runSweep() {
  notice.value = null
  try {
    const r = await api.post('/api/waitlist/sweep', {
      site_id: null, idempotency_key: newIdempotencyKey(),
    })
    notice.value = {
      kind: 'info',
      text: `巡检完成：超时邀请 ${r.expired_invites} 笔，关闭过期候补 ${r.closed_requests} 条`,
    }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function hold(inv) {
  notice.value = null
  try {
    await api.post(`/api/waitlist/invites/${inv.id}/hold`, {
      note: '运营人工保留', idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'info', text: `邀请 #${inv.id} 已人工保留并失效，候补回到队列` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function openMaintenance() {
  notice.value = null
  try {
    const body = {
      connector_id: mw.connector_id,
      kind: mw.kind,
      start_at: mw.start_at + ':00',
      planned_end_at: mw.planned_end_at + ':00',
      effective_power_kw: mw.kind === 'DERATE' ? mw.effective_power_kw : null,
      reason: mw.reason,
      idempotency_key: newIdempotencyKey(),
    }
    lastImpact.value = await api.post('/api/operations/maintenance', body)
    notice.value = { kind: 'ok', text: `维护窗口 #${lastImpact.value.maintenance.id} 已生效，影响判定见下表` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function recover(w) {
  notice.value = null
  try {
    await api.post(`/api/operations/maintenance/${w.id}/recover`, {
      ended_at: (recoverAt[w.id] || w.planned_end_at.slice(0, 16)) + ':00',
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'info', text: `窗口 #${w.id} 已恢复；历史确认快照与订单状态不会被自动改写` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function submitService() {
  notice.value = null
  try {
    const body = {
      reservation_id: svc.reservation_id,
      kind: svc.kind,
      target_connector_id: svc.kind === 'ALTERNATIVE_CONNECTOR' ? svc.target_connector_id : null,
      amount: svc.kind === 'FEE_ADJUSTMENT' ? svc.amount : null,
      note: svc.note,
      idempotency_key: newIdempotencyKey(),
    }
    const a = await api.post('/api/operations/service-actions', body)
    notice.value = { kind: 'ok', text: `客服动作 #${a.id} 已记录并固化当时设备状态` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

function gunCode(id) {
  for (const s of sites.value) {
    const c = s.connectors.find((x) => x.id === id)
    if (c) return c.code
  }
  return '#' + id
}
function fmt(t) { return t ? t.replace('T', ' ').slice(5, 16) : '' }
function pretty(s) {
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s }
}
function statusBadge(s) {
  return {
    CONFIRMED: 'ok', ARRIVED: 'info', CHARGING: 'warn', COMPLETED: 'muted',
    CANCELLED: 'muted', NO_SHOW: 'muted',
    NEEDS_RESELECTION: 'bad', MANUAL_CONTACT: 'bad',
  }[s] || 'muted'
}
function impactBadge(d) {
  return {
    UNCHANGED: 'ok', NEEDS_RESELECTION: 'bad',
    MANUAL_CONTACT: 'warn', PROTECTED_CHARGING: 'warn',
  }[d] || 'muted'
}
function siteName(id) { const s = sites.value.find((x) => x.id === id); return s ? s.name : '#' + id }
function altText(w) {
  try {
    const ids = JSON.parse(w.acceptable_site_ids || '[]')
    return ids.length ? '可替代：' + ids.map(siteName).join('、') : '仅首选站'
  } catch { return '仅首选站' }
}
function wlRef(id) { const w = waitlist.value.find((x) => x.id === id); return w ? w.ref : '#' + id }
function wlBadge(s) {
  return { QUEUED: 'info', INVITED: 'warn', CONFIRMED: 'ok', DECLINED: 'muted',
    EXPIRED: 'muted', CANCELLED: 'muted', CLOSED: 'bad' }[s] || 'muted'
}
function inviteBadge(s) {
  return { PENDING: 'warn', ACCEPTED: 'ok', DECLINED: 'muted', EXPIRED: 'bad', SUPERSEDED: 'muted' }[s] || 'muted'
}
</script>

<template>
  <div class="grid two">
    <!-- 左：候补登记 -->
    <section class="stack">
      <div class="panel">
        <h2>① 登记候补请求</h2>
        <p class="muted" style="margin-top:0">
          预约不到合适空档时可登记候补；司机临时取消释放未开始时段后，系统按站点规则
          发放<strong>带截止时刻的一次性邀请</strong>。邀请不是预约，须在倒计时内接受才会确认。
        </p>
        <label class="field">
          <span>车辆（接口 / 最大受电 / 无障碍）</span>
          <select v-model="form.vehicle_id" @change="onVehicleChange">
            <option v-for="v in vehicles" :key="v.id" :value="v.id">
              {{ v.name }} — {{ v.connector_type }} / {{ v.max_power_kw }}kW{{ v.accessible ? ' / 无障碍' : '' }}
            </option>
          </select>
        </label>
        <label class="field">
          <span>首选站点</span>
          <select v-model="form.site_id">
            <option v-for="s in sites" :key="s.id" :value="s.id">
              {{ s.name }} — {{ s.location }}
            </option>
          </select>
        </label>
        <div class="row">
          <label class="field">
            <span>最低功率 kW</span>
            <input type="number" min="1" v-model.number="form.min_power_kw" />
          </label>
          <label class="field">
            <span>所需时长（分钟）</span>
            <input type="number" min="5" v-model.number="form.duration_minutes" />
          </label>
        </div>
        <label class="field">
          <span>到站期限（只接受不晚于该时刻开始的空档）</span>
          <input type="datetime-local" v-model="form.arrive_by" />
        </label>
        <label class="field">
          <span>可接受的替代站点（可多选，不含首选站）</span>
          <div class="chip-row">
            <label v-for="s in alternativeSites" :key="s.id" class="seg" style="cursor:pointer">
              <input type="checkbox" style="width:auto;margin-right:6px"
                     :value="s.id" v-model="form.acceptable_site_ids" />
              {{ s.name }}
            </label>
            <span v-if="!alternativeSites.length" class="muted">暂无其他站点</span>
          </div>
        </label>
        <button @click="submit">登记候补</button>
        <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px">
          {{ notice.text }}
        </div>
      </div>

      <div class="panel">
        <h2>② 当前站点分配规则</h2>
        <p class="muted" style="margin-top:0">
          两条规则都只会向<strong>车辆能力匹配且能覆盖完整时长</strong>的请求发邀请。
        </p>
        <ul class="timeline">
          <li><strong>严格先后（FIFO）</strong>：按登记时间发邀请，公平易解释；早到的宽松需求可能占走稀缺高功率枪。</li>
          <li><strong>兼容优先</strong>：先安排只有少数枪能满足的严格请求，同等候选再按排队时间，资源利用更高，但较早登记者可能后移。</li>
        </ul>
      </div>
    </section>

    <!-- 右：候补队列与邀请 -->
    <section class="stack">
      <div class="panel">
        <h2>③ 候补队列与我的邀请
          <button class="ghost small" style="float:right" @click="loadAll">刷新</button>
        </h2>
        <table v-if="waitlist.length">
          <thead>
            <tr><th>候补号</th><th>车辆/需求</th><th>站点</th><th>到站期限</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="w in waitlist" :key="w.id">
              <td>
                <strong class="mono">{{ w.ref }}</strong><br />
                <span class="muted mono">{{ fmt(w.created_at) }} 登记</span>
              </td>
              <td>
                V{{ w.vehicle_id }}<br />
                <span class="muted">≥{{ w.min_power_kw }}kW / {{ w.duration_minutes }}分钟</span>
              </td>
              <td>{{ siteName(w.site_id) }}<br />
                <span v-if="altSitesOf(w).length" class="muted">可替代：{{ altSitesOf(w).map(siteName).join('、') }}</span>
              </td>
              <td class="mono">{{ fmt(w.arrive_by) }}</td>
              <td><span :class="['badge', wlBadge(w.status)]">{{ WAITLIST_LABELS[w.status] }}</span></td>
              <td>
                <button v-if="!terminal(w.status)" class="small ghost"
                        @click="cancelWaitlist(w)">撤销候补</button>
                <span v-if="w.reservation_id" class="badge ok">已生成预约 #{{ w.reservation_id }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">暂无候补请求。</p>
      </div>

      <div class="panel">
        <h2>④ 一次性空档邀请（邀请 ≠ 预约）</h2>
        <div v-for="inv in invites" :key="inv.id" :class="['candidate', inv.status === 'PENDING' ? 'eligible' : '']">
          <div class="head">
            <span class="code">邀请 #{{ inv.id }} · {{ siteName(inv.site_id) }} 枪口 {{ gunCode(inv.connector_id) }}</span>
            <span :class="['badge', inviteBadge(inv.status)]">{{ INVITE_LABELS[inv.status] }}</span>
          </div>
          <div class="kv">
            <span class="k">空档时段</span>
            <span class="mono">{{ fmt(inv.slot_start) }}–{{ fmt(inv.slot_end) }}（{{ inv.offered_power_kw }}kW）</span>
            <span class="k">截止时刻</span>
            <span class="mono">{{ fmt(inv.expires_at) }}
              <strong v-if="inv.status === 'PENDING'" :style="{ color: remainMs(inv) <= 0 ? 'var(--bad)' : 'var(--warn)' }">
                剩余 {{ countdown(inv) }}
              </strong>
            </span>
          </div>
          <div class="chip-row" v-if="inv.status === 'PENDING'">
            <button class="small" @click="accept(inv)">接受并确认（容量即时锁定）</button>
            <button class="small secondary" @click="decline(inv)">拒绝（继续排队）</button>
          </div>
          <p v-if="inv.status === 'PENDING'" class="muted" style="margin-bottom:0">
            倒计时结束后到达的接受会被拒绝，不能写成成功；接受后才生成 CONFIRMED 预约。
          </p>
        </div>
        <p v-if="!invites.length" class="muted">暂无邀请。取消一笔未开始的预约释放空档后，这里会出现带截止时刻的邀请。</p>
      </div>

      <div class="panel">
        <h2>⑤ 候补事件流（全部留痕）</h2>
        <details v-for="w in waitlist" :key="w.id">
          <summary class="mono">{{ w.ref }}（{{ WAITLIST_LABELS[w.status] }}，{{ w.events.length }} 条）</summary>
          <ul class="timeline">
            <li v-for="e in w.events" :key="e.id">
              <strong>{{ e.event_type }}</strong>
              <div class="muted mono">{{ fmt(e.created_at) }}</div>
              <pre class="mono" style="white-space:pre-wrap;margin:2px 0 0">{{ pretty(e.detail) }}</pre>
            </li>
          </ul>
        </details>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, INVITE_LABELS, newIdempotencyKey, WAITLIST_LABELS } from '../api.js'

const vehicles = ref([])
const sites = ref([])
const waitlist = ref([])
const invites = ref([])
const notice = ref(null)
const nowTs = ref(Date.now())
let timer = null

const form = reactive({
  vehicle_id: 5,
  site_id: 2,
  min_power_kw: 60,
  duration_minutes: 30,
  arrive_by: '2026-11-27T18:00',
  acceptable_site_ids: [],
})

const alternativeSites = computed(() => sites.value.filter((s) => s.id !== form.site_id))

onMounted(async () => {
  await loadAll()
  timer = setInterval(() => { nowTs.value = Date.now() }, 1000)
})
onUnmounted(() => timer && clearInterval(timer))

async function loadAll() {
  vehicles.value = await api.get('/api/vehicles')
  sites.value = await api.get('/api/sites')
  waitlist.value = await api.get('/api/waitlist')
  invites.value = await api.get('/api/waitlist/invites')
}

function onVehicleChange() {
  const v = vehicles.value.find((x) => x.id === form.vehicle_id)
  if (v && Number(form.min_power_kw) > Number(v.max_power_kw)) {
    form.min_power_kw = Number(v.max_power_kw)
  }
}

async function submit() {
  notice.value = null
  try {
    const r = await api.post('/api/waitlist', {
      vehicle_id: form.vehicle_id,
      site_id: form.site_id,
      min_power_kw: form.min_power_kw,
      duration_minutes: form.duration_minutes,
      arrive_by: form.arrive_by + ':00',
      acceptable_site_ids: form.acceptable_site_ids,
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'ok', text: `候补 ${r.ref} 已登记，按 created_at 进入队列` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: '登记被拒绝：' + e.message }
  }
}

async function accept(inv) {
  notice.value = null
  try {
    const r = await api.post(`/api/waitlist/invites/${inv.id}/accept`, {
      idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'ok', text: `已确认预约 ${r.reservation.ref}，锁定枪口 ${gunCode(r.reservation.connector_id)}` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: '接受失败：' + e.message }
    await loadAll()
  }
}

async function decline(inv) {
  notice.value = null
  try {
    await api.post(`/api/waitlist/invites/${inv.id}/decline`, {
      reason: '司机主动拒绝', idempotency_key: newIdempotencyKey(),
    })
    notice.value = { kind: 'info', text: `邀请 #${inv.id} 已拒绝，候补保持原顺位继续排队` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

async function cancelWaitlist(w) {
  notice.value = null
  try {
    await api.post(`/api/waitlist/${w.id}/cancel`, { idempotency_key: newIdempotencyKey() })
    notice.value = { kind: 'info', text: `候补 ${w.ref} 已撤销，其待响应邀请同步失效` }
    await loadAll()
  } catch (e) {
    notice.value = { kind: 'bad', text: e.message }
  }
}

function remainMs(inv) {
  return new Date(inv.expires_at).getTime() - nowTs.value
}
function countdown(inv) {
  const ms = Math.max(0, remainMs(inv))
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}分${String(s % 60).padStart(2, '0')}秒`
}
function terminal(s) { return ['CONFIRMED', 'CANCELLED', 'CLOSED'].includes(s) }
function altSitesOf(w) {
  try { return (JSON.parse(w.acceptable_site_ids) || []).map((id) => sites.value.find((s) => s.id === id)).filter(Boolean) }
  catch { return [] }
}
function siteName(id) { const s = sites.value.find((x) => x.id === id); return s ? s.name : '#' + id }
function gunCode(id) {
  for (const s of sites.value) {
    const c = s.connectors.find((x) => x.id === id)
    if (c) return c.code
  }
  return '#' + id
}
function fmt(t) { return t ? t.replace('T', ' ').slice(5, 16) : '' }
function pretty(s) { try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s } }
function wlBadge(s) {
  return { QUEUED: 'info', INVITED: 'warn', CONFIRMED: 'ok', DECLINED: 'muted',
    EXPIRED: 'muted', CANCELLED: 'muted', CLOSED: 'bad' }[s] || 'muted'
}
function inviteBadge(s) {
  return { PENDING: 'warn', ACCEPTED: 'ok', DECLINED: 'muted', EXPIRED: 'bad', SUPERSEDED: 'muted' }[s] || 'muted'
}
</script>

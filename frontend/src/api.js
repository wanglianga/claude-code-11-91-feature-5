async function request(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    // FastAPI 的 detail 可能是字符串或结构化对象
    const detail = data.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || JSON.stringify(detail || data)
    const err = new Error(message)
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
}

export function newIdempotencyKey() {
  // 前端生成可读幂等键；重复提交时复用同一个键即可回放原结果
  return 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8)
}

export const STATUS_LABELS = {
  CONFIRMED: '已确认',
  ARRIVED: '已到站',
  CHARGING: '充电中（受保护）',
  COMPLETED: '已完成',
  CANCELLED: '已取消（已释放）',
  NO_SHOW: '迟到失约（已释放）',
  NEEDS_RESELECTION: '需改选',
  MANUAL_CONTACT: '需人工联系',
}

export const MODE_LABELS = {
  CONNECTOR: '固定枪口',
  POOL: '资源池',
}

export const IMPACT_LABELS = {
  UNCHANGED: '继续履约',
  NEEDS_RESELECTION: '进入改选',
  MANUAL_CONTACT: '人工联系',
  PROTECTED_CHARGING: '充电中受保护',
}

export const WAITLIST_LABELS = {
  QUEUED: '排队中',
  INVITED: '已发邀请（待响应）',
  CONFIRMED: '已成交确认',
  DECLINED: '已拒绝（仍排队）',
  EXPIRED: '邀请超时（仍排队）',
  CANCELLED: '已撤销',
  CLOSED: '已关闭（超到站期限）',
}

export const INVITE_LABELS = {
  PENDING: '待响应',
  ACCEPTED: '已接受并确认',
  DECLINED: '已拒绝',
  EXPIRED: '已超时',
  SUPERSEDED: '已失效（保留/撤销）',
}

export const POLICY_LABELS = {
  FIFO: '严格先后（FIFO）',
  COMPATIBILITY: '兼容优先（稀缺能力优先）',
}

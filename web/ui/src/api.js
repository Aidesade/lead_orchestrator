const j = async (r) => {
  if (!r.ok) {
    let detail = r.statusText
    try {
      const body = await r.json()
      detail = body.detail ?? detail
    } catch { /* тело не json — оставляем statusText */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return r.json()
}

const qs = (params) => {
  const p = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined && v !== 0) p.set(k, v)
  })
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  health: () => fetch('/api/health').then(j),
  industries: () => fetch('/api/industries').then(j),
  regions: () => fetch('/api/regions').then(j),
  models: () => fetch('/api/models').then(j),
  rusprofile: () => fetch('/api/rusprofile').then(j),
  checkRusprofile: () => fetch('/api/rusprofile/check', { method: 'POST' }).then(j),

  leads: (f) => fetch(`/api/leads${qs(f)}`).then(j),
  facets: () => fetch('/api/leads/facets').then(j),
  lead: (inn) => fetch(`/api/leads/${encodeURIComponent(inn)}`).then(j),
  exportUrl: (f) => `/api/leads/export.csv${qs(f)}`,

  runs: () => fetch('/api/runs').then(j),
  run: (id) => fetch(`/api/runs/${id}`).then(j),
  startRun: (params) =>
    fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }).then(j),
  cancelRun: (id) => fetch(`/api/runs/${id}/cancel`, { method: 'POST' }).then(j),
}

/** Подписка на события прогона. Возвращает функцию отписки.
 *  after=<seq> — догон пропущенного: перезагрузка страницы не теряет прогресс. */
export function subscribeRun(id, after, onEvent, onDone) {
  const es = new EventSource(`/api/runs/${id}/events?after=${after || 0}`)
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data)
    if (ev.type === '_eof') {
      es.close()
      onDone?.()
      return
    }
    onEvent(ev)
  }
  es.onerror = () => es.close()
  return () => es.close()
}

export const fmtMoney = (n) =>
  n ? `$${Number(n).toFixed(2)}` : '—'

export const fmtRevenue = (v) => {
  if (!v) return '—'
  if (v >= 1e12) return `${(v / 1e12).toFixed(2)} трлн ₽`
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)} млрд ₽`
  if (v >= 1e6) return `${(v / 1e6).toFixed(0)} млн ₽`
  return `${v} ₽`
}

export const fmtTime = (t) =>
  t ? new Date(t * 1000).toLocaleString('ru-RU') : '—'

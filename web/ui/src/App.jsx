import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api.js'
import Runs from './pages/Runs.jsx'
import RunDetail from './pages/RunDetail.jsx'
import Leads from './pages/Leads.jsx'
import LeadCard from './pages/LeadCard.jsx'

/** Статус платной сессии RusProfile. Без неё фаза 1 падает сразу (orchestrator.py:395-396).
 *
 *  Проверка НЕ автоматическая: она поднимает настоящий Chrome на том же профиле, что и сбор,
 *  поэтому дёргается только по клику — и блокируется, пока идёт прогон. */
function RusProfileBadge() {
  const [st, setSt] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { api.rusprofile().then(setSt).catch(() => {}) }, [])

  const check = async () => {
    setBusy(true)
    try { setSt(await api.checkRusprofile()) } catch { setSt({ ok: false, reason: 'нет ответа' }) }
    finally { setBusy(false) }
  }

  const cls = busy ? '' : st?.ok ? 'ok' : st?.ok === false ? 'err' : ''
  const label = busy ? 'проверяю (Chrome)…'
    : st?.ok ? 'сессия жива'
    : st?.ok === false ? 'нужен вход'
    : 'не проверено'

  return (
    <span
      className={`badge ${cls}`}
      style={{ cursor: busy ? 'progress' : 'pointer' }}
      onClick={busy ? undefined : check}
      title={[
        st?.reason, st?.hint,
        'Клик — проверить (откроет Chrome на ~1 мин). Во время прогона проверка запрещена: ' +
        'она заняла бы тот же профиль Chrome, что и сбор.',
      ].filter(Boolean).join('\n')}
    >
      <i className={`dot ${busy ? 'pulse' : ''}`} />
      RusProfile: {label}
    </span>
  )
}

export default function App() {
  const [health, setHealth] = useState(null)
  useEffect(() => { api.health().then(setHealth).catch(() => {}) }, [])

  return (
    <>
      <nav className="nav">
        <span className="brand">Лидген</span>
        <NavLink to="/runs">Прогоны</NavLink>
        <NavLink to="/leads">Лиды</NavLink>
        <span className="spacer" />
        <RusProfileBadge />
        {health && (
          <span className="badge mono" title={`оркестратор: ${health.orchestrator}`}>
            {health.disk_base}
          </span>
        )}
      </nav>
      <div className="wrap">
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/leads" element={<Leads />} />
          <Route path="/leads/:inn" element={<LeadCard />} />
        </Routes>
      </div>
    </>
  )
}

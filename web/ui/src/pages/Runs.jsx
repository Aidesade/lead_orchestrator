import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, fmtMoney, fmtTime } from '../api.js'
import RegionPicker from '../components/RegionPicker.jsx'

const STATUS = {
  running: ['run', 'идёт'],
  done: ['ok', 'готово'],
  failed: ['err', 'сбой'],
  cancelled: ['warn', 'отменён'],
  interrupted: ['warn', 'прерван'],
}

export function RunStatus({ status }) {
  const [cls, label] = STATUS[status] || ['', status]
  return (
    <span className={`badge ${cls}`}>
      <i className={`dot ${status === 'running' ? 'pulse' : ''}`} />
      {label}
    </span>
  )
}

const EMPTY = {
  industries: [], per_industry: 10, min_revenue: 1e9,
  regions: [], exclude_regions: [],
  model: 'kimi', workers: 2, show_browser: false, redo: false,
}

export default function Runs() {
  const nav = useNavigate()
  const [inds, setInds] = useState([])
  const [regions, setRegions] = useState([])
  const [models, setModels] = useState([])
  const [runs, setRuns] = useState([])
  const [f, setF] = useState(EMPTY)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const load = () => api.runs().then(setRuns).catch(() => {})
  useEffect(() => {
    api.industries().then(setInds).catch((e) => setErr(String(e.message)))
    api.regions().then(setRegions).catch(() => {})
    api.models().then((m) => {
      setModels(m)
      const def = m.find((x) => x.default)
      if (def) setF((s) => ({ ...s, model: def.id }))
    }).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [])

  const set = (k) => (e) => {
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.value
    setF((s) => ({ ...s, [k]: v }))
  }

  const toggleInd = (key) =>
    setF((s) => ({
      ...s,
      industries: s.industries.includes(key)
        ? s.industries.filter((i) => i !== key)
        : [...s.industries, key],
    }))

  // Имя отрасли по ключу — и для чипов, и для журнала прогонов ниже.
  const indLabel = useMemo(
    () => Object.fromEntries(inds.map((i) => [i.key, i.label])), [inds])

  // Объём = N на КАЖДУЮ отрасль (--per-industry). Через --count оркестратор делит
  // ceil(count/K), и «10 по трём отраслям» тихо превращается в 4 на отрасль.
  const total = f.industries.length * Number(f.per_industry || 0)
  const modelInfo = models.find((m) => m.id === f.model)
  const byProvider = modelInfo?.billing === 'provider'    // Kimi: цену шлюз наружу не отдаёт
  const hours = total ? ((total * 14) / Math.max(1, Number(f.workers))) / 60 : 0
  const heavy = total > 20

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    if (!f.industries.length) {
      setErr('Выберите хотя бы одну отрасль')
      return
    }
    const money = byProvider
      ? 'Стоимость считает провайдер Kimi (оркестратор её не видит).'
      : `Ориентировочно $${total}–$${total * 2}.`
    if (heavy && !confirm(
      `Прогон на ${total} компаний: ~${hours.toFixed(1)} ч. ${money}\nЗапускаем?`)) return

    setBusy(true)
    try {
      const run = await api.startRun({
        ...f,
        per_industry: Number(f.per_industry),
        workers: Number(f.workers),
        min_revenue: Number(f.min_revenue),
      })
      nav(`/runs/${run.id}`)
    } catch (e2) {
      setErr(String(e2.message))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <form className="card" onSubmit={submit}>
        <h2>Новый прогон</h2>
        {err && <div className="err-box">{err}</div>}

        <h3>Отрасли</h3>
        <div className="chips">
          {inds.map((i) => (
            <button
              type="button" key={i.key}
              onClick={() => toggleInd(i.key)}
              className={f.industries.includes(i.key) ? '' : 'ghost'}
              style={{ padding: '5px 11px', fontWeight: 500 }}
              title={i.pain || ''}
            >
              {i.label}
            </button>
          ))}
          {!inds.length && <span className="muted">каталог отраслей не загрузился</span>}
        </div>

        <h3>Объём и фильтры</h3>
        <div className="row">
          <div className="field" style={{ width: 150 }}>
            <label>Компаний на отрасль</label>
            <input type="number" min="1" value={f.per_industry} onChange={set('per_industry')} />
          </div>
          <div className="field" style={{ width: 170 }}>
            <label>Выручка от, ₽</label>
            <input type="number" step="1e8" value={f.min_revenue} onChange={set('min_revenue')} />
          </div>
          <div className="field" style={{ width: 200 }}>
            <label>Модель (писатель .docx)</label>
            <select value={f.model} onChange={set('model')}>
              {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
          </div>
          <div className="field" style={{ width: 110 }}>
            <label>Воркеров</label>
            <input type="number" min="1" max="4" value={f.workers} onChange={set('workers')} />
          </div>
        </div>

        <div className="row" style={{ marginTop: 14 }}>
          <div className="field" style={{ flex: 1, minWidth: 260 }}>
            <label>Регионы — только эти</label>
            <RegionPicker
              options={regions} value={f.regions}
              onChange={(v) => setF((s) => ({ ...s, regions: v }))}
              placeholder="начните вводить: Татарстан, ХМАО…"
            />
          </div>
          <div className="field" style={{ flex: 1, minWidth: 260 }}>
            <label>Регионы — исключить</label>
            <RegionPicker
              options={regions} value={f.exclude_regions} tone="exclude"
              onChange={(v) => setF((s) => ({ ...s, exclude_regions: v }))}
              placeholder="например: Москва"
            />
          </div>
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Пусто в «только эти» = вся РФ. Осторожно: «Москва» во включении матчит и
          «Московскую область» — если нужна только столица, исключите область.
          В исключении города федерального значения области не задевают.
        </p>

        <h3>Режим</h3>
        <div className="row" style={{ gap: 18 }}>
          <label className="check">
            <input type="checkbox" checked={f.redo} onChange={set('redo')} /> Переделать готовые
          </label>
          <label className="check">
            <input type="checkbox" checked={f.show_browser} onChange={set('show_browser')} /> Показать Chrome
          </label>
        </div>

        <div className="row" style={{ marginTop: 20, alignItems: 'center' }}>
          <button disabled={busy}>{busy ? 'Запускаю…' : 'Запустить'}</button>
          {total > 0 && (
            <span className={heavy ? 'badge warn' : 'muted'}>
              {total} компаний · ~{hours.toFixed(1)} ч ·{' '}
              {byProvider ? 'счёт у провайдера Kimi' : `≈ $${total}–$${total * 2}`}
            </span>
          )}
        </div>
      </form>

      <div className="card">
        <h2>Прогоны</h2>
        {!runs.length && <div className="empty">Прогонов ещё не было</div>}
        {!!runs.length && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Прогон</th><th>Статус</th><th>Отрасли</th><th>Регионы</th>
                  <th className="num">Компаний</th><th className="num">Стоимость</th><th>Начало</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const p = r.params || {}
                  const names = (p.industries || []).map((k) => indLabel[k] || k)
                  const regs = [
                    ...(p.regions || []),
                    ...(p.exclude_regions || []).map((x) => `кроме ${x}`),
                  ]
                  return (
                    <tr key={r.id}>
                      <td><Link to={`/runs/${r.id}`} className="mono">{r.id}</Link></td>
                      <td><RunStatus status={r.status} /></td>
                      <td>
                        {p.leads_json
                          ? <span className="muted">ресёрч по JSON</span>
                          : names.length
                            ? <span title={names.join(', ')}>{names.join(', ')}</span>
                            : <span className="muted">—</span>}
                        {p.model && <div className="muted mono" style={{ fontSize: 11 }}>{p.model}</div>}
                      </td>
                      <td className="muted">{regs.length ? regs.join(', ') : 'вся РФ'}</td>
                      <td className="num">
                        {r.summary ? `${r.summary.ok}/${r.summary.total}` : (r.total ?? '—')}
                      </td>
                      <td className="num">{fmtMoney(r.summary?.cost)}</td>
                      <td className="muted">{fmtTime(r.created_at)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

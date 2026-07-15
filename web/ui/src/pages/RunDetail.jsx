import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, fmtMoney, subscribeRun } from '../api.js'
import { RunStatus } from './Runs.jsx'

const STAGES = [
  ['research', 'дипресёрч'],
  ['person', 'ЛПР'],
  ['writing', '2 .docx'],
  ['onepager', 'one-pager'],
  ['upload', 'Диск'],
]

function StageChain({ company }) {
  const cur = STAGES.findIndex((s) => s[0] === company.stage)
  const finished = company.status === 'done' || company.status === 'skipped'
  return (
    <div className="stages">
      {STAGES.map(([key, label], i) => {
        const done = finished || (cur > -1 && i < cur)
        const active = !finished && i === cur
        return (
          <span key={key} className={`stage ${done ? 'done' : ''} ${active ? 'active' : ''}`}>
            <i className="pip" />{label}
          </span>
        )
      })}
    </div>
  )
}

export default function RunDetail() {
  const { id } = useParams()
  const [run, setRun] = useState(null)
  const [lines, setLines] = useState([])
  const [err, setErr] = useState('')
  const logRef = useRef(null)
  const stick = useRef(true)

  // Снимок + догон пропущенных событий по seq: перезагрузка страницы посреди
  // прогона не теряет ни строчки (события живут на сервере и в events.jsonl).
  useEffect(() => {
    let stop = () => {}
    let cancelled = false
    api.run(id).then((snap) => {
      if (cancelled) return
      setRun(snap)
      setLines(snap.companies ? [] : [])
      fetch(`/api/runs/${id}/log`).then((r) => r.text()).then((t) => {
        setLines(t ? t.split('\n').map((l) => ({ line: l, type: 'log' })) : [])
      })
      if (snap.status === 'running') {
        stop = subscribeRun(id, snap.seq, (ev) => {
          if (ev.line) setLines((ls) => [...ls.slice(-2000), ev])
          // Дёшево и надёжно: состояние компаний/итога держит сервер (Run.apply),
          // фронт его переспрашивает на «значимых» событиях, а не пересобирает сам.
          if (['company_done', 'company_skipped', 'phase', 'finished', 'leads_saved',
               'company_stage', 'run_finished'].includes(ev.type)) {
            api.run(id).then(setRun).catch(() => {})
          }
        }, () => api.run(id).then(setRun).catch(() => {}))
      }
    }).catch((e) => setErr(String(e.message)))
    return () => { cancelled = true; stop() }
  }, [id])

  useEffect(() => {
    const el = logRef.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [lines])

  if (err) return <div className="card err-box">{err}</div>
  if (!run) return <div className="empty">Загружаю…</div>

  const cs = run.companies || []
  const done = cs.filter((c) => c.status === 'done' || c.status === 'skipped').length
  const cost = cs.reduce((s, c) => s + (c.cost || 0), 0)
  const pct = run.total ? Math.round((done / run.total) * 100) : 0

  const cancel = async () => {
    if (!confirm('Остановить прогон? Уже готовые компании останутся на Диске.')) return
    try {
      await api.cancelRun(id)
      setRun(await api.run(id))
    } catch (e) { setErr(String(e.message)) }
  }

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>
            <Link to="/runs" className="muted">Прогоны</Link> / <span className="mono">{run.id}</span>
          </h2>
          <RunStatus status={run.status} />
          {run.phase && (
            <span className="badge">{run.phase === 'collect' ? 'фаза 1: сбор' : 'фаза 2: ресёрч'}</span>
          )}
          <span className="right">
            {run.status === 'running' && (
              <button className="danger" onClick={cancel}>Остановить</button>
            )}
          </span>
        </div>

        <div className="row" style={{ marginTop: 14, gap: 28 }}>
          <div><div className="muted">Компаний</div><b>{done}{run.total ? ` / ${run.total}` : ''}</b></div>
          <div><div className="muted">Стоимость</div><b>{fmtMoney(cost)}</b></div>
          {run.summary && <div><div className="muted">Файлов</div><b>{run.summary.files}</b></div>}
          {run.estimate && <div><div className="muted">Оценка</div><b className="muted">{run.estimate}</b></div>}
        </div>
        {!!run.total && (
          <div className="bar" style={{ marginTop: 12 }}><i style={{ width: `${pct}%` }} /></div>
        )}

        {run.phase === 'collect' && !!Object.keys(run.collect || {}).length && (
          <p className="muted" style={{ marginBottom: 0 }}>
            Сбор: найдено {run.collect.found ?? '—'}, собрано {run.collect.collected ?? '—'}
            {run.collect.picked ? `, отобрано ${run.collect.picked}` : ''}
            {run.collect.done ? `, контакты по ${run.collect.done}` : ''}
          </p>
        )}

        {!!run.errors?.length && (
          <div style={{ marginTop: 14 }}>
            {run.errors.map((e, i) => <div className="err-box" key={i}>{e}</div>)}
          </div>
        )}
      </div>

      {!!cs.length && (
        <div className="card">
          <h2>Компании</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 34 }}>#</th><th>Компания</th><th>Стадии</th>
                  <th className="num">$</th><th></th>
                </tr>
              </thead>
              <tbody>
                {cs.map((c) => (
                  <tr key={c.idx}>
                    <td className="muted mono">{c.idx}</td>
                    <td>
                      {c.inn
                        ? <Link to={`/leads/${c.inn}`}>{c.name || '—'}</Link>
                        : (c.name || '—')}
                      {c.retries > 0 && <span className="badge warn" style={{ marginLeft: 8 }}>ретрай {c.retries}/3</span>}
                      {c.status === 'skipped' && <span className="badge" style={{ marginLeft: 8 }}>по резюму</span>}
                      {c.note && <div className="muted mono" style={{ fontSize: 11 }}>{c.note.slice(0, 90)}</div>}
                    </td>
                    <td><StageChain company={c} /></td>
                    <td className="num">{c.cost ? fmtMoney(c.cost) : '—'}</td>
                    <td>{c.one_pager && <span className="badge ok">+pdf</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <div className="row" style={{ alignItems: 'center', marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>Лог</h2>
          {run.log_path && <span className="muted mono">{run.log_path}</span>}
        </div>
        <div
          className="log" ref={logRef}
          onScroll={(e) => {
            const el = e.currentTarget
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
          }}
        >
          {lines.map((l, i) => (
            <div key={i} className={`l-${l.type || 'log'}`}>{l.line}</div>
          ))}
        </div>
      </div>
    </>
  )
}

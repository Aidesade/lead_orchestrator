import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, fmtRevenue } from '../api.js'

const CONTACTS = [
  ['', 'все'],
  ['full', 'все 4 контакта'],
  ['partial', 'частично'],
  ['none', 'нет контактов'],
]

export default function Leads() {
  const [f, setF] = useState({ q: '', industry: '', region: '', min_revenue: 0, contacts: '', sort: 'revenue' })
  const [data, setData] = useState(null)
  const [facets, setFacets] = useState(null)
  const [inds, setInds] = useState([])
  const [page, setPage] = useState(0)
  const LIMIT = 50

  useEffect(() => {
    api.facets().then(setFacets).catch(() => {})
    api.industries().then(setInds).catch(() => {})
  }, [])

  useEffect(() => {
    const t = setTimeout(() => {
      api.leads({ ...f, limit: LIMIT, offset: page * LIMIT }).then(setData).catch(() => {})
    }, 200)                                   // дебаунс: поиск печатают, а не вставляют
    return () => clearTimeout(t)
  }, [f, page])

  const label = useMemo(
    () => Object.fromEntries(inds.map((i) => [i.key, i.label])), [inds])

  const set = (k) => (e) => { setPage(0); setF((s) => ({ ...s, [k]: e.target.value })) }
  const sortBy = (k) => () => { setPage(0); setF((s) => ({ ...s, sort: k })) }

  const total = data?.total ?? 0
  const pages = Math.ceil(total / LIMIT)

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>Лиды</h2>
          <span className="muted">
            {facets ? `${facets.total} компаний из ${facets.files.length} файлов · ${facets.leads_dir}` : ''}
          </span>
          <a className="right" href={api.exportUrl(f)}>
            <button className="ghost" type="button">Выгрузить CSV</button>
          </a>
        </div>

        <div className="row" style={{ marginTop: 14 }}>
          <div className="field" style={{ flex: 2, minWidth: 220 }}>
            <label>Поиск (название, ИНН, сайт)</label>
            <input value={f.q} onChange={set('q')} placeholder="РЖД или 7708503727" />
          </div>
          <div className="field" style={{ width: 200 }}>
            <label>Отрасль</label>
            <select value={f.industry} onChange={set('industry')}>
              <option value="">все</option>
              {(facets?.industries || []).map((k) => (
                <option key={k} value={k}>{label[k] || k}</option>
              ))}
            </select>
          </div>
          <div className="field" style={{ width: 200 }}>
            <label>Регион</label>
            <select value={f.region} onChange={set('region')}>
              <option value="">все</option>
              {(facets?.regions || []).map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <div className="field" style={{ width: 160 }}>
            <label>Выручка от, ₽</label>
            <input type="number" step="1e9" value={f.min_revenue} onChange={set('min_revenue')} />
          </div>
          <div className="field" style={{ width: 160 }}>
            <label>Контакты</label>
            <select value={f.contacts} onChange={set('contacts')}>
              {CONTACTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
        </div>
      </div>

      <div className="card">
        {!data && <div className="empty">Загружаю…</div>}
        {data && !data.items.length && <div className="empty">Ничего не нашлось</div>}
        {data && !!data.items.length && (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th onClick={sortBy('name')}>Компания {f.sort === 'name' && '▾'}</th>
                    <th>ИНН</th>
                    <th>Отрасль</th>
                    <th>Регион</th>
                    <th className="num" onClick={sortBy('revenue')}>Выручка {f.sort === 'revenue' && '▾'}</th>
                    <th onClick={sortBy('contacts')}>Контакты {f.sort === 'contacts' && '▾'}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((l) => (
                    <tr key={l._inn || l.name}>
                      <td>
                        <Link to={`/leads/${l._inn || encodeURIComponent(l.name)}`}>{l.name}</Link>
                        {l.website && <div className="muted mono" style={{ fontSize: 11 }}>{l.website}</div>}
                      </td>
                      <td className="mono muted">{l._inn || '—'}</td>
                      <td className="muted">{label[l._industry] || l._industry || '—'}</td>
                      <td className="muted">{l._region || '—'}</td>
                      <td className="num">{fmtRevenue(l._revenue)}</td>
                      <td>
                        <span className={`badge ${l._contacts === 4 ? 'ok' : l._contacts ? 'warn' : 'err'}`}>
                          {l._contacts}/4
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {pages > 1 && (
              <div className="row" style={{ marginTop: 14, alignItems: 'center' }}>
                <button className="ghost" disabled={!page} onClick={() => setPage((p) => p - 1)}>Назад</button>
                <span className="muted">стр. {page + 1} из {pages} · всего {total}</span>
                <button className="ghost" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>Вперёд</button>
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}

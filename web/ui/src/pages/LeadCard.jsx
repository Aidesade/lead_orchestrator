import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, fmtRevenue } from '../api.js'

function Row({ k, children }) {
  if (!children) return null
  return (<><dt>{k}</dt><dd>{children}</dd></>)
}

export default function LeadCard() {
  const { inn } = useParams()
  const [card, setCard] = useState(null)
  const [err, setErr] = useState('')
  const [showFindings, setShowFindings] = useState(false)

  useEffect(() => {
    api.lead(inn).then(setCard).catch((e) => setErr(String(e.message)))
  }, [inn])

  if (err) return <div className="card err-box">{err}</div>
  if (!card) return <div className="empty">Загружаю…</div>

  const { lead: l, deliverables: d, findings } = card
  const copy = (t) => navigator.clipboard?.writeText(t)

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>
            <Link to="/leads" className="muted">Лиды</Link> / {l.name}
          </h2>
          <span className={`badge ${l._contacts === 4 ? 'ok' : l._contacts ? 'warn' : 'err'}`}>
            контакты {l._contacts}/4
          </span>
        </div>

        <h3>Профиль</h3>
        <dl className="kv">
          <Row k="ИНН">{l._inn && <span className="mono">{l._inn}</span>}</Row>
          <Row k="ОГРН">{l._ogrn && <span className="mono">{l._ogrn}</span>}</Row>
          <Row k="Выручка">
            {l._revenue ? (
              <>
                {fmtRevenue(l._revenue)}{' '}
                {/* Источник выручки — обязательная ссылка: это не «оборот», а данные конкретного реестра. */}
                {l._revenue_source_url && (
                  <a href={l._revenue_source_url} target="_blank" rel="noreferrer" className="muted">
                    источник: {l._revenue_source_name || l._revenue_src || 'ссылка'}
                  </a>
                )}
              </>
            ) : null}
          </Row>
          <Row k="Отрасль / ОКВЭД">{l._okved_descr || l.niche}</Row>
          <Row k="Регион">{l._region}</Row>
          <Row k="Адрес">{l._address}</Row>
          <Row k="Карточка">
            {l._rusprofile_url && (
              <a href={l._rusprofile_url} target="_blank" rel="noreferrer">RusProfile</a>
            )}
          </Row>
        </dl>

        <h3>Контакты</h3>
        <dl className="kv">
          <Row k="ЛПР">{l.contact_person}</Row>
          <Row k="Телефон">{l.phone}</Row>
          <Row k="Почта">
            {l.email && (
              <>
                <a href={`mailto:${l.email}`}>{l.email}</a>{' '}
                {l._email_kind && <span className="muted">({l._email_kind})</span>}
              </>
            )}
          </Row>
          <Row k="Сайт">
            {l.website && <a href={l.website} target="_blank" rel="noreferrer">{l.website}</a>}
          </Row>
        </dl>

        {(l.pain || l.offer) && (
          <>
            <h3>Гипотеза</h3>
            <dl className="kv">
              <Row k="Боль">{l.pain}</Row>
              <Row k="Оффер">{l.offer}</Row>
            </dl>
          </>
        )}
      </div>

      <div className="card">
        <h2>Деливераблы на Яндекс Диске</h2>
        {!d.available && <div className="muted">правила имён недоступны: {d.error}</div>}
        {d.available && (
          <>
            <p className="muted mono" style={{ marginTop: 0 }}>
              {d.dir}{' '}
              <button className="ghost" style={{ padding: '2px 8px', fontSize: 12 }}
                      onClick={() => copy(d.dir)}>копировать путь</button>
            </p>
            <table>
              <tbody>
                {d.files.map((f) => (
                  <tr key={f.name}>
                    <td style={{ width: 240 }}>{f.kind}</td>
                    <td className="mono muted">{f.name}</td>
                    <td style={{ width: 120 }}>
                      <button className="ghost" style={{ padding: '3px 9px', fontSize: 12 }}
                              onClick={() => copy(f.path)}>копировать</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted" style={{ marginBottom: 0, fontSize: 12 }}>
              Пути вычислены по правилам disk_organize. Файл появляется на Диске только
              после успешного прогона по этой компании.
            </p>
          </>
        )}
      </div>

      <div className="card">
        <div className="row" style={{ alignItems: 'center' }}>
          <h2 style={{ margin: 0 }}>Находки дипресёрча</h2>
          {findings && (
            <span className="muted">кэш {findings.age_h} ч назад · {(findings.size / 1024).toFixed(0)} КБ</span>
          )}
          {findings && (
            <button className="ghost right" onClick={() => setShowFindings((s) => !s)}>
              {showFindings ? 'Свернуть' : 'Показать'}
            </button>
          )}
        </div>
        {!findings && (
          <div className="muted" style={{ marginTop: 10 }}>
            Кэша находок нет — по этой компании дипресёрч ещё не гоняли
            (или он старше TTL и был вычищен).
          </div>
        )}
        {findings && showFindings && (
          <div className="findings" style={{ marginTop: 12 }}>{findings.text}</div>
        )}
      </div>
    </>
  )
}

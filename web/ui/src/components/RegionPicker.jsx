import { useMemo, useRef, useState } from 'react'

/** Мультивыбор регионов с подсказкой по вводу.
 *
 *  Отдаёт наружу массив ТОКЕНОВ (не «красивых» названий): фильтр по региону в RusProfile
 *  клиентский и матчит регион ПОДСТРОКОЙ, поэтому в прогон должен уехать именно тот токен,
 *  который гарантированно входит в полное имя субъекта («Татарстан» ⊂ «Республика Татарстан»).
 */
export default function RegionPicker({ options, value, onChange, placeholder, tone }) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [hi, setHi] = useState(0)
  const boxRef = useRef(null)

  const matches = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (!s) return []
    return options
      .filter((o) => !value.includes(o.value))
      .filter((o) => o.label.toLowerCase().includes(s) || o.value.toLowerCase().includes(s))
      .slice(0, 8)
  }, [q, options, value])

  const add = (token) => {
    if (token && !value.includes(token)) onChange([...value, token])
    setQ('')
    setOpen(false)
    setHi(0)
  }

  const key = (e) => {
    if (!open || !matches.length) {
      // Enter по свободному тексту: регион, которого нет в справочнике, всё равно
      // валиден — коллектор матчит подстрокой, а не по списку.
      if (e.key === 'Enter' && q.trim()) { e.preventDefault(); add(q.trim()) }
      return
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi((h) => (h + 1) % matches.length) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi((h) => (h - 1 + matches.length) % matches.length) }
    else if (e.key === 'Enter') { e.preventDefault(); add(matches[hi]?.value || q.trim()) }
    else if (e.key === 'Escape') setOpen(false)
  }

  return (
    <div className="rp" ref={boxRef}>
      <div className={`rp-box ${tone === 'exclude' ? 'exclude' : ''}`}>
        {value.map((v) => (
          <span className="rp-chip" key={v}>
            {v}
            <button type="button" onClick={() => onChange(value.filter((x) => x !== v))}>×</button>
          </span>
        ))}
        <input
          value={q}
          placeholder={value.length ? '' : placeholder}
          onChange={(e) => { setQ(e.target.value); setOpen(true); setHi(0) }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 120)}   // клик по подсказке успевает отработать
          onKeyDown={key}
        />
      </div>
      {open && !!matches.length && (
        <ul className="rp-menu">
          {matches.map((o, i) => (
            <li
              key={o.value}
              className={i === hi ? 'hi' : ''}
              onMouseEnter={() => setHi(i)}
              onMouseDown={() => add(o.value)}
            >
              <b>{o.label}</b>
              {o.label !== o.value && <span className="muted"> → {o.value}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

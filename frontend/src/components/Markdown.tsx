import { Fragment, type ReactNode } from 'react'

/** 리서치 리포트용 아주 작은 마크다운 표시기. HTML 을 해석하지 않고 제목·목록·굵게·링크만 그립니다. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = []
  const re = /\*\*(.+?)\*\*|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    if (m[1]) out.push(<strong key={m.index}>{m[1]}</strong>)
    else out.push(<a key={m.index} href={m[3]} target="_blank" rel="noreferrer noopener">{m[2]}</a>)
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  let list: string[] = []
  const flush = (key: number) => {
    if (list.length) {
      blocks.push(
        <ul key={`ul${key}`} style={{ margin: '6px 0 10px', paddingLeft: 20 }}>
          {list.map((li, i) => <li key={i} style={{ marginTop: 3 }}>{inline(li)}</li>)}
        </ul>,
      )
      list = []
    }
  }
  text.split('\n').forEach((line, i) => {
    const t = line.trimEnd()
    if (t.startsWith('- ')) {
      list.push(t.slice(2))
      return
    }
    flush(i)
    if (!t.trim()) return
    if (t === '---') blocks.push(<hr key={i} style={{ border: 0, borderTop: '1px solid var(--border)', margin: '14px 0' }} />)
    else if (t.startsWith('### ')) blocks.push(<h4 key={i} style={{ margin: '14px 0 4px', fontSize: 14 }}>{inline(t.slice(4))}</h4>)
    else if (t.startsWith('## ')) blocks.push(<h3 key={i} style={{ margin: '18px 0 6px', fontSize: 15 }}>{inline(t.slice(3))}</h3>)
    else if (t.startsWith('# ')) blocks.push(<h2 key={i} style={{ margin: '0 0 10px', fontSize: 17 }}>{inline(t.slice(2))}</h2>)
    else blocks.push(<p key={i} style={{ margin: '6px 0', lineHeight: 1.65 }}>{inline(t)}</p>)
  })
  flush(-1)
  return <Fragment>{blocks}</Fragment>
}

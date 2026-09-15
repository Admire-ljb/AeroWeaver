import { useEffect, useState } from 'react'
import { ArrowLeft, ChevronLeft, ChevronRight, Download, RefreshCw, Search } from 'lucide-react'
import './TrajectoryMemoryWorkspace.css'

const PAGE = 25
const number = (value) => value == null ? '--' : Number(value).toFixed(3)
const json = (value) => JSON.stringify(value ?? null, null, 2)

function useRead(url, revision) {
  const [result, setResult] = useState({ url: '', data: null, error: '', loading: true })
  useEffect(() => {
    if (!url) return undefined
    const controller = new AbortController()
    fetch(url, { signal: controller.signal })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`)
        return data
      })
      .then((data) => setResult({ url, data, error: '', loading: false }))
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ url, data: null, error: error.message, loading: false })
      })
    return () => controller.abort()
  }, [url, revision])
  return result.url === url ? result : { data: null, error: '', loading: Boolean(url) }
}

function Pager({ offset, total, onChange, t }) {
  return <div className="tm-pager">
    <span>{total ? `${offset + 1}-${Math.min(offset + PAGE, total)} / ${total}` : '0 / 0'}</span>
    <button type="button" title={t('上一页', 'Previous page')} aria-label={t('上一页', 'Previous page')} disabled={!offset} onClick={() => onChange(Math.max(0, offset - PAGE))}><ChevronLeft size={16} /></button>
    <button type="button" title={t('下一页', 'Next page')} aria-label={t('下一页', 'Next page')} disabled={offset + PAGE >= total} onClick={() => onChange(offset + PAGE)}><ChevronRight size={16} /></button>
  </div>
}

export default function TrajectoryMemoryWorkspace({ language, apiBase }) {
  const t = (zh, en) => language === 'zh' ? zh : en
  const taskName = (value) => ({ coverage: t('覆盖', 'Coverage'), pursuit: t('追逐', 'Pursuit'),
    navigation: t('协同导航', 'Navigation'), private_communication: t('私有通信', 'Private communication'),
    circle: t('环形编队', 'Circle formation'), line: t('线形编队', 'Line formation'),
    world_communication: t('协同通信', 'World communication'), collection: t('收集', 'Collection'),
    concealment: t('隐蔽', 'Concealment') }[value] || value)
  const policyName = (value) => value === 'local_skill_baseline' ? t('局部规则', 'Local baseline') : value === 'llm' ? 'LLM' : value || '--'
  const [revision, setRevision] = useState(0)
  const [view, setView] = useState('episodes')
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [episode, setEpisode] = useState('')
  const [agent, setAgent] = useState('')
  const [role, setRole] = useState('')
  const [offset, setOffset] = useState(0)
  const [recordId, setRecordId] = useState('')
  useEffect(() => {
    const timer = window.setInterval(() => setRevision((value) => value + 1), 5000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const timer = window.setTimeout(() => { setSearch(query); setOffset(0); setRecordId('') }, 250)
    return () => window.clearTimeout(timer)
  }, [query])

  const stats = useRead(`${apiBase}/api/memory/trajectory-stats`, revision)
  const episodeInfo = useRead(episode ? `${apiBase}/api/memory/episodes/${encodeURIComponent(episode)}` : '', revision)
  const listingEpisodes = view === 'episodes' && !episode
  const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset), q: search })
  if (episode) params.set('episode_id', episode)
  if (agent) params.set('agent_id', agent)
  if (role) params.set('role', role)
  if (view === 'failed') params.set('failed', 'true')
  const list = useRead(`${apiBase}/api/memory/${listingEpisodes ? 'episodes' : 'transitions'}?${params}`, revision)
  const detail = useRead(recordId ? `${apiBase}/api/memory/transitions/${encodeURIComponent(recordId)}` : '', revision)
  const record = detail.data?.record
  const items = list.data?.items || []
  const allAgents = episodeInfo.data?.agents || []
  const roles = [...new Set(allAgents.map((item) => item.role))]
  const error = stats.error || list.error || episodeInfo.error || detail.error

  const reset = (nextView) => {
    setView(nextView); setEpisode(''); setAgent(''); setRole(''); setOffset(0); setRecordId(''); setQuery(''); setSearch('')
  }
  const openEpisode = (id) => {
    setEpisode(id); setOffset(0); setRecordId(''); setAgent(''); setRole(''); setQuery(''); setSearch('')
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([json(record)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url; link.download = `${record.experience_id}.json`; link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return <section className="workspace-body tm-workspace" aria-label={t('轨迹记忆', 'Trajectory Memory')}>
    <header className="tm-heading">
      <div><strong>{t('轨迹记忆', 'Trajectory Memory')}</strong><span>{t('执行记录', 'Execution records')}</span></div>
      <button type="button" className="tm-icon" title={t('刷新', 'Refresh memory')} aria-label={t('刷新', 'Refresh memory')} onClick={() => setRevision((value) => value + 1)}><RefreshCw size={18} /></button>
    </header>
    <div className="tm-stats">
      {[
        ['episodes', 'episodes', t('回合', 'Episodes')],
        ['transitions', 'records', t('逐步轨迹', 'Transitions')],
        ['failed', 'failed_records', t('失败关联区间', 'Failed intervals')],
      ].map(([key, field, label]) => <button type="button" className={view === key ? 'active' : ''} key={key} aria-pressed={view === key} onClick={() => reset(key)}>
        <span>{label}</span><strong>{stats.data?.stats?.[field] ?? '--'}</strong>
      </button>)}
    </div>
    {error && <div className="tm-error" role="alert">{error}</div>}
    <div className="tm-toolbar">
      {episode && <button className="tm-icon" type="button" title={t('返回回合列表', 'Back to episodes')} aria-label={t('返回回合列表', 'Back to episodes')} onClick={() => reset('episodes')}><ArrowLeft size={18} /></button>}
      <label className="tm-search"><Search size={16} /><input aria-label={t('搜索轨迹', 'Search memory')} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('回合、场景、技能、角色或 UAV', 'Episode, scenario, skill, role or UAV')} /></label>
      {episode && <>
        <select aria-label={t('参与者', 'Agent filter')} value={agent} onChange={(event) => { setAgent(event.target.value); setOffset(0); setRecordId('') }}>
          <option value="">{t('全部参与者', 'All agents')}</option>
          {[...new Set(allAgents.map((item) => item.agent_id))].map((id) => <option key={id}>{id}</option>)}
        </select>
        <select aria-label={t('角色', 'Role filter')} value={role} onChange={(event) => { setRole(event.target.value); setOffset(0); setRecordId('') }}>
          <option value="">{t('全部角色', 'All roles')}</option>{roles.map((name) => <option key={name}>{name}</option>)}
        </select>
      </>}
    </div>
    {episode && <div className="tm-episode-heading"><strong>{taskName(episodeInfo.data?.task) || t('回合', 'Episode')}</strong><code>{episode}</code><span>{policyName(episodeInfo.data?.metadata?.condition)}</span></div>}
    {list.loading && <p role="status">{t('加载中...', 'Loading...')}</p>}
    {!list.loading && !list.error && !items.length && <p className="empty-copy">{search || agent || role ? t('无匹配记录', 'No matching records') : t('暂无记录', 'No records')}</p>}
    {listingEpisodes ? <div className="tm-episode-list">
      {items.map((item) => <button type="button" className="tm-episode-row" key={item.episode_id} onClick={() => openEpisode(item.episode_id)}>
        <span><strong>{taskName(item.task)}</strong><code>{item.episode_id}</code></span>
        <span>{policyName(item.policy)}<small>{item.agents} {t('参与者', 'agents')} / {item.steps} {t('步', 'steps')}</small></span>
        <span><strong>{item.records}</strong><small>{t('条轨迹', 'records')}</small></span>
        <span className={item.returns_finalized ? 'tm-final' : ''}>{item.returns_finalized ? t('回报已结算', 'Returns finalized') : t('记录中', 'Recording')}<small>{new Date(item.started_at * 1000).toLocaleString()}</small></span>
        <ChevronRight size={18} />
      </button>)}
    </div> : <div className="tm-table-scroll">
      <table className="tm-table"><thead><tr><th>{t('步', 'Step')}</th><th>{t('参与者 / 角色', 'Agent / role')}</th><th>{t('技能', 'Skill')}</th><th>r</th><th>G</th><th>{t('调用', 'Invocation')}</th></tr></thead>
        <tbody>{items.map((item) => <tr key={item.experience_id} className={recordId === item.experience_id ? 'selected' : ''}>
          <td><button type="button" className="tm-step" aria-label={`${t('查看步骤', 'Inspect step')} ${item.step_index} ${item.agent_id}`} onClick={() => setRecordId(item.experience_id)}>{item.step_index}</button></td>
          <td>{item.agent_id}<small>{item.role}</small></td>
          <td><button type="button" className="tm-skill" onClick={() => setRecordId(item.experience_id)}>{item.skill}</button><small>{item.continued_skill ? t('持续执行', 'Continued') : item.selector_source || '--'}</small></td>
          <td>{number(item.immediate_reward)}</td><td>{number(item.return_value)}{!item.return_finalized && <small>{t('未结算', 'Partial')}</small>}</td>
          <td className={item.success ? 'tm-final' : 'tm-failed'}>{item.success ? t('已接受', 'Accepted') : t('失败', 'Failed')}</td>
        </tr>)}</tbody>
      </table>
    </div>}
    <Pager offset={offset} total={list.data?.total || 0} onChange={(value) => { setOffset(value); setRecordId('') }} t={t} />
    {recordId && <section className="tm-inspector" aria-label={t('轨迹详情', 'Transition details')}>
      {detail.loading && <p role="status">{t('加载详情...', 'Loading transition...')}</p>}
      {record && <>
        <header className="tm-heading"><div><strong>{record.skill}</strong><span>{record.agent_id} / {record.role} / {t('步', 'step')} {record.step_index}</span></div>
          <button type="button" className="tm-icon" title={t('下载记录 JSON', 'Download record JSON')} aria-label={t('下载记录 JSON', 'Download record JSON')} onClick={download}><Download size={18} /></button>
        </header>
        <div className="tm-record-facts">
          <span>{t('回合', 'Episode')} <button type="button" onClick={() => openEpisode(record.episode_id)}>{record.episode_id}</button></span>
          <span>{t('轨迹段', 'Segment')} <b>{record.trajectory_id}</b></span>
          <span>r <b>{number(record.immediate_reward)}</b></span><span>G <b>{number(record.return_value)}</b></span><span>gamma <b>{record.gamma}</b></span>
          <span>{record.return_finalized ? t('回报已结算', 'Returns finalized') : t('回报未结算', 'Partial return')}</span>
          <span>{record.metadata.reuse_allowed ? t('可用于检索', 'Retrieval enabled') : t('仅记录', 'Write only')}</span>
        </div>
        {record.trace?.error && <div className="tm-error">{record.trace.error}</div>}
        <p className="tm-provenance">{t('奖励来源', 'Reward source')}: <code>{record.reward_source || '--'}</code></p>
        <div className="tm-json-grid">
          {[[t('局部状态 s', 'Local state s'), record.state], [t('下一状态 s′', 'Next state s\u2032'), record.next_state],
            [t('技能调用', 'Skill invocation'), record.invocation], [t('定向消息', 'Directed messages'), record.trace?.peer_messages],
            [t('决策与执行记录', 'Decision and execution trace'), record.trace], [t('元数据', 'Metadata'), record.metadata]]
            .map(([label, value]) => <details key={label} open={label === t('技能调用', 'Skill invocation')}><summary>{label}</summary><pre>{json(value)}</pre></details>)}
        </div>
      </>}
    </section>}
  </section>
}

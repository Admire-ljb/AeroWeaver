import { useEffect, useState } from 'react'

export default function MockTaskControls({ language }) {
  const [tasks, setTasks] = useState([])
  const [current, setCurrent] = useState(null)
  const [taskId, setTaskId] = useState('coverage')
  const [policy, setPolicy] = useState('local_skill_baseline')
  const [seed, setSeed] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const zh = language === 'zh'
  useEffect(() => {
    let alive = true
    const refresh = async () => {
      try {
        const response = await fetch('/api/mock/tasks')
        const data = await response.json()
        if (alive) { setTasks(data.tasks || []); setCurrent(data.current) }
      } catch { /* The normal connection indicator owns connectivity state. */ }
    }
    refresh()
    const interval = setInterval(refresh, 1500)
    return () => { alive = false; clearInterval(interval) }
  }, [])
  const run = async (action) => {
    setBusy(true)
    setError('')
    try {
      const response = await fetch(`/api/mock/tasks/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, policy, seed: Number(seed) }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.error || 'Task request failed')
      if (data.task) setCurrent(data.task)
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }
  const selected = tasks.find(t => t.id === taskId)
  return <div className="mock-task-controls">
    <strong>{zh ? 'Mock 任务' : 'Mock Task'}</strong>
    <label>{zh ? '场景' : 'Scenario'}
      <select value={taskId} onChange={e => setTaskId(e.target.value)} disabled={busy || current?.status === 'running'}>
        {tasks.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}
      </select>
    </label>
    <div className="mock-task-objective">{selected?.objective}</div>
    <div className="mock-task-roles">{selected?.roles.map((role, i) => <span key={i}>UAV-{i + 1}: {role.replaceAll('_', ' ')}</span>)}</div>
    <label>{zh ? '选择器' : 'Selector'}
      <select value={policy} onChange={e => setPolicy(e.target.value)} disabled={current?.status === 'running'}>
        <option value="local_skill_baseline">{zh ? '本地技能基线' : 'Local skill baseline'}</option>
        <option value="llm">{zh ? 'LLM 智能体' : 'LLM agents'}</option>
      </select>
    </label>
    <label>{zh ? '种子' : 'Seed'}<input type="number" min="0" step="1" value={seed} onChange={e => setSeed(e.target.value)} /></label>
    <div className="mock-task-actions">
      <button className="setting-apply" disabled={busy || !selected || current?.status === 'running'} onClick={() => run('start')}>{zh ? '启动任务' : 'Start Task'}</button>
      <button className="setting-secondary" disabled={busy || current?.status !== 'running'} onClick={() => run('stop')}>{zh ? '停止' : 'Stop'}</button>
    </div>
    {current && <output>{current.title}: {current.status} · {current.round}</output>}
    {error && <div className="mock-task-error" role="alert">{error}</div>}
  </div>
}

export default function MockTaskOverlay({ task }) {
  if (!task) return null
  const visible = task.objects.filter(o => !o.delivered && !o.carried_by)
  return <g className="mock-task-overlay">
    {visible.map(o => {
      const x = 50 + o.position[1] / 2
      const y = 50 - o.position[0] / 2
      const color = o.color === 'red' ? '#ff7878' : o.color === 'blue' ? '#69bbff'
        : o.kind === 'forest' ? '#5ab584' : o.kind === 'food' ? '#ffcd56' : '#b8e4f4'
      return <g key={o.id} style={{ color }}>
        <circle cx={x} cy={y} r={o.radius ? o.radius / 2 : o.kind === 'slot' ? 1.5 : 1}
          fill="currentColor" fillOpacity={o.kind === 'forest' ? 0.15 : 0.4}
          stroke="currentColor" strokeWidth="0.25" strokeDasharray={o.kind === 'slot' ? '0.6 0.4' : undefined} />
        <text x={x + 1.5} y={y - 1.4}>{o.id.replaceAll('_', ' ')}</text>
      </g>
    })}
  </g>
}

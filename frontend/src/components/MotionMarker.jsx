import { useEffect, useRef } from 'react'

export default function MotionMarker({ x, y, children, style, ...props }) {
  const element = useRef(null)
  const sample = useRef({ from: [x, y], to: [x, y], start: 0, duration: 100 })

  useEffect(() => {
    const now = performance.now(), old = sample.current
    const fraction = Math.max(0, Math.min(1, (now - old.start) / old.duration))
    const current = old.from.map((value, i) => value + (old.to[i] - value) * fraction)
    const jumped = Math.hypot(x - old.to[0], y - old.to[1]) > 15
    sample.current = { from: jumped ? [x, y] : current, to: [x, y], start: now, duration: 100 }
  }, [x, y])

  useEffect(() => {
    let frame
    const draw = (now) => {
      const node = element.current, parent = node?.offsetParent
      if (parent) {
        const data = sample.current
        const fraction = Math.max(0, Math.min(1, (now - data.start) / data.duration))
        const point = data.from.map((value, i) => value + (data.to[i] - value) * fraction)
        node.style.transform = `translate3d(${point[0] * parent.clientWidth / 100}px, ${point[1] * parent.clientHeight / 100}px, 0) translate(-50%, -50%)`
      }
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [])

  return <button ref={element} {...props} style={{ ...style, left: 0, top: 0, transition: 'none' }}>{children}</button>
}

/**
 * useSocket.js
 * Socket.IO connection hook for WebSocket events.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { io } from 'socket.io-client'

const SERVER_URL = window.location.protocol + '//' + window.location.host

function logEntryText(entry) {
  return typeof entry === 'object' ? entry.msg || entry.message || JSON.stringify(entry) : String(entry)
}

function isRobotSelectionLog(entry) {
  const level = typeof entry === 'object' ? String(entry.level || 'info').toLowerCase() : 'info'
  if (level === 'user' || level === 'chat') return false
  return /^选中机器人[:：]\s*UAV[_-]?\d+\b/i.test(logEntryText(entry).trim())
}

export function useSocket() {
  const socketRef = useRef(null)
  const [connected, setConnected] = useState(false)
  const [systemStatus, setSystemStatus] = useState({
    initialized: false,
    mode: 'manual',
    is_executing: false,
    ai_executing: false,
    executing_robots: [],
    current_robot: 'UAV_1',
  })
  const [worldState, setWorldState] = useState({ robots: {}, targets: [] })
  // skillCatalog: { robot_id: [skills] } -- each robot owns an independent skill catalog and execution history.
  const [skillCatalog, setSkillCatalog] = useState({})
  const [logs, setLogs] = useState([])
  const [lastSkillResult, setLastSkillResult] = useState(null)
  const [lastAiPlan, setLastAiPlan] = useState(null)
  const [lastAiReport, setLastAiReport] = useState(null)
  const [aiThinking, setAiThinking] = useState({ phase: 'idle', detail: '' })
  const [aiThoughts, setAiThoughts] = useState([])  // Structured reasoning trace.
  const [aiStream, setAiStream] = useState({ text: '', done: true })
  const [sensorCamera, setSensorCamera] = useState(null)
  const [sensorCameras, setSensorCameras] = useState({})
  const [sensorScene, setSensorScene] = useState(null)
  const [sensorLidar, setSensorLidar] = useState(null)
  const [cockpitOpen, setCockpitOpen] = useState(false)
  const [cockpitInitialView, setCockpitInitialView] = useState('front')
  const [chatHistory, setChatHistory] = useState([])

  useEffect(() => {
    const socket = io(SERVER_URL, {
      transports: ['websocket', 'polling'],
      reconnectionAttempts: 10,
      reconnectionDelay: 1000,
    })
    socketRef.current = socket

    socket.on('connect', () => {
      setConnected(true)
      console.log('[Socket] connected:', socket.id)
    })
    socket.on('disconnect', () => {
      setConnected(false)
      console.log('[Socket] disconnected')
    })

    socket.on('system_status', (data) => setSystemStatus(data))
    socket.on('world_state', (data) => setWorldState(data))
    socket.on('skill_catalog', (data) => setSkillCatalog(data))
    socket.on('skill_result', (data) => setLastSkillResult(data))
    socket.on('ai_plan_result', (data) => setLastAiPlan(data))
    socket.on('ai_execution_report', (data) => setLastAiReport(data))
    socket.on('ai_thinking', (data) => {
      setAiThinking(data)
      // Clear reasoning trace when a new task starts.
      if (data.phase === 'planning') setAiThoughts([])
    })
    socket.on('ai_thought', (data) => {
      setAiThoughts(prev => {
        // Keep only the latest event for the same iteration.
        const filtered = prev.filter(t => t.iteration !== data.iteration)
        return [...filtered, data].sort((a, b) => a.iteration - b.iteration)
      })
    })
    socket.on('ai_stream', (data) => setAiStream(prev => {
      if (data.done) return { text: '', done: true }
      return { text: (prev.done ? '' : (prev.text || '')) + data.token, done: false }
    }))
    socket.on('sensor_camera', (data) => setSensorCamera(data))
    socket.on('sensor_cameras', (data) => setSensorCameras(data))
    socket.on('sensor_scene', (data) => setSensorScene(data))
    socket.on('sensor_lidar', (data) => setSensorLidar(data))
    socket.on('ai_chat_reply', (data) => {
      if (data.ok && data.reply) {
        const now = Date.now()
        setChatHistory(prev => [
          ...prev,
          { role: 'assistant', content: data.reply, intent: data.intent, ts: now },
        ])
        setLogs(prev => {
          const next = [...prev, { ts: now, level: data.intent === 'RESULT' ? 'warn' : 'info', msg: data.reply }]
          return next.length > 300 ? next.slice(-300) : next
        })
      }
    })

    // New robots join dynamically; world_state already contains full details, so only auto-switch here.
    socket.on('robot_joined', (info) => {
      console.log('[Socket] robot_joined:', info)
      // If no robot is selected, or the current one is a placeholder, auto-select the new robot.
      setSystemStatus(prev => {
        if (!prev.current_robot || prev.current_robot === '') {
          return { ...prev, current_robot: info.robot_id }
        }
        return prev
      })
    })

    socket.on('log', (entry) => {
      if (isRobotSelectionLog(entry)) return
      setLogs(prev => {
        const next = [...prev, entry]
        return next.length > 300 ? next.slice(-300) : next
      })
    })

    return () => {
      socket.disconnect()
    }
  }, [])

  // Execute a skill in manual mode.
  const executeSkill = useCallback((robotId, skillName, parameters = {}) => {
    if (socketRef.current) {
      socketRef.current.emit('execute_skill', { robot_id: robotId, skill_name: skillName, parameters })
    }
  }, [])

  // Register or update a robot.
  const registerRobot = useCallback((robot) => {
    if (socketRef.current) {
      socketRef.current.emit('register_robot', robot)
    }
  }, [])

  // Select robot.
  const selectRobot = useCallback((robotId) => {
    if (socketRef.current) {
      socketRef.current.emit('select_robot', { robot_id: robotId })
    }
  }, [])

  // Switch mode.
  const setMode = useCallback((mode) => {
    if (socketRef.current) {
      socketRef.current.emit('set_mode', { mode })
    }
    // Also switch through REST API for reliability.
    fetch(`${SERVER_URL}/api/mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    })
      .then(r => r.json())
      .then(data => console.log('[Mode]', data))
      .catch(e => console.error('[Mode error]', e))
  }, [])

  // Submit AI task.
  const submitAiTask = useCallback((task, useTools = false) => {
    if (socketRef.current) {
      const now = Date.now()
      setAiStream({ text: '', done: true }) // Reset stream.
      setLogs(prev => {
        const next = [...prev, { ts: now, level: 'user', msg: `Mission input: ${task}` }]
        return next.length > 300 ? next.slice(-300) : next
      })
      socketRef.current.emit('ai_task', { task, use_tools: useTools })
    }
  }, [])

  // Stop execution.
  const stopExecution = useCallback(() => {
    if (socketRef.current) {
      socketRef.current.emit('stop_execution')
    }
  }, [])

  // Initialize system.
  const initSystem = useCallback(() => {
    fetch(`${SERVER_URL}/api/init`, { method: 'POST' })
      .then(r => r.json())
      .then(d => console.log('[Init]', d))
      .catch(e => console.error('[Init error]', e))
  }, [])

  // Open/close cockpit.
  const openCockpit = useCallback((view) => { setCockpitInitialView(view || 'front'); setCockpitOpen(true) }, [])
  const closeCockpit = useCallback(() => setCockpitOpen(false), [])

  // AI chat.
  const sendChat = useCallback((message, interactionMode = 'auto', displayMessage = message) => {
    if (socketRef.current) {
      const now = Date.now()
      // Keep machine-readable area context out of the operator-facing transcript.
      setChatHistory(prev => [...prev, { role: 'user', content: displayMessage, mode: interactionMode, ts: now }])
      setLogs(prev => {
        const next = [...prev, { ts: now, level: 'user', msg: displayMessage }]
        return next.length > 300 ? next.slice(-300) : next
      })
      socketRef.current.emit('ai_chat', { message, mode: interactionMode })
    }
  }, [])
  // Get raw socket reference for cockpit.
  const getSocket = useCallback(() => socketRef.current, [])

  return {
    connected,
    systemStatus,
    worldState,
    skillCatalog,
    logs,
    lastSkillResult,
    lastAiPlan,
    lastAiReport,
    aiThinking,
    aiThoughts,
    aiStream,
    sensorCamera,
    sensorCameras,
    sensorScene,
    sensorLidar,
    cockpitOpen,
    cockpitInitialView,
    chatHistory,
    executeSkill,
    selectRobot,
    setMode,
    submitAiTask,
    stopExecution,
    initSystem,
    openCockpit,
    closeCockpit,
    getSocket,
    sendChat,
    registerRobot,
  }
}

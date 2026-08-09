import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSocket } from './hooks/useSocket'
import CockpitView from './components/CockpitView'
import SkillPanel, { skillDescription, skillLabel } from './components/SkillPanel'
import {
  createDemoTrajectorySamples,
  downloadTextFile,
  groupTrajectorySamples,
  readRobotPosition,
  readRobotSpeed,
  summarizeTrajectory,
  trajectoryColor,
  trajectoryCsv,
  trajectoryJson,
} from './trajectory'
import './App.css'

const API_BASE = window.location.protocol + '//' + window.location.host
const DEFAULT_LANGUAGE = 'en'
const MAX_UAV_COUNT = 10

function textFor(language, zh, en) {
  return language === 'zh' ? zh : en
}

function makeTranslator(language) {
  return (zh, en) => textFor(language, zh, en)
}

function statusText(status, language) {
  const normalized = String(status || 'idle').toLowerCase()
  const labels = {
    idle: ['空闲', 'Idle'],
    executing: ['执行中', 'Executing'],
    airborne: ['飞行中', 'Airborne'],
    standby: ['待命', 'Standby'],
    busy: ['忙碌', 'Busy'],
  }
  const [zh, en] = labels[normalized] || [status || '空闲', status || 'Idle']
  return textFor(language, zh, en)
}

function logTagText(tag, language) {
  const labels = {
    User: ['用户', 'User'],
    System: ['系统', 'System'],
    Assistant: ['助手', 'Assistant'],
    Execution: ['执行', 'Execution'],
    Notice: ['提示', 'Notice'],
    Error: ['错误', 'Error'],
    Result: ['结果', 'Result'],
    Record: ['记录', 'Record'],
  }
  const [zh, en] = labels[tag] || [tag, tag]
  return textFor(language, zh, en)
}

function skillTypeText(type, language) {
  const normalized = String(type || '').toLowerCase()
  if (normalized === 'perception') return textFor(language, '感知', 'Perception')
  if (normalized === 'soft' || normalized === 'advanced') return textFor(language, '高级', 'Advanced')
  return textFor(language, '基础', 'Basic')
}

const ADVANCED_SKILL_COPY = {
  area_recon: {
    zhTitle: '环境侦察',
    enTitle: 'Area Reconnaissance',
    zhSummary: '快速了解指定区域，建立周边环境的整体认知。',
    enSummary: 'Survey an area to establish an overall understanding of the surrounding environment.',
  },
  building_inspect: {
    zhTitle: '建筑巡检',
    enTitle: 'Building Inspection',
    zhSummary: '巡检建筑屋顶、外墙、窗户和结构状况。',
    enSummary: 'Inspect a building roof, facades, windows, and structural condition.',
  },
  flight_safety: {
    zhTitle: '飞行安全经验',
    enTitle: 'Flight Safety Experience',
    zhSummary: '在无人机移动前和飞行过程中应用经过验证的安全经验。',
    enSummary: 'Apply validated flight-safety practices before and during vehicle movement.',
  },
  integrate_platform: {
    zhTitle: '平台接入',
    enTitle: 'Platform Integration',
    zhSummary: '为新平台或设备生成、验证并部署适配器。',
    enSummary: 'Generate, validate, and deploy an adapter for a new platform or device.',
  },
  patrol_area: {
    zhTitle: '区域巡逻',
    enTitle: 'Area Patrol',
    zhSummary: '沿指定航线飞行，在关键位置悬停观察并完成区域巡逻。',
    enSummary: 'Follow a planned route, hover at key locations, and observe the patrol area.',
  },
  rescue_person: {
    zhTitle: '人员搜救',
    enTitle: 'Search and Rescue',
    zhSummary: '在灾区搜索受困人员，标记位置并向操作员汇报。',
    enSummary: 'Search for people in a disaster area, mark their positions, and report findings.',
  },
  safe_approach: {
    zhTitle: '安全接近观察',
    enTitle: 'Safe Approach and Observation',
    zhSummary: '逐步接近目标，同时持续检查障碍物与飞行安全。',
    enSummary: 'Approach a target incrementally while checking obstacles and flight safety.',
  },
  search_target: {
    zhTitle: '目标搜索',
    enTitle: 'Target Search',
    zhSummary: '在指定区域搜索人员或物体，并标记发现位置。',
    enSummary: 'Search a specified area for people or objects and mark detected targets.',
  },
  smart_navigate: {
    zhTitle: '智能导航',
    enTitle: 'Smart Navigation',
    zhSummary: '分段规划航线并避开障碍，安全飞往远距离目标。',
    enSummary: 'Plan a segmented route and avoid obstacles while flying to a distant target.',
  },
}

function containsCjk(value) {
  return /[\u3400-\u9fff]/.test(String(value || ''))
}

function humanizeSkillId(name) {
  return String(name || 'Advanced Skill')
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function localizedAdvancedSkill(skill, language) {
  const name = skill?.name || ''
  const copy = ADVANCED_SKILL_COPY[name]
  if (copy) {
    return {
      title: textFor(language, copy.zhTitle, copy.enTitle),
      summary: textFor(language, copy.zhSummary, copy.enSummary),
    }
  }

  const rawTitle = String(skill?.title || name || '')
  const rawSummary = String(skill?.summary || '')
  if (language === 'zh') {
    const title = rawTitle.replace(/\s*\([^)]*[A-Za-z][^)]*\)\s*$/, '').trim() || name
    return {
      title,
      summary: rawSummary && rawSummary !== rawTitle ? rawSummary : '',
    }
  }

  const parenthesizedEnglish = [...rawTitle.matchAll(/\(([^()]*)\)/g)]
    .map((match) => match[1].trim())
    .find((value) => /[A-Za-z]/.test(value) && !containsCjk(value))
  const rawTitleLooksLikeId = rawTitle === name && /[-_]/.test(rawTitle)
  const title = parenthesizedEnglish
    || (containsCjk(rawTitle) || rawTitleLooksLikeId ? humanizeSkillId(name) : rawTitle)
    || humanizeSkillId(name)
  const summary = containsCjk(rawSummary)
    ? ''
    : rawSummary && rawSummary !== rawTitle && rawSummary !== title
      ? rawSummary
      : ''
  return { title, summary }
}

function localizedAdvancedSkillDetail(doc, language) {
  if (!doc) return ''
  if (doc.error) return doc.error
  const display = localizedAdvancedSkill(doc, language)
  const content = String(doc.content || '').trim()
  const contentMatchesLanguage = language === 'zh' ? containsCjk(content) : !containsCjk(content)
  if (contentMatchesLanguage && content) return `${display.title}: ${content.slice(0, 260)}`
  return `${display.title}: ${display.summary || textFor(language, '暂无当前语言的技能说明。', 'No description is available in the selected language.')}`
}

const FALLBACK_LOGS_EN = [
  { time: '14:35:12', tag: 'User', tone: 'user', text: 'Execute a coordinated reconnaissance mission over the target area and cover the entire park.' },
  { time: '14:35:20', tag: 'System', tone: 'system', text: 'Mission parsed: coordinated reconnaissance, target area: park, constraint: full coverage.' },
  { time: '14:35:28', tag: 'System', tone: 'system', text: 'Processing airspace planning and task dispatch. Fleet initialized, link checked, payloads ready.' },
  { time: '14:35:36', tag: 'System', tone: 'system', text: 'Joint plan generated: split the area into 4 subregions and assign UAV-1 to UAV-4 for waypoint patrol and image collection.' },
  { time: '14:35:48', tag: 'Execution', tone: 'exec', text: 'Plan dispatched. Mission execution started.' },
  { time: '14:35:56', tag: 'Execution', tone: 'exec', text: 'UAV-1 reached the start waypoint and began route patrol.' },
  { time: '14:36:08', tag: 'Execution', tone: 'exec', text: 'UAV-2 reached the start waypoint and began route patrol.' },
  { time: '14:36:19', tag: 'Execution', tone: 'exec', text: 'UAV-3 reached the start waypoint and began route patrol.' },
  { time: '14:36:30', tag: 'Execution', tone: 'exec', text: 'UAV-4 reached the start waypoint and began route patrol.' },
  { time: '14:37:19', tag: 'Execution', tone: 'exec', text: 'All UAVs returned and regrouped. Mission complete.' },
  { time: '14:37:25', tag: 'System', tone: 'system', text: 'Mission succeeded. Estimated coverage: 98.7%; image collection complete.' },
  { time: '14:37:32', tag: 'Record', tone: 'notice', text: 'Recorded posture and park-area collaborative reconnaissance; coverage: 98.7%.' },
]

const FALLBACK_LOGS_ZH = [
  { time: '14:35:12', tag: 'User', tone: 'user', text: '请执行对目标区域的协同侦察任务，覆盖整个公园区域。' },
  { time: '14:35:20', tag: 'System', tone: 'system', text: '已解析任务：协同侦察；目标区域：公园；约束：全覆盖。' },
  { time: '14:35:28', tag: 'System', tone: 'system', text: '正在进行空域规划与任务派发。编队初始化完成，链路检查通过，载荷就绪。' },
  { time: '14:35:36', tag: 'System', tone: 'system', text: '已生成联动计划：区域划分为4个子区，分配 UAV-1 至 UAV-4 执行航点巡航与图像采集。' },
  { time: '14:35:48', tag: 'Execution', tone: 'exec', text: '计划已下发，任务开始执行。' },
  { time: '14:35:56', tag: 'Execution', tone: 'exec', text: 'UAV-1 已到达起始航点，开始航线巡航。' },
  { time: '14:36:08', tag: 'Execution', tone: 'exec', text: 'UAV-2 已到达起始航点，开始航线巡航。' },
  { time: '14:36:19', tag: 'Execution', tone: 'exec', text: 'UAV-3 已到达起始航点，开始航线巡航。' },
  { time: '14:36:30', tag: 'Execution', tone: 'exec', text: 'UAV-4 已到达起始航点，开始航线巡航。' },
  { time: '14:37:19', tag: 'Execution', tone: 'exec', text: '所有无人机返航并汇聚，任务完成。' },
  { time: '14:37:25', tag: 'System', tone: 'system', text: '任务执行成功，预计覆盖率 98.7%，图像采集完整。' },
  { time: '14:37:32', tag: 'Record', tone: 'notice', text: '已记录姿态与公园区域协同侦察经验，覆盖率 98.7%。' },
]

const SAMPLE_UAVS = [
  { id: 'UAV-1', x: 13, y: 24 },
  { id: 'UAV-2', x: 38, y: 22 },
  { id: 'UAV-3', x: 41, y: 49 },
  { id: 'UAV-4', x: 13, y: 56 },
]

const DEFAULT_UAV_COUNT = 4
const MAP_WORLD_SCALE = 2 // meters per percent point on the 0-100 map canvas
const MAP_MIN_VIEW_SCALE = 0.25
const MAP_MAX_VIEW_SCALE = 4

const SENSOR_DEFS = [
  { key: 'front', labelEn: 'Sensor - Visible Light - Front', labelZh: '传感器-可见光-前视', shortEn: 'Front', shortZh: '前视', type: 'camera' },
  { key: 'down', labelEn: 'Sensor - Visible Light - Down', labelZh: '传感器-可见光-下视', shortEn: 'Down', shortZh: '下视', type: 'camera' },
  { key: 'rear', labelEn: 'Sensor - Visible Light - Rear', labelZh: '传感器-可见光-后视', shortEn: 'Rear', shortZh: '后视', type: 'camera' },
  { key: 'left', labelEn: 'Sensor - Visible Light - Left', labelZh: '传感器-可见光-左视', shortEn: 'Left', shortZh: '左视', type: 'camera' },
  { key: 'right', labelEn: 'Sensor - Visible Light - Right', labelZh: '传感器-可见光-右视', shortEn: 'Right', shortZh: '右视', type: 'camera' },
  { key: 'infrared', labelEn: 'Sensor - Infrared', labelZh: '传感器-红外', shortEn: 'IR', shortZh: '红外', type: 'imaging' },
  { key: 'thermal', labelEn: 'Sensor - Thermal Infrared', labelZh: '传感器-热红外', shortEn: 'Thermal', shortZh: '热红外', type: 'imaging' },
  { key: 'multispectral', labelEn: 'Sensor - Multispectral', labelZh: '传感器-多光谱', shortEn: 'Multi-Spec', shortZh: '多光谱', type: 'imaging' },
  { key: 'depth', labelEn: 'Sensor - Depth', labelZh: '传感器-深度', shortEn: 'Depth', shortZh: '深度', type: 'range' },
  { key: 'bottom_distance', labelEn: 'Sensor - Bottom Distance', labelZh: '传感器-机腹测距', shortEn: 'AGL', shortZh: '离地', type: 'range' },
  { key: 'lidar', labelEn: 'Sensor - LiDAR', labelZh: '传感器-激光雷达', shortEn: 'LiDAR', shortZh: 'LiDAR', type: 'range' },
  { key: 'imu', labelEn: 'Sensor - IMU', labelZh: '传感器-IMU', shortEn: 'IMU', shortZh: 'IMU', type: 'state' },
  { key: 'gps', labelEn: 'Sensor - GPS', labelZh: '传感器-GPS', shortEn: 'GPS', shortZh: 'GPS', type: 'state' },
  { key: 'barometer', labelEn: 'Sensor - Barometer', labelZh: '传感器-气压计', shortEn: 'Baro', shortZh: '气压', type: 'state' },
]

function localizedSensorOptions(language) {
  return SENSOR_DEFS.map((sensor) => ({
    key: sensor.key,
    label: textFor(language, sensor.labelZh, sensor.labelEn),
    shortLabel: textFor(language, sensor.shortZh, sensor.shortEn),
    type: sensor.type,
  }))
}

const CITY_LINES = [
  { className: 'street street-a' },
  { className: 'street street-b' },
  { className: 'street street-c' },
  { className: 'street street-d' },
  { className: 'street street-e' },
  { className: 'street street-f' },
  { className: 'street street-g' },
  { className: 'street street-h' },
  { className: 'street street-i' },
]

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function niceScaleDistance(targetMeters) {
  if (!Number.isFinite(targetMeters) || targetMeters <= 0) return 50
  const magnitude = 10 ** Math.floor(Math.log10(targetMeters))
  const normalized = targetMeters / magnitude
  const multiplier = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return multiplier * magnitude
}

function formatScaleDistance(meters) {
  if (meters >= 1000) return `${Number((meters / 1000).toFixed(1))} km`
  return `${Number(meters.toFixed(meters < 1 ? 1 : 0))} m`
}

function normalizeUavId(id) {
  const match = String(id || '').match(/UAV[-_\s]*(\d+)/i)
  return match ? `UAV-${Number(match[1])}` : String(id || '').replaceAll('_', '-').toUpperCase()
}

function uavNumber(id) {
  const match = String(id || '').match(/UAV[-_\s]*(\d+)/i)
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER
}

function fallbackRobotId(label) {
  const match = String(label || '').match(/UAV[-_\s]*(\d+)/i)
  return match ? `UAV_${Number(match[1])}` : String(label || '').replaceAll('-', '_')
}

function mapPercentToWorld(xPct, yPct) {
  return {
    n: Number(((50 - yPct) * MAP_WORLD_SCALE).toFixed(1)),
    e: Number(((xPct - 50) * MAP_WORLD_SCALE).toFixed(1)),
  }
}

function worldToMapPercent(n = 0, e = 0) {
  return {
    x: 50 + Number(e || 0) / MAP_WORLD_SCALE,
    y: 50 - Number(n || 0) / MAP_WORLD_SCALE,
  }
}

function taskAreaFromMapPoints(start, end) {
  const x = Math.min(start.xPct, end.xPct)
  const y = Math.min(start.yPct, end.yPct)
  const width = Math.abs(end.xPct - start.xPct)
  const height = Math.abs(end.yPct - start.yPct)
  const first = mapPercentToWorld(x, y)
  const second = mapPercentToWorld(x + width, y + height)
  const northMin = Math.min(first.n, second.n)
  const northMax = Math.max(first.n, second.n)
  const eastMin = Math.min(first.e, second.e)
  const eastMax = Math.max(first.e, second.e)

  return {
    x,
    y,
    width,
    height,
    northMin,
    northMax,
    eastMin,
    eastMax,
    centerNorth: Number(((northMin + northMax) / 2).toFixed(1)),
    centerEast: Number(((eastMin + eastMax) / 2).toFixed(1)),
    widthMeters: Number((eastMax - eastMin).toFixed(1)),
    heightMeters: Number((northMax - northMin).toFixed(1)),
  }
}

function taskAreaSummary(area, language) {
  if (!area) return ''
  const size = `${area.widthMeters.toFixed(0)} x ${area.heightMeters.toFixed(0)} m`
  return textFor(language, `任务区域 ${size}`, `Task area ${size}`)
}

function taskAreaPrompt(area, language) {
  if (!area) return ''
  const bounds = `N=[${area.northMin.toFixed(1)}, ${area.northMax.toFixed(1)}] m; E=[${area.eastMin.toFixed(1)}, ${area.eastMax.toFixed(1)}] m`
  const center = `[${area.centerNorth.toFixed(1)}, ${area.centerEast.toFixed(1)}]`
  const size = `${area.widthMeters.toFixed(1)} x ${area.heightMeters.toFixed(1)} m`
  return textFor(
    language,
    `地图框选任务区域：${bounds}；中心 N/E=${center}；尺寸 E x N=${size}。所有搜索、巡检和编队动作应限制在此边界内。`,
    `Map-selected mission area: ${bounds}; center N/E=${center}; size E x N=${size}. Keep all search, inspection, and formation actions within this boundary.`,
  )
}

function registrationPositionForUav(uav, total = DEFAULT_UAV_COUNT) {
  const index = Math.max(uavNumber(uav?.id) - 1, 0)
  const pos = uav || fallbackUavPosition(index, total)
  const { n, e } = mapPercentToWorld(pos.x, pos.y)
  return [n, e, 0]
}

function defaultAirSimFleetPosition(index) {
  const group = Math.floor(index / 2)
  const side = index % 2
  return [
    10 + group * 30 + side * 10,
    -10 + side * 20,
    -10 - index * 2,
  ]
}

function fleetRequestFromWorld(worldState, count) {
  const robots = worldState?.robots || {}
  return Array.from({ length: count }, (_, index) => {
    const robotId = `UAV_${index + 1}`
    const raw = robots[robotId]?.position
    const position = Array.isArray(raw) && raw.length >= 3
      ? raw.slice(0, 3).map((value) => Number(value))
      : defaultAirSimFleetPosition(index)
    return {
      robot_id: robotId,
      position: position.every(Number.isFinite) ? position : defaultAirSimFleetPosition(index),
    }
  })
}

function fallbackUavPosition(index, total) {
  if (SAMPLE_UAVS[index]) return SAMPLE_UAVS[index]

  const cols = Math.ceil(Math.sqrt(Math.max(total, 1)))
  const row = Math.floor(index / cols)
  const col = index % cols
  const colRatio = cols <= 1 ? 0.5 : col / (cols - 1)
  const rows = Math.ceil(total / cols)
  const rowRatio = rows <= 1 ? 0.5 : row / (rows - 1)

  return {
    id: `UAV-${index + 1}`,
    x: clamp(14 + colRatio * 72 + (row % 2) * 4, 9, 92),
    y: clamp(22 + rowRatio * 58, 12, 84),
  }
}

function textFromLogEntry(entry) {
  if (!entry) return ''
  if (typeof entry === 'string') return entry
  return entry.msg || entry.message || entry.content || entry.reply || JSON.stringify(entry)
}

function isRobotInventoryLog(entry) {
  const text = textFromLogEntry(entry).trim()
  return /^(选中机器人|新增机器人|更新机器人)[:：]\s*UAV[_-]?\d+\b/i.test(text)
    || /^✅?\s*机器人\s+UAV[_-]?\d+\s*\([^)]*\)\s*已加入编队/i.test(text)
}

function extractUavCountFromText(text) {
  let count = 0
  const rangePattern = /UAV[-_\s]*(\d+)\s*(?:~|至|到|-|–|—)\s*UAV[-_\s]*(\d+)/gi
  for (const match of String(text || '').matchAll(rangePattern)) {
    count = Math.max(count, Number(match[1]), Number(match[2]))
  }

  const idPattern = /UAV[-_\s]*(\d+)/gi
  for (const match of String(text || '').matchAll(idPattern)) {
    count = Math.max(count, Number(match[1]))
  }

  const amountPattern = /(\d+)\s*(?:架|台|个)?\s*(?:无人机|UAV|uav)/g
  for (const match of String(text || '').matchAll(amountPattern)) {
    count = Math.max(count, Number(match[1]))
  }

  return count
}

function inferMissionUavCount(logs, chatHistory, missionPrompt) {
  const promptCount = extractUavCountFromText(missionPrompt)
  if (promptCount) return promptCount

  const recentChatText = (chatHistory || [])
    .slice(-6)
    .map((msg) => msg.content || msg.reply || '')
    .join('\n')
  const chatCount = extractUavCountFromText(recentChatText)
  if (chatCount) return chatCount

  const recentLogText = (logs || [])
    .filter((entry) => !isRobotInventoryLog(entry))
    .slice(-20)
    .map(textFromLogEntry)
    .join('\n')
  return extractUavCountFromText(recentLogText)
}

function formatClock(date) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function formatLogTime(ts) {
  const date = ts ? new Date(ts) : new Date()
  if (Number.isNaN(date.getTime())) return '14:37:10'
  return date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function buildUavMarkers(worldState, missionUavCount = 0, desiredUavCount = DEFAULT_UAV_COUNT) {
  const robots = worldState?.robots || {}
  const liveMarkers = Object.entries(robots)
    .filter(([id]) => /^UAV/i.test(id.replace('_', '-')))
    .map(([id, robot]) => {
      const raw = robot?.position || robot?.pose || {}
      const n = Array.isArray(raw) ? raw[0] : raw.north ?? raw.n ?? 0
      const e = Array.isArray(raw) ? raw[1] : raw.east ?? raw.e ?? 0
      const label = normalizeUavId(id)
      const mapPos = worldToMapPercent(n, e)
      return {
        id: label,
        robotId: id,
        canExecute: true,
        isVirtual: false,
        x: mapPos.x,
        y: mapPos.y,
        status: robot?.status || 'idle',
        battery: robot?.battery,
        groundClearance: robot?.ground_clearance,
      }
    })

  const liveByLabel = new Map(liveMarkers.map((uav) => [uav.id, uav]))
  const targetCount = Math.max(
    Math.round(Number(desiredUavCount) || Number(missionUavCount) || DEFAULT_UAV_COUNT),
    1,
  )
  const markers = Array.from({ length: targetCount }, (_, index) => {
    const label = `UAV-${index + 1}`
    if (liveByLabel.has(label)) return liveByLabel.get(label)
    const pos = fallbackUavPosition(index, targetCount)
    return {
      ...pos,
      id: label,
      robotId: fallbackRobotId(label),
      canExecute: false,
      isVirtual: true,
      status: 'idle',
    }
  })

  return markers.sort((a, b) => uavNumber(a.id) - uavNumber(b.id))
}

function cameraImage(sensorCamera, sensorCameras, view = 'front') {
  return sensorCameras?.[view]?.image
    || sensorCameras?.front?.image
    || sensorCameras?.down?.image
    || sensorCamera?.image
    || null
}

function logPresentation(level = 'info') {
  const normalized = String(level || 'info').toLowerCase()
  if (normalized === 'user' || normalized === 'chat') return { tag: 'User', tone: 'user' }
  if (normalized === 'success' || normalized === 'exec' || normalized === 'execution') return { tag: 'Execution', tone: 'exec' }
  if (normalized === 'warn' || normalized === 'warning') return { tag: 'Notice', tone: 'notice' }
  if (normalized === 'error' || normalized === 'alert') return { tag: 'Error', tone: 'alert' }
  if (normalized === 'result') return { tag: 'Result', tone: 'result' }
  return { tag: 'System', tone: 'system' }
}

function explainLogMessage(text, language = DEFAULT_LANGUAGE) {
  const t = makeTranslator(language)
  const raw = String(text || '')
  const trimmed = raw.trim()
  const normalized = trimmed.replace(/^[^\p{L}\p{N}\x5B]+/u, '').trim()

  if (language === 'en') {
    let match = normalized.match(/^系统初始化中/)
    if (match) return 'Initializing system...'

    match = normalized.match(/^世界模型初始化\s*\(([^)]+)\)/)
    if (match) return `World model initialized (${match[1]})`

    match = normalized.match(/^([A-Za-z]+[_-]?\d*)\s*\(([^)]+)\):\s*注册\s*(\d+)\s*个技能/)
    if (match) return `${match[1]} (${match[2]}): registered ${match[3]} skills`

    match = normalized.match(/^技能注册完成[:：]\s*(\d+)\s*台机器人[，,]\s*共\s*(\d+)\s*个技能实例/)
    if (match) return `Skill registration complete: ${match[1]} robot(s), ${match[2]} skill instances`

    match = normalized.match(/^反思引擎\s*\+\s*技能进化模块已加载/)
    if (match) return 'Reflection engine and skill evolution module loaded'

    match = normalized.match(/^经验向量存储已初始化/)
    if (match) return 'Experience vector store initialized'

    match = normalized.match(/^系统初始化完成[，,]\s*等待设备接入/)
    if (match) return 'System initialized; waiting for device access'

    match = normalized.match(/^仿真设备[:：]\s*(.+)$/)
    if (match) return `Simulation device: ${match[1]}`

    match = normalized.match(/^初始化适配器[:：]\s*(.+)$/)
    if (match) return `Initializing adapter: ${match[1]}`

    match = normalized.match(/^客户端连接[:：]\s*(.+)$/)
    if (match) return `Client connected: ${match[1]}`

    match = normalized.match(/^日志系统初始化完成\s*\(级别=([^,，]+)[,，]\s*目录=([^,，]+)[,，]\s*保留=([^)]+)\)$/)
    if (match) return `Logging initialized (level=${match[1]}, directory=${match[2]}, retention=${match[3]})`

    match = normalized.match(/^(?:AeroWeaver|AeroWeaver) 控制台服务启动于\s*(.+)$/)
    if (match) return `AeroWeaver console started at ${match[1]}`

    match = normalized.match(/^存储后端[:：]\s*(.+)$/)
    if (match) return `Storage backend: ${match[1]}`

    match = normalized.match(/^软技能管理器[:：]\s*加载\s*(\d+)\s*个文档/)
    if (match) return `Advanced Skill manager loaded ${match[1]} documents`

    match = normalized.match(/^sentence-transformers 不可用[，,]\s*降级到 TF-IDF[:：]\s*(.+)$/)
    if (match) return `sentence-transformers unavailable; falling back to TF-IDF: ${match[1]}`

    match = normalized.match(/^Embedding 后端降级[:：]\s*手写 TF-IDF（语义精度有限）/)
    if (match) return 'Embedding backend downgraded to built-in TF-IDF (limited semantic accuracy)'

    match = normalized.match(/^遥测同步线程已启动[（(](\d+\s*Hz)\s*刷新位置\/电量\/状态[）)]/)
    if (match) return `Telemetry synchronization started (${match[1]} position, battery, and status refresh)`

    match = normalized.match(/^被动感知引擎已启动\s*[（(](\d+)\s*s\/次[）)]/)
    if (match) return `Passive perception engine started (every ${match[1]} s)`
  } else {
    let match = normalized.match(/^Connecting to AirSim adapter\s*\(([^)]+)\)\.{0,3}$/i)
    if (match) return `正在连接 AirSim 适配器（${match[1]}）...`

    match = normalized.match(/^Adapter connected[:：]\s*(.+)$/i)
    if (match) return `适配器已连接：${match[1]}`

    match = normalized.match(/^AirSim camera stream started;\s*use Sensor\/visible-light to view frames$/i)
    if (match) return 'AirSim 相机流已启动；请通过“传感器/可见光”查看画面'
  }

  if (/Adapter/i.test(raw) && /(?:\?{2,}|reconnect|reconnecting|retry)/i.test(raw)) {
    const attempt = raw.match(/(\d+)/)
    return attempt
      ? t(`后端与仿真器连接中断，正在自动重连（第 ${attempt[1]} 次）`, `Backend connection to the simulator was interrupted; reconnecting automatically (attempt ${attempt[1]})`)
      : t('后端与仿真器连接中断，正在自动重连', 'Backend connection to the simulator was interrupted; reconnecting automatically')
  }
  if (/AirSim connect failed/i.test(raw) && /Vehicle API/i.test(raw)) {
    return t('AirSim 已响应，但无人机名称不匹配。请检查 AIRSIM_VEHICLE_NAME。', 'AirSim responded, but the vehicle name does not match. Check AIRSIM_VEHICLE_NAME.')
  }
  if (/AirSim connect failed/i.test(raw) && /Cannot connect/i.test(raw)) {
    return t('无法连接 AirSim 服务。请确认 AirSim 已运行且 41451 端口可访问。', 'Cannot connect to the AirSim service. Confirm AirSim is running and port 41451 is open.')
  }
  return raw
}

function logEntryText(entry, language = DEFAULT_LANGUAGE) {
  const text = typeof entry === 'object' ? entry.msg || entry.message || JSON.stringify(entry) : String(entry)
  return explainLogMessage(text, language)
}


function isRobotSelectionLog(entry) {
  const level = typeof entry === 'object' ? String(entry.level || 'info').toLowerCase() : 'info'
  if (level === 'user' || level === 'chat') return false
  return /^选中机器人[:：]\s*UAV[_-]?\d+\b/i.test(logEntryText(entry).trim())
}

function buildTimeline(logs, chatHistory, language = DEFAULT_LANGUAGE) {
  const timestampOf = (value) => {
    const timestamp = value ? new Date(value).getTime() : Date.now()
    return Number.isFinite(timestamp) ? timestamp : Date.now()
  }
  const fromChat = (chatHistory || []).slice(-10).map((msg) => {
    const isUser = msg.role === 'user'
    const isResult = msg.intent === 'RESULT'
    return {
      timestamp: timestampOf(msg.ts || msg.time),
      time: formatLogTime(msg.ts || msg.time),
      tag: isUser ? 'User' : isResult ? 'Result' : 'Assistant',
      tone: isUser ? 'user' : isResult ? 'result' : 'assistant',
      text: msg.content || '',
      source: 'chat',
    }
  })

  const fromLogs = (logs || []).filter((entry) => !isRobotSelectionLog(entry)).slice(-16).map((entry) => {
    const text = logEntryText(entry, language)
    const level = typeof entry === 'object' ? entry.level : 'info'
    const presentation = logPresentation(level)
    return {
      timestamp: timestampOf(entry.ts || entry.time),
      time: formatLogTime(entry.ts || entry.time),
      tag: presentation.tag,
      tone: presentation.tone,
      text,
      source: 'log',
    }
  })

  const merged = [...fromChat, ...fromLogs]
    .filter((item) => item.text)
    .sort((left, right) => left.timestamp - right.timestamp || (left.source === 'chat' ? -1 : 1))
  const unique = merged.filter((item, index, rows) => (
    rows.findIndex((row) => `${row.time}|${row.text}` === `${item.time}|${item.text}`) === index
  ))
  return unique.length ? unique.slice(-16) : (language === 'zh' ? FALLBACK_LOGS_ZH : FALLBACK_LOGS_EN)
}

function LanguageToggle({ language, onChange }) {
  return (
    <div className="language-toggle" role="group" aria-label="Language">
      <button className={language === 'zh' ? 'active' : ''} onClick={() => onChange('zh')}>中文</button>
      <button className={language === 'en' ? 'active' : ''} onClick={() => onChange('en')}>EN</button>
    </div>
  )
}

function MissionHeader({ connected, systemStatus, now, language, onLanguageChange, onInit, onStop }) {
  const t = (zh, en) => textFor(language, zh, en)
  return (
    <header className="mission-header">
      <div className="brand-lockup">
        <div className="brand-mark" aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
        </div>
        <div>
          <h1>AeroWeaver</h1>
          <p>{t('多无人机技能协同交互系统', 'Multi-UAV Skill Orchestration System')}</p>
        </div>
      </div>

      <div className="header-actions">
        {!systemStatus.initialized && (
          <button className="mini-action" onClick={onInit}>{t('初始化', 'Initialize')}</button>
        )}
        {systemStatus.is_executing && (
          <button className="mini-action danger" onClick={onStop}>{t('打断', 'Abort')}</button>
        )}
        <LanguageToggle language={language} onChange={onLanguageChange} />
        <span className={`link-state ${connected ? 'online' : 'offline'}`}>
          {connected ? t('链路在线', 'Link Online') : t('链路离线', 'Link Offline')}
        </span>
        <span className="header-time">
          <span className="clock-icon" />
          {formatClock(now)}
        </span>
      </div>
    </header>
  )
}

function AirSimRelayScene({ language, sceneImage, sceneImageUrl }) {
  const t = makeTranslator(language)
  const [status, setStatus] = useState('connecting')
  const fallbackSrc = sceneImageUrl || (sceneImage ? `data:image/jpeg;base64,${sceneImage}` : '')

  useEffect(() => {
    setStatus('connecting')
  }, [sceneImageUrl])

  const label = {
    connecting: t('正在连接 AirSim 俯视场景', 'Connecting to AirSim aerial view'),
    playing: t('AirSim 环境俯视', 'AirSim Aerial View'),
    error: t('AirSim 俯视流已断开', 'AirSim aerial feed disconnected'),
  }[status] || t('正在连接 AirSim 俯视场景', 'Connecting to AirSim aerial view')

  return (
    <div className="scene-map-feed">
      {fallbackSrc && (
        <img
          src={fallbackSrc}
          alt={t('AirSim 全局场景', 'AirSim global scene')}
          onLoad={() => setStatus('playing')}
          onError={() => setStatus('error')}
        />
      )}
      {!fallbackSrc && (
        <span className="scene-map-placeholder">{t('等待场景画面', 'Waiting for scene feed')}</span>
      )}
      <span className={`scene-stream-status ${status === 'playing' ? 'online' : ''}`}>
        <i />
        {label}
      </span>
    </div>
  )
}

function MissionMap({
  uavs,
  selectedUavId,
  activeFpv,
  activeSensor,
  language,
  sensorOptions,
  showFpv,
  showPayloadMenu,
  skillPanelOpen,
  mapTools,
  layerOptions,
  measurementPoints,
  selectedTaskArea,
  desiredUavCount,
  fleetSync,
  onSelectUav,
  onOpenSensorMenu,
  onSelectSensor,
  onOpenSkillPanel,
  onOpenTracks,
  onOpenCockpit,
  onToggleMapTool,
  onToggleLayer,
  onApplyUavCount,
  onClearMeasurement,
  onMeasurePoint,
  onSelectTaskArea,
  onClearTaskArea,
  onClosePayloadMenu,
  onCloseFpv,
  onExpandFpv,
  mapPickRequest,
  onMapPick,
  onCancelMapPick,
  sceneMode,
  onSetSceneMode,
  sceneImage,
  sceneImageUrl,
  fpvImage,
  trajectorySeries,
  trajectoryRecording,
  trajectorySampleCount,
  tracksWorkspaceOpen,
}) {
  const t = makeTranslator(language)
  const selectedUav = uavs.find((uav) => uav.id === selectedUavId)
  const visibleUavCount = uavs.length
  const activeFpvLabel = sensorOptions.find((sensor) => sensor.key === activeFpv?.sensor)?.label
  const measuredDistance = measurementPoints.length >= 2
    ? Math.hypot(
      measurementPoints[1].n - measurementPoints[0].n,
      measurementPoints[1].e - measurementPoints[0].e,
    )
    : null

  const mapRef = useRef(null)
  const dragRef = useRef(null)
  const suppressClickRef = useRef(false)
  const [viewport, setViewport] = useState({ scale: 1, x: 0, y: 0 })
  const [mapSize, setMapSize] = useState({ width: 0, height: 0 })
  const [areaDraft, setAreaDraft] = useState(null)
  const [isPanning, setIsPanning] = useState(false)

  const constrainViewport = useCallback((candidate) => ({
    scale: clamp(candidate.scale, MAP_MIN_VIEW_SCALE, MAP_MAX_VIEW_SCALE),
    x: Number.isFinite(candidate.x) ? candidate.x : 0,
    y: Number.isFinite(candidate.y) ? candidate.y : 0,
  }), [])

  const screenToMapPoint = useCallback((clientX, clientY, activeViewport = viewport) => {
    const rect = mapRef.current?.getBoundingClientRect()
    if (!rect?.width || !rect?.height) return null
    const xPct = ((clientX - rect.left - activeViewport.x) / (rect.width * activeViewport.scale)) * 100
    const yPct = ((clientY - rect.top - activeViewport.y) / (rect.height * activeViewport.scale)) * 100
    return { xPct, yPct, ...mapPercentToWorld(xPct, yPct) }
  }, [viewport])

  const displayTaskArea = useMemo(
    () => areaDraft ? taskAreaFromMapPoints(areaDraft.start, areaDraft.current) : selectedTaskArea,
    [areaDraft, selectedTaskArea],
  )
  const worldStyle = useMemo(
    () => ({ transform: `translate3d(${viewport.x}px, ${viewport.y}px, 0) scale(${viewport.scale})` }),
    [viewport],
  )

  useEffect(() => {
    const mapNode = mapRef.current
    if (!mapNode) return undefined

    const updateMapSize = () => {
      const rect = mapNode.getBoundingClientRect()
      if (rect.width && rect.height) setMapSize({ width: rect.width, height: rect.height })
    }
    updateMapSize()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updateMapSize)
    observer?.observe(mapNode)
    window.addEventListener('resize', updateMapSize)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', updateMapSize)
    }
  }, [])

  const zoomMap = useCallback((factor, anchorClientX, anchorClientY) => {
    const rect = mapRef.current?.getBoundingClientRect()
    if (!rect?.width || !rect?.height) return
    const anchorX = Number.isFinite(anchorClientX) ? anchorClientX - rect.left : rect.width / 2
    const anchorY = Number.isFinite(anchorClientY) ? anchorClientY - rect.top : rect.height / 2
    setViewport((previous) => {
      const scale = clamp(previous.scale * factor, MAP_MIN_VIEW_SCALE, MAP_MAX_VIEW_SCALE)
      const worldX = (anchorX - previous.x) / previous.scale
      const worldY = (anchorY - previous.y) / previous.scale
      return constrainViewport({
        scale,
        x: anchorX - worldX * scale,
        y: anchorY - worldY * scale,
      }, rect)
    })
  }, [constrainViewport])

  const scaleBar = useMemo(() => {
    const pixelsPerMeter = mapSize.width * viewport.scale / (100 * MAP_WORLD_SCALE)
    const meters = niceScaleDistance(96 / Math.max(pixelsPerMeter, 0.001))
    return { meters, pixels: meters * pixelsPerMeter }
  }, [mapSize.width, viewport.scale])

  const resetMapViewport = useCallback(() => {
    setViewport({ scale: 1, x: 0, y: 0 })
  }, [])

  const handleWheel = (event) => {
    if (event.target.closest('button, input, label, .map-popover, .uav-payload-menu, .fpv-window')) return
    event.preventDefault()
    zoomMap(event.deltaY < 0 ? 1.16 : 1 / 1.16, event.clientX, event.clientY)
  }

  const handlePointerDown = (event) => {
    if (event.target.closest('button, input, label, .map-popover, .uav-payload-menu, .fpv-window')) return
    if (event.button !== 0 && event.button !== 1) return

    if (mapTools.area) {
      const point = screenToMapPoint(event.clientX, event.clientY)
      if (!point) return
      event.preventDefault()
      dragRef.current = { type: 'area', pointerId: event.pointerId, start: point }
      setAreaDraft({ start: point, current: point })
      event.currentTarget.setPointerCapture?.(event.pointerId)
      return
    }

    if (mapPickRequest || mapTools.measure) return
    event.preventDefault()
    dragRef.current = {
      type: 'pan',
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startViewport: viewport,
      moved: false,
    }
    setIsPanning(true)
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }

  const handlePointerMove = (event) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return

    if (drag.type === 'area') {
      const point = screenToMapPoint(event.clientX, event.clientY)
      if (point) setAreaDraft({ start: drag.start, current: point })
      return
    }

    const rect = mapRef.current?.getBoundingClientRect()
    if (!rect?.width || !rect?.height) return
    const dx = event.clientX - drag.startClientX
    const dy = event.clientY - drag.startClientY
    if (Math.hypot(dx, dy) > 3) drag.moved = true
    setViewport(constrainViewport({
      ...drag.startViewport,
      x: drag.startViewport.x + dx,
      y: drag.startViewport.y + dy,
    }, rect))
  }

  const finishPointerInteraction = (event, cancelled = false) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return

    if (drag.type === 'area') {
      const end = screenToMapPoint(event.clientX, event.clientY)
      const area = end ? taskAreaFromMapPoints(drag.start, end) : null
      setAreaDraft(null)
      if (!cancelled && area && area.width >= 1 && area.height >= 1) onSelectTaskArea(area)
      suppressClickRef.current = true
    } else if (drag.moved) {
      suppressClickRef.current = true
    }

    dragRef.current = null
    setIsPanning(false)
    event.currentTarget.releasePointerCapture?.(event.pointerId)
  }

  const handleMapClick = (event) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    if (event.target.closest('button, input, label, .map-popover, .uav-payload-menu, .fpv-window')) return

    const point = screenToMapPoint(event.clientX, event.clientY)
    if (!point) return

    if (mapPickRequest) {
      onMapPick(point)
      return
    }

    if (mapTools.measure) {
      onMeasurePoint(point)
      return
    }

    if (showPayloadMenu) onClosePayloadMenu()
  }

  return (
    <section className="mission-panel map-panel">
      <div className="panel-title-row">
        <h2>{t('无人机协同行动实时画面', 'Real-Time UAV Operations')}</h2>
        <button className="panel-icon" aria-label={t('全屏地图', 'Fullscreen map')}>□</button>
      </div>

      <div className="map-stage">
        <div className="map-mode-bar">
          <button className={!sceneMode ? 'active' : ''} onClick={() => onSetSceneMode(false)}>{t('战术图', 'Tactical')}</button>
          <button className={sceneMode ? 'active' : ''} onClick={() => onSetSceneMode(true)}>{t('环境俯视', 'Aerial View')}</button>
          <span className="uav-count-chip">UAV × {uavs.length}</span>
        </div>

        <div className="map-tool-strip">
          <button className={showPayloadMenu || showFpv ? 'active' : ''} onClick={() => onOpenSensorMenu(selectedUav || uavs[0])}>{t('传感器', 'Sensors')}</button>
          <button onClick={() => onOpenCockpit(activeFpv?.sensor || 'front')}>{t('驾驶舱', 'Cockpit')}</button>
          <button className={skillPanelOpen ? 'active' : ''} onClick={() => onOpenSkillPanel(selectedUav || uavs[0])}>{t('可视化Skill', 'Visualize Skill')}</button>
          <button className={layerOptions.routes || tracksWorkspaceOpen ? 'active' : ''} onClick={onOpenTracks}>{t('航迹', 'Tracks')}</button>
          <button className={mapTools.measure ? 'active' : ''} onClick={() => onToggleMapTool('measure')}>{t('测距', 'Measure')}</button>
          <button className={mapTools.area ? 'active' : ''} onClick={() => onToggleMapTool('area')} title={t('框选任务区域', 'Select mission area')}>{t('框选区域', 'Select Area')}</button>
          <button className={mapTools.layers ? 'active' : ''} onClick={() => onToggleMapTool('layers')}>{t('图层', 'Layers')}</button>
          <button className={mapTools.settings ? 'active' : ''} onClick={() => onToggleMapTool('settings')}>{t('设置', 'Settings')}</button>
        </div>

        <div
          ref={mapRef}
          className={`city-map ${sceneMode ? 'scene-mode' : ''} ${mapPickRequest || mapTools.measure || mapTools.area ? 'picking' : ''} ${!mapPickRequest && !mapTools.measure && !mapTools.area ? 'can-pan' : ''} ${isPanning ? 'is-panning' : ''} ${!layerOptions.grid ? 'no-grid' : ''} ${!layerOptions.roads ? 'no-roads' : ''}`}
          onClick={handleMapClick}
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={(event) => finishPointerInteraction(event)}
          onPointerCancel={(event) => finishPointerInteraction(event, true)}
        >
          <div className="map-world map-world-base" style={worldStyle}>
            <div className="map-extent-grid" aria-hidden="true" />
            {sceneMode && (
              <AirSimRelayScene
                language={language}
                sceneImage={sceneImage}
                sceneImageUrl={sceneImageUrl}
              />
            )}
            <div className="map-noise" />
            {CITY_LINES.map((line) => <span key={line.className} className={line.className} />)}
          </div>

          {mapPickRequest && (
            <div className="map-pick-hint">
              <span>{t(`地图取点：点击地图填充 ${mapPickRequest.paramKey}`, `Map pick: click the map to fill ${mapPickRequest.paramKey}`)}</span>
              <button onClick={onCancelMapPick}>{t('取消', 'Cancel')}</button>
            </div>
          )}

          {mapTools.measure && !mapPickRequest && (
            <div className="map-pick-hint measure">
              <span>{measurementPoints.length < 1 ? t('测距：点击起点', 'Measure: click start') : measurementPoints.length < 2 ? t('测距：点击终点', 'Measure: click end') : t(`距离 ${measuredDistance.toFixed(1)} m`, `Distance ${measuredDistance.toFixed(1)} m`)}</span>
              <button onClick={onClearMeasurement}>{t('清除', 'Clear')}</button>
            </div>
          )}

          {mapTools.area && !mapPickRequest && (
            <div className="map-pick-hint area">
              <span>{t('拖动框选任务区域', 'Drag to select mission area')}</span>
              <button onClick={() => {
                setAreaDraft(null)
                onToggleMapTool('area')
              }}>{t('取消', 'Cancel')}</button>
            </div>
          )}

          {selectedTaskArea && !mapTools.area && !mapPickRequest && (
            <div className="map-area-status">
              <span>{taskAreaSummary(selectedTaskArea, language)}</span>
              <button type="button" onClick={onClearTaskArea} aria-label={t('清除任务区域', 'Clear task area')}>×</button>
            </div>
          )}

          {mapTools.layers && (
            <MapLayerPanel language={language} layerOptions={layerOptions} onToggleLayer={onToggleLayer} />
          )}

          {mapTools.settings && (
            <MapSettingsPanel
              language={language}
              desiredUavCount={desiredUavCount}
              liveUavCount={visibleUavCount}
              fleetSync={fleetSync}
              onApplyUavCount={onApplyUavCount}
            />
          )}

          <div className="map-world map-world-objects" style={worldStyle}>
          <svg className="route-layer" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <g className="map-origin-marker">
              <title>{t('\u539f\u70b9 (0,0)', 'Origin (0,0)')}</title>
              <circle cx="50" cy="50" r="0.62" />
            </g>
            {displayTaskArea && (
              <>
                <rect
                  className={`task-area-rect ${areaDraft ? 'draft' : ''}`}
                  x={displayTaskArea.x}
                  y={displayTaskArea.y}
                  width={displayTaskArea.width}
                  height={displayTaskArea.height}
                />
                <text className="task-area-label" x={displayTaskArea.x + 1.2} y={Math.max(displayTaskArea.y - 1.2, 3)}>
                  {t('任务区域', 'TASK AREA')}
                </text>
              </>
            )}
            {layerOptions.routes && trajectorySeries.map((series) => {
              const mapPoints = series.samples.map((sample) => ({
                ...worldToMapPercent(sample.north_m, sample.east_m),
                sample,
              }))
              const firstPoint = mapPoints[0]
              const lastPoint = mapPoints[mapPoints.length - 1]
              const labelOffset = {
                'UAV-1': { x: 1.8, y: -5.0 },
                'UAV-2': { x: 1.8, y: -0.5 },
                'UAV-3': { x: 1.8, y: 4.0 },
                'UAV-4': { x: 1.8, y: -2.4 },
              }[series.vehicleId] || { x: 1.6, y: -1.5 }
              return (
                <g key={series.vehicleId} className="recorded-track-group">
                  <polyline
                    className="recorded-track"
                    points={mapPoints.map((point) => `${point.x},${point.y}`).join(' ')}
                    style={{ stroke: series.color }}
                  />
                  {firstPoint && <circle className="track-start" cx={firstPoint.x} cy={firstPoint.y} r="0.8" style={{ fill: series.color }} />}
                  {lastPoint && (
                    <>
                      <circle className="track-end" cx={lastPoint.x} cy={lastPoint.y} r="1.15" style={{ stroke: series.color }} />
                      <text className="track-end-label" x={lastPoint.x + labelOffset.x} y={lastPoint.y + labelOffset.y}>{series.vehicleId}</text>
                    </>
                  )}
                </g>
              )
            })}
            {measurementPoints.length >= 1 && (
              <circle className="measure-point" cx={measurementPoints[0].xPct} cy={measurementPoints[0].yPct} r="1.2" />
            )}
            {measurementPoints.length >= 2 && (
              <>
                <circle className="measure-point" cx={measurementPoints[1].xPct} cy={measurementPoints[1].yPct} r="1.2" />
                <line
                  className="measure-line"
                  x1={measurementPoints[0].xPct}
                  y1={measurementPoints[0].yPct}
                  x2={measurementPoints[1].xPct}
                  y2={measurementPoints[1].yPct}
                />
                <text
                  className="measure-label"
                  x={(measurementPoints[0].xPct + measurementPoints[1].xPct) / 2}
                  y={(measurementPoints[0].yPct + measurementPoints[1].yPct) / 2 - 2}
                >
                  {measuredDistance.toFixed(1)}m
                </text>
              </>
            )}
          </svg>

          {layerOptions.routes && (
            <div className={`track-map-status ${trajectoryRecording ? 'recording' : ''}`}>
              <i />
              <span>{trajectoryRecording ? t('记录中', 'Recording') : t('航迹', 'Tracks')}</span>
              <strong>{trajectorySampleCount}</strong>
            </div>
          )}

          {uavs.map((uav) => (
            <button
              key={uav.id}
              type="button"
              className={`uav-marker ${uav.id === selectedUavId ? 'selected' : ''} ${activeFpv?.uavId === uav.id && showFpv ? 'has-fpv' : ''} ${uav.canExecute ? '' : 'virtual'}`}
              style={{ left: `${uav.x}%`, top: `${uav.y}%`, '--uav-color': trajectoryColor(uav.id) }}
              onClick={() => onSelectUav(uav)}
              aria-label={t(`选择 ${uav.id}`, `Select ${uav.id}`)}
            >
              {layerOptions.labels && <span className="uav-label">{uav.id}</span>}
              <span className="uav-dot" />
            </button>
          ))}
          </div>

          <div className="map-scale-indicator" aria-label={t(`比例尺 ${formatScaleDistance(scaleBar.meters)}`, `Map scale ${formatScaleDistance(scaleBar.meters)}`)}>
            <span className="map-scale-rule" style={{ width: `${scaleBar.pixels}px` }} />
            <strong>{formatScaleDistance(scaleBar.meters)}</strong>
          </div>

          <div className="map-navigation" aria-label={t('地图视图控制', 'Map view controls')}>
            <button type="button" onClick={() => zoomMap(1 / 1.25)} aria-label={t('缩小地图', 'Zoom out')} title={t('缩小', 'Zoom out')}>−</button>
            <button type="button" className="map-zoom-level" onClick={resetMapViewport} title={t('重置地图视图', 'Reset map view')}>{Math.round(viewport.scale * 100)}%</button>
            <button type="button" onClick={() => zoomMap(1.25)} aria-label={t('放大地图', 'Zoom in')} title={t('放大', 'Zoom in')}>+</button>
          </div>

          <div className={`uav-legend ${layerOptions.labels ? '' : 'compact'}`}>
            {uavs.map((uav) => (
              <button
                key={uav.id}
                type="button"
                className={`uav-legend-item ${uav.id === selectedUavId ? 'active' : ''} ${uav.canExecute ? '' : 'virtual'}`}
                style={{ '--uav-color': trajectoryColor(uav.id) }}
                onClick={() => onSelectUav(uav)}
                aria-label={t(`选择 ${uav.id}`, `Select ${uav.id}`)}
                title={t(`选择 ${uav.id}`, `Select ${uav.id}`)}
              >
                <span aria-hidden="true" />
                <strong>{uav.id}</strong>
              </button>
            ))}
          </div>

          {showPayloadMenu && selectedUav && (
            <UavPayloadMenu
              uav={selectedUav}
              activeFpv={activeFpv}
              activeSensor={activeSensor}
              showFpv={showFpv}
              language={language}
              sensorOptions={sensorOptions}
              onSelectSensor={(sensor) => onSelectSensor(selectedUav, sensor)}
              onOpenSkillPanel={() => onOpenSkillPanel(selectedUav)}
              onClose={onClosePayloadMenu}
            />
          )}

          {showFpv && (
            <FpvWindow image={fpvImage} activeFpv={activeFpv} sensorLabel={activeFpvLabel} language={language} onClose={onCloseFpv} onExpand={onExpandFpv} />
          )}
        </div>
      </div>
    </section>
  )
}

function MapLayerPanel({ language, layerOptions, onToggleLayer }) {
  const t = makeTranslator(language)
  const layers = [
    { key: 'grid', label: t('坐标网格', 'Coordinate Grid') },
    { key: 'roads', label: t('地图线框', 'Map Lines') },
    { key: 'routes', label: t('历史航迹', 'Recorded Tracks') },
    { key: 'labels', label: t('无人机标签', 'UAV Labels') },
  ]

  return (
    <div className="map-popover layer-popover">
      <div className="map-popover-title">{t('图层', 'Layers')}</div>
      {layers.map((layer) => (
        <label key={layer.key} className="map-check-row">
          <input
            type="checkbox"
            checked={layerOptions[layer.key]}
            onChange={() => onToggleLayer(layer.key)}
          />
          <span>{layer.label}</span>
        </label>
      ))}
    </div>
  )
}

function MapSettingsPanel({ language, desiredUavCount, liveUavCount, fleetSync, onApplyUavCount }) {
  const t = makeTranslator(language)
  const [draftCount, setDraftCount] = useState(desiredUavCount)

  useEffect(() => {
    setDraftCount(desiredUavCount)
  }, [desiredUavCount])

  const setCount = (nextCount) => setDraftCount(Math.round(clamp(Number(nextCount) || 1, 1, MAX_UAV_COUNT)))

  return (
    <div className="map-popover settings-popover">
      <div className="map-popover-title">{t('编队设置', 'Fleet Settings')}</div>
      <div className="setting-row">
        <span>{t('无人机数量', 'Fleet Size')}</span>
        <div className="stepper">
          <button onClick={() => setCount(draftCount - 1)}>-</button>
          <input
            value={draftCount}
            onChange={(event) => setCount(event.target.value)}
            aria-label={t('无人机数量', 'UAV count')}
          />
          <button onClick={() => setCount(draftCount + 1)}>+</button>
        </div>
      </div>
      <div className="setting-note">{t(
        `当前显示 ${liveUavCount} 架无人机。确认后从 10 架备用池中激活 UAV_1 至 UAV_${draftCount}；其余无人机停放到场地外，不在界面中显示。`,
        `Currently showing ${liveUavCount} UAVs. Applying activates UAV_1 through UAV_${draftCount} from the 10-vehicle pool; reserve UAVs are parked outside the site and hidden from the interface.`,
      )}</div>
      {fleetSync?.message && (
        <div className={`setting-note ${fleetSync.status === 'error' ? 'error' : ''}`}>
          {fleetSync.message}
        </div>
      )}
      <button
        className="setting-apply"
        disabled={fleetSync?.status === 'syncing'}
        onClick={() => onApplyUavCount(draftCount)}
      >
        {fleetSync?.status === 'syncing' ? t('正在同步 AirSim...', 'Synchronizing AirSim...') : t('同步编队', 'Sync Fleet')}
      </button>
    </div>
  )
}

function UavPayloadMenu({
  uav,
  activeFpv,
  activeSensor,
  showFpv,
  language,
  sensorOptions,
  onSelectSensor,
  onOpenSkillPanel,
  onClose,
}) {
  const t = makeTranslator(language)
  const anchorX = uav.x <= 34 ? 'right' : uav.x >= 66 ? 'left' : 'center'
  const anchorY = uav.y <= 28 ? 'down' : uav.y >= 70 ? 'up' : 'middle'
  const menuLeft = anchorX === 'right'
    ? clamp(uav.x + 4, 5, 42)
    : anchorX === 'left'
      ? clamp(uav.x - 4, 58, 96)
      : clamp(uav.x, 28, 72)
  const menuTop = anchorY === 'down'
    ? clamp(uav.y + 5, 8, 30)
    : anchorY === 'up'
      ? clamp(uav.y - 5, 64, 96)
      : clamp(uav.y, 32, 68)
  const activeKey = activeSensor?.uavId === uav.id ? activeSensor.sensor : null
  const cameraOptions = sensorOptions.filter((sensor) => sensor.type === 'camera')
  const payloadOptions = sensorOptions.filter((sensor) => sensor.type !== 'camera')

  return (
    <div
      className={`uav-payload-menu anchor-${anchorX} anchor-${anchorY}`}
      style={{ left: `${menuLeft}%`, top: `${menuTop}%` }}
    >
      <div className="payload-title">
        <strong>{uav.id}</strong>
        <button onClick={onClose} aria-label={t('关闭无人机载荷菜单', 'Close UAV payload menu')}>×</button>
      </div>
      <div className="payload-meta">
        <span className={`payload-state ${uav.status || 'idle'}`}>{statusText(uav.status, language)}</span>
        {uav.battery != null && <span>{t('电量', 'Battery')} {Math.round(uav.battery)}%</span>}
        {uav.groundClearance != null && Number.isFinite(Number(uav.groundClearance)) && (
          <span>{t('离地', 'AGL')} {Number(uav.groundClearance).toFixed(1)} m</span>
        )}
        {!uav.canExecute && <span>{t('待注册', 'Pending')}</span>}
      </div>
      <div className="payload-actions">
        <div className="payload-section-title">{t('可见光 FPV', 'Visible-Light FPV')}</div>
        <div className="sensor-grid">
          {cameraOptions.map((sensor) => (
            <button
              key={sensor.key}
              className={`payload-button sensor-button ${activeKey === sensor.key ? 'active' : ''}`}
              onClick={() => onSelectSensor(sensor)}
            >
              {sensor.shortLabel}
            </button>
          ))}
        </div>
        <div className="payload-section-title">{t('其他载荷', 'Other Payloads')}</div>
        <div className="sensor-grid payload-sensor-grid">
          {payloadOptions.map((sensor) => (
            <button
              key={sensor.key}
              className={`payload-button sensor-button ${activeKey === sensor.key ? 'active' : ''}`}
              onClick={() => onSelectSensor(sensor)}
            >
              {sensor.shortLabel}
            </button>
          ))}
        </div>
        <button className="payload-button" onClick={onOpenSkillPanel} disabled={!uav.canExecute}>
          {t('可视化Skill', 'Visualize Skill')}
        </button>
      </div>
      {!uav.canExecute && <div className="payload-warning">{t('请在设置中确认无人机数量，或等待后端注册该无人机。', 'Apply the UAV count in Settings, or wait for the backend to register this UAV.')}</div>}
    </div>
  )
}

function FpvWindow({ image, activeFpv, sensorLabel, language, onClose, onExpand }) {
  const t = makeTranslator(language)
  const streamView = ['front', 'rear', 'left', 'right', 'down'].includes(activeFpv?.sensor)
    ? activeFpv.sensor
    : 'front'
  const streamUrl = activeFpv
    ? `${API_BASE}/api/sensor/camera/stream?view=${encodeURIComponent(streamView)}&robot_id=${encodeURIComponent(activeFpv.uavId || 'UAV_1')}`
    : ''
  return (
    <div className="fpv-window">
      <div className="fpv-titlebar">
        <span>{activeFpv?.uavId || 'UAV'} · FPV · {sensorLabel || activeFpv?.label || t('可见光传感器', 'Visible-Light Sensor')}</span>
        <div className="fpv-actions">
          <button onClick={onExpand} aria-label={t('打开驾驶舱', 'Open cockpit')}>□</button>
          <button onClick={onClose} aria-label={t('关闭 FPV', 'Close FPV')}>×</button>
        </div>
      </div>

      <div className="fpv-feed">
        {(streamUrl || image) ? (
          <img src={streamUrl || `data:image/jpeg;base64,${image}`} alt={t('FPV 可见光画面', 'FPV visible-light feed')} />
        ) : (
          <div className="fpv-placeholder">
            <span className="sky-layer" />
            <span className="hill-layer" />
            <span className="city-layer" />
            <span className="ground-layer" />
          </div>
        )}
      </div>

      <div className="fpv-status">
        <span><i />RX&nbsp;&nbsp;GOOD</span>
        <span>00:02:02</span>
      </div>
    </div>
  )
}

function DialogueLog({ timeline, language }) {
  const t = makeTranslator(language)
  return (
    <section className="mission-panel log-panel">
      <div className="panel-title-row">
        <h2>{t('对话与执行日志', 'Dialogue & Execution Log')}</h2>
      </div>
      <div className="dialogue-list">
        {timeline.map((item, index) => (
          <div className={`dialogue-row ${item.tone}`} key={`${item.time}-${index}`}>
            <span className="log-time">[{item.time}]</span>
            <span className="log-tag">[{logTagText(item.tag, language)}]</span>
            <span className="log-text">{item.text}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

function LogsWorkspace({ timeline, language }) {
  const t = makeTranslator(language)
  const [serverLogs, setServerLogs] = useState([])
  const [loading, setLoading] = useState(false)

  const refreshLogs = () => {
    setLoading(true)
    fetch(`${API_BASE}/api/logs`)
      .then((r) => r.json())
      .then((data) => setServerLogs(Array.isArray(data) ? data : []))
      .catch(() => setServerLogs([]))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    refreshLogs()
  }, [])

  const filteredServerLogs = serverLogs.filter((entry) => !isRobotSelectionLog(entry))
  const logRows = filteredServerLogs.length
    ? filteredServerLogs.slice(-14).map((entry) => {
      const presentation = logPresentation(entry.level)
      return {
        time: formatLogTime(entry.ts || entry.time),
        tag: presentation.tag,
        tone: presentation.tone,
        text: logEntryText(entry, language),
      }
    })
    : timeline
  const liveRows = (timeline === FALLBACK_LOGS_EN || timeline === FALLBACK_LOGS_ZH) ? [] : timeline
  const mergedRows = filteredServerLogs.length ? [...logRows, ...liveRows] : logRows
  const displayRows = mergedRows.filter((item, index, rows) => (
    rows.findIndex((row) => `${row.time}|${row.tag}|${row.text}` === `${item.time}|${item.tag}|${item.text}`) === index
  )).slice(-14)

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{t('对话与执行日志', 'Dialogue & Execution Log')}</strong>
          <span>{filteredServerLogs.length ? t(`后端缓存：${filteredServerLogs.length} 条`, `Backend buffer: ${filteredServerLogs.length} entries`) : t('实时链路记录', 'Live link records')}</span>
        </div>
        <button onClick={refreshLogs} disabled={loading}>{loading ? t('同步中', 'Syncing') : t('同步日志', 'Sync Logs')}</button>
      </div>
      <div className="dialogue-list embedded">
        {displayRows.map((item, index) => (
          <div className={`dialogue-row ${item.tone}`} key={`${item.time}-${index}`}>
            <span className="log-time">[{item.time}]</span>
            <span className="log-tag">[{logTagText(item.tag, language)}]</span>
            <span className="log-text">{item.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function formatTrajectoryDuration(seconds, language) {
  const value = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(value / 60)
  const remainder = Math.round(value % 60)
  if (!minutes) return `${remainder} s`
  return textFor(language, `${minutes} 分 ${remainder} 秒`, `${minutes}m ${remainder}s`)
}

function TrajectoryWorkspace({
  language,
  samples,
  recording,
  source,
  sceneMode,
  tracksVisible,
  onStart,
  onStop,
  onClear,
  onLoadDemo,
  onExportCsv,
  onExportJson,
  onSetSceneMode,
  onToggleTracks,
}) {
  const t = makeTranslator(language)
  const summary = useMemo(() => summarizeTrajectory(samples), [samples])
  const latestSamples = [...samples].slice(-7).reverse()
  const statusLabel = recording
    ? t('实时记录中', 'Live recording')
    : source === 'demo' && samples.length
      ? t('示例数据', 'Demo data')
      : samples.length
        ? t('记录已暂停', 'Recording paused')
        : t('等待记录', 'Ready to record')

  return (
    <div className="workspace-body trajectory-workspace">
      <div className="ops-summary trajectory-summary">
        <div>
          <strong><i className={recording ? 'recording' : ''} />{t('无人机轨迹记录', 'UAV Trajectory Recorder')}</strong>
          <span>{statusLabel} · NED · {t('米', 'meters')}</span>
        </div>
        <button className={recording ? 'danger-button' : ''} onClick={recording ? onStop : onStart}>
          <span aria-hidden="true">{recording ? '■' : '●'}</span>
          {recording ? t('停止', 'Stop') : samples.length && source === 'live' ? t('继续', 'Resume') : t('记录', 'Record')}
        </button>
      </div>

      <div className="trajectory-stats">
        <div><span>{t('无人机', 'UAVs')}</span><strong>{summary.vehicle_count}</strong></div>
        <div><span>{t('采样点', 'Samples')}</span><strong>{summary.sample_count}</strong></div>
        <div><span>{t('时长', 'Duration')}</span><strong>{formatTrajectoryDuration(summary.duration_s, language)}</strong></div>
        <div><span>{t('总里程', 'Distance')}</span><strong>{summary.total_distance_m.toFixed(1)} m</strong></div>
      </div>

      <div className="trajectory-options">
        <div className="trajectory-option-group">
          <span>{t('地图背景', 'Map Background')}</span>
          <div className="segmented-control">
            <button className={!sceneMode ? 'active' : ''} onClick={() => onSetSceneMode(false)}>{t('战术图', 'Tactical')}</button>
            <button className={sceneMode ? 'active' : ''} onClick={() => onSetSceneMode(true)}>{t('环境俯视', 'Aerial View')}</button>
          </div>
        </div>
        <label className="trajectory-toggle">
          <input type="checkbox" checked={tracksVisible} onChange={onToggleTracks} />
          <span>{t('显示航迹', 'Show Tracks')}</span>
        </label>
      </div>

      <div className="trajectory-actions">
        <button onClick={onLoadDemo}><span aria-hidden="true">◇</span>{t('加载示例', 'Load Demo')}</button>
        <button onClick={onClear} disabled={!samples.length && !recording}><span aria-hidden="true">↺</span>{t('清空', 'Clear')}</button>
        <button onClick={onExportCsv} disabled={!samples.length}><span aria-hidden="true">⇩</span>CSV</button>
        <button onClick={onExportJson} disabled={!samples.length}><span aria-hidden="true">⇩</span>JSON</button>
      </div>

      {summary.vehicles.length ? (
        <div className="trajectory-series-list">
          {summary.vehicles.map((vehicle) => (
            <div className="trajectory-series-row" key={vehicle.uav_id}>
              <i style={{ background: vehicle.color }} />
              <strong>{vehicle.uav_id}</strong>
              <span>{vehicle.samples} {t('点', 'pts')}</span>
              <span>{vehicle.distance_m.toFixed(1)} m</span>
              <small>{vehicle.latest_speed_mps.toFixed(1)} m/s</small>
            </div>
          ))}
        </div>
      ) : (
        <div className="trajectory-empty">{t('点击“记录”采集 AirSim 实时位置，或加载示例数据。', 'Record live AirSim positions or load the demo dataset.')}</div>
      )}

      <div className="trajectory-table">
        <div className="trajectory-table-head">
          <span>UAV</span><span>{t('时间', 'Time')}</span><span>N</span><span>E</span><span>{t('高度', 'Alt')}</span>
        </div>
        {latestSamples.map((sample) => (
          <div className="trajectory-table-row" key={`${sample.timestamp}-${sample.uav_id}`}>
            <span style={{ color: trajectoryColor(sample.uav_id) }}>{sample.uav_id}</span>
            <span>{Number(sample.elapsed_s || 0).toFixed(1)}s</span>
            <span>{Number(sample.north_m || 0).toFixed(1)}</span>
            <span>{Number(sample.east_m || 0).toFixed(1)}</span>
            <span>{Number(sample.altitude_m || -sample.down_m || 0).toFixed(1)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function RightWorkspace({
  activeView,
  timeline,
  skillPanelOpen,
  selectedUav,
  language,
  sensorOptions,
  sensorCameras,
  sensorLidar,
  activeSensor,
  worldState,
  skillCatalog,
  connected,
  systemStatus,
  aiThinking,
  aiThoughts,
  aiStream,
  lastAiPlan,
  lastAiReport,
  skillPanelProps,
  trajectoryProps,
  onSetView,
  onShowLog,
  onShowSkill,
  onSelectSensor,
  onOpenCockpit,
  onSetMode,
  onCloseSkill,
}) {
  const t = makeTranslator(language)
  const showSkill = activeView === 'skill' && skillPanelOpen
  const tabs = [
    { key: 'log', label: t('日志', 'Log') },
    { key: 'fleet', label: t('态势', 'Situation') },
    { key: 'tracks', label: t('轨迹', 'Tracks') },
    { key: 'skill', label: t('Skill', 'Skill') },
    { key: 'sensors', label: t('载荷', 'Payloads') },
    { key: 'reasoning', label: t('推理', 'Reasoning') },
    { key: 'memory', label: t('记忆', 'Memory') },
    { key: 'capability', label: t('能力', 'Capability') },
    { key: 'model', label: t('通道', 'Channels') },
    { key: 'devices', label: t('接入', 'Access') },
  ]

  return (
    <section className="mission-panel workspace-panel">
      <div className="workspace-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            className={activeView === tab.key || (tab.key === 'log' && !showSkill && activeView === 'log') ? 'active' : ''}
            onClick={() => {
              if (tab.key === 'log') onShowLog()
              else if (tab.key === 'skill') onShowSkill()
              else onSetView(tab.key)
            }}
          >
            {tab.label}
          </button>
        ))}
        {skillPanelOpen && activeView === 'skill' && (
          <button className="workspace-close" onClick={onCloseSkill} aria-label={t('关闭 Skill 可视化', 'Close Skill visualizer')}>×</button>
        )}
      </div>

      {showSkill ? (
        <div className="right-skill-panel">
          <div className="right-skill-title">
            <span>{selectedUav?.id || 'UAV'} · {t('Skill 可视化', 'Skill Visualizer')}</span>
            <span>{t('基础 / 高级技能', 'Basic / Advanced Skills')}</span>
          </div>
          <div className="right-skill-body">
            <SkillPanel {...skillPanelProps} />
          </div>
        </div>
      ) : activeView === 'fleet' ? (
        <FleetWorkspace
          language={language}
          worldState={worldState}
          systemStatus={systemStatus}
        />
      ) : activeView === 'tracks' ? (
        <TrajectoryWorkspace {...trajectoryProps} />
      ) : activeView === 'sensors' ? (
        <PayloadWorkspace
          language={language}
          sensorOptions={sensorOptions}
          selectedUav={selectedUav}
          activeSensor={activeSensor}
          sensorCameras={sensorCameras}
          sensorLidar={sensorLidar}
          onSelectSensor={onSelectSensor}
          onOpenCockpit={onOpenCockpit}
        />
      ) : activeView === 'reasoning' ? (
        <ReasoningWorkspace
          language={language}
          systemStatus={systemStatus}
          aiThinking={aiThinking}
          aiThoughts={aiThoughts}
          aiStream={aiStream}
          lastAiPlan={lastAiPlan}
          lastAiReport={lastAiReport}
          onSetMode={onSetMode}
        />
      ) : activeView === 'memory' ? (
        <MemoryWorkspace language={language} />
      ) : activeView === 'capability' ? (
        <CapabilityWorkspace
          language={language}
          selectedUav={selectedUav}
          skillCatalog={skillCatalog}
        />
      ) : activeView === 'model' ? (
        <ModelWorkspace language={language} />
      ) : activeView === 'devices' ? (
        <DeviceWorkspace language={language} connected={connected} />
      ) : (
        <LogsWorkspace language={language} timeline={timeline} />
      )}
    </section>
  )
}

function formatVec(position) {
  if (!position) return '--'
  const list = Array.isArray(position)
    ? position
    : [position.north ?? position.n ?? 0, position.east ?? position.e ?? 0, position.down ?? position.d ?? 0]
  return list.map((value) => Number(value || 0).toFixed(1)).join(', ')
}

function FleetWorkspace({ language, worldState, systemStatus }) {
  const t = makeTranslator(language)
  const [adapterStatus, setAdapterStatus] = useState(null)
  const [sensorStatus, setSensorStatus] = useState(null)
  const [landmarks, setLandmarks] = useState([])
  const robots = Object.entries(worldState?.robots || {})
  const targets = worldState?.targets || []

  const refresh = () => {
    fetch(`${API_BASE}/api/adapter/status`).then((r) => r.json()).then(setAdapterStatus).catch(() => {})
    fetch(`${API_BASE}/api/sensor/status`).then((r) => r.json()).then(setSensorStatus).catch(() => {})
    fetch(`${API_BASE}/api/map/landmarks`).then((r) => r.json()).then((data) => setLandmarks(data.landmarks || [])).catch(() => {})
  }

  useEffect(() => {
    refresh()
  }, [])

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{t('协同态势', 'Collaborative Situation')}</strong>
          <span>{systemStatus.initialized ? t('世界模型在线', 'World model online') : t('等待初始化', 'Waiting for initialization')} · {robots.length} {t('节点', 'nodes')}</span>
        </div>
        <button onClick={refresh}>{t('刷新', 'Refresh')}</button>
      </div>

      <div className="status-grid">
        <div className="status-card"><span>{t('执行', 'Execution')}</span><strong>{systemStatus.is_executing ? t('忙碌', 'Busy') : t('待命', 'Standby')}</strong><small>{systemStatus.mode === 'ai' ? t('自主', 'Autonomous') : t('手动', 'Manual')}</small></div>
        <div className="status-card"><span>{t('适配器', 'Adapter')}</span><strong>{adapterStatus?.adapter || adapterStatus?.name || '--'}</strong><small>{adapterStatus?.connected ? t('已连接', 'Connected') : t('待命', 'Standby')}</small></div>
        <div className="status-card"><span>{t('感知桥接', 'Perception Bridge')}</span><strong>{sensorStatus?.ok === false || sensorStatus?.running === false ? t('待命', 'Standby') : t('在线', 'Online')}</strong><small>{sensorStatus?.mode || sensorStatus?.source || t('载荷总线', 'Payload Bus')}</small></div>
      </div>

      <div className="split-list">
        <div>
          <div className="mini-section-title">{t('编队节点', 'Fleet Nodes')}</div>
          <div className="scroll-box">
            {robots.map(([id, data]) => (
              <div className="trace-row" key={id}>
                <strong>{id}</strong>
                <span>{statusText(data.status, language)} · {t('电量', 'Battery')} {Math.round(data.battery || 0)}% · [{formatVec(data.position)}]</span>
              </div>
            ))}
            {!robots.length && <p className="empty-copy">{t('暂无节点。', 'No nodes yet.')}</p>}
          </div>
        </div>
        <div>
          <div className="mini-section-title">{t('地图语义', 'Map Semantics')}</div>
          <div className="scroll-box semantic-list">
            {targets.map((target) => (
              <div className="semantic-row target" key={target.target_id || target.label}>
                <strong>{target.label || t('目标', 'Target')}</strong>
                <span className="semantic-coord">{Math.round((target.confidence || 0) * 100)}%</span>
                <span className="semantic-desc">[{formatVec(target.position)}]</span>
              </div>
            ))}
            {landmarks.slice(0, 20).map((landmark) => (
              <div className="semantic-row" key={landmark.name}>
                <strong>{landmark.name}</strong>
                <span className="semantic-coord">N {Number(landmark.n || 0).toFixed(0)} · E {Number(landmark.e || 0).toFixed(0)}</span>
                <span className="semantic-desc">{landmark.desc}</span>
              </div>
            ))}
            {!targets.length && !landmarks.length && <p className="empty-copy">{t('暂无目标或地标。', 'No targets or landmarks yet.')}</p>}
          </div>
        </div>
      </div>
    </div>
  )
}

function PayloadWorkspace({ language, sensorOptions, selectedUav, activeSensor, sensorCameras, sensorLidar, onSelectSensor, onOpenCockpit }) {
  const t = makeTranslator(language)
  const cameraSensors = sensorOptions.filter((sensor) => sensor.type === 'camera')
  const dataSensors = sensorOptions.filter((sensor) => sensor.type !== 'camera')
  const cockpitSensor = activeSensor?.type === 'camera' ? activeSensor.sensor : 'front'
  const activeSensorLabel = sensorOptions.find((sensor) => sensor.key === activeSensor?.sensor)?.label || activeSensor?.label
  const robotId = selectedUav?.robotId || activeSensor?.robotId || 'UAV_1'
  const [sensorSnapshot, setSensorSnapshot] = useState({ cameraUrl: '', lidar: null, message: '' })
  const displayLidar = sensorSnapshot.lidar && !sensorSnapshot.lidar.error ? sensorSnapshot.lidar : sensorLidar

  useEffect(() => () => {
    if (sensorSnapshot.cameraUrl) URL.revokeObjectURL(sensorSnapshot.cameraUrl)
  }, [sensorSnapshot.cameraUrl])

  const fetchCameraSnapshot = () => {
    fetch(`${API_BASE}/api/sensor/camera?view=${encodeURIComponent(cockpitSensor)}&robot_id=${encodeURIComponent(robotId)}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text())
        return r.blob()
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob)
        setSensorSnapshot((prev) => {
          if (prev.cameraUrl) URL.revokeObjectURL(prev.cameraUrl)
          return { ...prev, cameraUrl: url, message: t('已获取相机快照', 'Camera snapshot received') }
        })
      })
      .catch((event) => setSensorSnapshot((prev) => ({ ...prev, message: t(`相机快照未就绪：${event.message}`, `Camera snapshot not ready: ${event.message}`) })))
  }

  const fetchLidarSnapshot = () => {
    fetch(`${API_BASE}/api/sensor/lidar?robot_id=${encodeURIComponent(robotId)}`)
      .then((r) => r.json())
      .then((data) => setSensorSnapshot((prev) => ({ ...prev, lidar: data, message: data.error || t(`LiDAR 快照：${data.ranges?.length || data.count || 0} 点`, `LiDAR snapshot: ${data.ranges?.length || data.count || 0} pts`) })))
      .catch((event) => setSensorSnapshot((prev) => ({ ...prev, message: t(`LiDAR 快照未就绪：${event.message}`, `LiDAR snapshot not ready: ${event.message}`) })))
  }

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{selectedUav?.id || 'UAV'} {t('载荷舱', 'Payload Bay')}</strong>
          <span>{activeSensorLabel || t('选择一个传感器', 'Select a sensor')}</span>
        </div>
        <button onClick={() => onOpenCockpit(cockpitSensor)}>{t('可见光驾驶舱', 'Visible-Light Cockpit')}</button>
      </div>

      <div className="action-grid">
        <button onClick={fetchCameraSnapshot}>{t('相机快照', 'Camera Snapshot')}</button>
        <button onClick={fetchLidarSnapshot}>{t('LiDAR 快照', 'LiDAR Snapshot')}</button>
        <button onClick={() => setSensorSnapshot({ cameraUrl: '', lidar: null, message: '' })}>{t('清除快照', 'Clear Snapshot')}</button>
      </div>
      {sensorSnapshot.message && <p className="result-copy">{sensorSnapshot.message}</p>}
      {sensorSnapshot.cameraUrl && <img className="sensor-snapshot" src={sensorSnapshot.cameraUrl} alt="Sensor snapshot" />}
      {sensorSnapshot.lidar && !sensorSnapshot.lidar.error && (
        <p className="empty-copy">{t('量程', 'Range')} {sensorSnapshot.lidar.range_max || '--'} m · {t('点数', 'Points')} {sensorSnapshot.lidar.ranges?.length || sensorSnapshot.lidar.count || 0}</p>
      )}

      <div className="sensor-camera-grid">
        {cameraSensors.map((sensor) => {
          const frame = sensorCameras?.[sensor.key]
          const active = activeSensor?.sensor === sensor.key
          return (
            <button
              key={sensor.key}
              className={`sensor-tile ${active ? 'active' : ''}`}
              onClick={() => onSelectSensor(sensor)}
            >
              {active || frame?.image ? (
                <img
                  src={active
                    ? `${API_BASE}/api/sensor/camera/stream?view=${encodeURIComponent(sensor.key)}&robot_id=${encodeURIComponent(robotId)}`
                    : `data:image/jpeg;base64,${frame.image}`}
                  alt={sensor.label}
                />
              ) : (
                <span className="sensor-placeholder">{t('等待中', 'Waiting')}</span>
              )}
              <span>{sensor.shortLabel}</span>
              <small>{frame?.fps ? `${frame.fps} fps` : t('待命', 'Standby')}</small>
            </button>
          )
        })}
      </div>

      <div className="status-grid">
        {dataSensors.map((sensor) => (
          <button
            key={sensor.key}
            className={`status-card ${activeSensor?.sensor === sensor.key ? 'active' : ''}`}
            onClick={() => onSelectSensor(sensor)}
          >
            <span>{sensor.shortLabel}</span>
            <strong>
              {sensor.key === 'lidar'
                ? t(`${displayLidar?.count || 0} 点`, `${displayLidar?.count || 0} pts`)
                : sensor.key === 'bottom_distance'
                  ? selectedUav?.groundClearance != null && Number.isFinite(Number(selectedUav.groundClearance))
                    ? `${Number(selectedUav.groundClearance).toFixed(2)} m`
                    : t('等待数据', 'Waiting')
                : sensor.type === 'range'
                  ? t('测距就绪', 'Range-ready')
                  : sensor.type === 'state'
                    ? t('可读取', 'Readable')
                    : t('可调用', 'Callable')}
            </strong>
            <small>
              {sensor.key === 'lidar'
                ? t(`量程 ${displayLidar?.range_max || '--'} m`, `Range ${displayLidar?.range_max || '--'} m`)
                : sensor.key === 'bottom_distance'
                  ? t('机腹实时离地高度', 'Real-time height above ground')
                : sensor.type === 'imaging'
                  ? t('由感知技能调用', 'Called by perception skill')
                  : sensor.type === 'range'
                    ? t('由测距技能汇聚', 'Aggregated by range skill')
                    : t('由状态技能汇聚', 'Aggregated by state skill')}
            </small>
          </button>
        ))}
      </div>
    </div>
  )
}

function ReasoningWorkspace({ language, systemStatus, aiThinking, aiThoughts, aiStream, lastAiPlan, lastAiReport, onSetMode }) {
  const t = makeTranslator(language)
  const streamText = (aiStream?.text || '').replace(/<think>[\s\S]*?<\/think>/gi, '').trim()
  const planSteps = lastAiPlan?.steps || []
  const reportSteps = lastAiReport?.step_results || []

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{t('任务推理', 'Mission Reasoning')}</strong>
          <span>{systemStatus.mode === 'ai' ? t('自主协同已启用', 'Autonomous collaboration active') : t('手动调度已启用', 'Manual dispatch active')}</span>
        </div>
        <button onClick={() => onSetMode(systemStatus.mode === 'ai' ? 'manual' : 'ai')}>
          {systemStatus.mode === 'ai' ? t('切换到手动', 'Switch to Manual') : t('进入自主', 'Enter Autonomous')}
        </button>
      </div>

      <div className="mini-section">
        <div className="mini-section-title">{t('当前状态', 'Current Status')}</div>
        <div className="status-grid">
          <div className="status-card">
            <span>{t('阶段', 'Phase')}</span>
            <strong>{aiThinking?.phase || 'idle'}</strong>
            <small>{aiThinking?.detail || t('等待任务', 'Waiting for mission')}</small>
          </div>
          <div className="status-card">
            <span>{t('轮次', 'Rounds')}</span>
            <strong>{aiThoughts?.length || 0}</strong>
            <small>{t('推理轨迹', 'Reasoning trace')}</small>
          </div>
          <div className="status-card">
            <span>{t('报告', 'Report')}</span>
            <strong>{lastAiReport ? (lastAiReport.ok ? t('完成', 'Complete') : t('复核', 'Review')) : t('无', 'None')}</strong>
            <small>{lastAiReport ? `${lastAiReport.completed_steps}/${lastAiReport.total_steps}` : t('未生成', 'Not generated')}</small>
          </div>
        </div>
      </div>

      <div className="split-list">
        <div>
          <div className="mini-section-title">{t('推理记录', 'Reasoning Notes')}</div>
          <div className="scroll-box">
            {aiThoughts?.length ? aiThoughts.slice(-8).map((item) => (
              <div className="trace-row" key={item.iteration}>
                <strong>#{item.iteration}</strong>
                <span>{item.thinking || item.reflection || item.progress || t('已记录状态更新', 'Status update recorded')}</span>
              </div>
            )) : <p className="empty-copy">{streamText || t('暂无推理轨迹。', 'No reasoning trace yet.')}</p>}
          </div>
        </div>

        <div>
          <div className="mini-section-title">{t('计划 / 回执', 'Plan / Receipts')}</div>
          <div className="scroll-box">
            {(reportSteps.length ? reportSteps : planSteps).slice(0, 12).map((step, index) => (
              <div className="trace-row" key={`${step.skill}-${index}`}>
                <strong>{step.success === false ? '×' : step.success ? '✓' : index + 1}</strong>
                <span>{step.skill} · {step.robot || 'UAV'}</span>
              </div>
            ))}
            {!reportSteps.length && !planSteps.length && <p className="empty-copy">{t('任务计划会归档在这里。', 'Mission plans will be archived here.')}</p>}
          </div>
        </div>
      </div>
    </div>
  )
}

function MemoryWorkspace({ language }) {
  const t = makeTranslator(language)
  const [stats, setStats] = useState(null)
  const [recent, setRecent] = useState([])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)

  const refresh = () => {
    fetch(`${API_BASE}/api/memory/stats`).then((r) => r.json()).then((data) => setStats(data.layers || {})).catch(() => {})
    fetch(`${API_BASE}/api/memory/recent`).then((r) => r.json()).then((data) => setRecent(data.items || [])).catch(() => {})
  }

  useEffect(() => {
    refresh()
  }, [])

  const search = () => {
    const text = query.trim()
    if (!text) return
    setLoading(true)
    fetch(`${API_BASE}/api/memory/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: text, top_k: 8 }),
    })
      .then((r) => r.json())
      .then((data) => setResults(data.items || []))
      .catch(() => setResults([]))
      .finally(() => setLoading(false))
  }

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{t('经验记忆库', 'Experience Store')}</strong>
          <span>{t('任务片段、技能经验与世界印象', 'Mission fragments, skill experience, and world impressions')}</span>
        </div>
        <button onClick={refresh}>{t('刷新', 'Refresh')}</button>
      </div>

      <div className="status-grid">
        {Object.entries(stats || {}).map(([key, value]) => (
          <div className="status-card" key={key}>
            <span>{value.label || key}</span>
            <strong>{value.count || 0}</strong>
            <small>{key}</small>
          </div>
        ))}
      </div>

      <div className="search-row">
        <input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && search()} placeholder={t('搜索任务经验...', 'Search mission experience...')} />
        <button onClick={search} disabled={loading}>{loading ? t('搜索中', 'Searching') : t('搜索', 'Search')}</button>
      </div>

      <div className="scroll-box tall">
        {(results.length ? results : recent).map((item, index) => (
          <div className="trace-row" key={index}>
            <strong>{item.layer || t('记录', 'Record')}</strong>
            <span>{item.text || JSON.stringify(item)}</span>
          </div>
        ))}
        {!results.length && !recent.length && <p className="empty-copy">{t('暂无可显示的经验片段。', 'No experience fragments to display.')}</p>}
      </div>
    </div>
  )
}

function CapabilityWorkspace({ language, selectedUav, skillCatalog }) {
  const t = makeTranslator(language)
  const [softSkills, setSoftSkills] = useState([])
  const [opResult, setOpResult] = useState('')
  const [softDraftName, setSoftDraftName] = useState('field_coop_skill')
  const [selectedSoftDoc, setSelectedSoftDoc] = useState(null)
  const robotId = selectedUav?.robotId || 'UAV_1'
  const skills = skillCatalog?.[robotId] || []
  const basic = skills.filter((skill) => !['soft', 'perception'].includes(skill.skill_type)).length
  const perception = skills.filter((skill) => skill.skill_type === 'perception').length
  const soft = softSkills.length

  const refreshSoft = () => {
    fetch(`${API_BASE}/api/skills/soft`).then((r) => r.json()).then((data) => setSoftSkills(Array.isArray(data) ? data : data.skills || [])).catch(() => {})
  }

  useEffect(() => {
    refreshSoft()
  }, [])

  const runSoftAction = (kind) => {
    if (kind === 'generate') {
      fetch(`${API_BASE}/api/skills/soft/patterns?min_count=2`)
        .then((r) => r.json())
        .then((patternData) => {
          const pattern = patternData.patterns?.[0]
          if (!pattern) {
            setOpResult(t('暂无可提升的候选模式', 'No candidate pattern is ready to promote'))
            return null
          }
          return fetch(`${API_BASE}/api/skills/soft/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pattern }),
          }).then((r) => r.json())
        })
        .then((data) => {
          if (!data) return
          setOpResult(data.ok ? t(`候选能力已生成：${data.name}`, `Candidate capability generated: ${data.name}`) : data.msg || t('候选能力未生成', 'Candidate capability was not generated'))
          refreshSoft()
        })
        .catch((error) => setOpResult(t(`候选能力未生成：${error.message}`, `Candidate capability was not generated: ${error.message}`)))
      return
    }

    const config = {
      patterns: { url: '/api/skills/soft/patterns', method: 'GET', label: t('模式复核完成', 'Pattern review complete') },
      retire: { url: '/api/skills/soft/retire', method: 'POST', label: t('能力健康检查完成', 'Capability health check complete'), body: { dry_run: true } },
    }[kind]
    if (!config) return
    fetch(`${API_BASE}${config.url}`, {
      method: config.method,
      headers: { 'Content-Type': 'application/json' },
      body: config.body ? JSON.stringify(config.body) : undefined,
    })
      .then((r) => r.json())
      .then((data) => {
        setOpResult(`${config.label}: ${JSON.stringify(data).slice(0, 220)}`)
        refreshSoft()
      })
      .catch((error) => setOpResult(t(`操作未完成：${error.message}`, `Operation incomplete: ${error.message}`)))
  }

  const viewSoftSkill = (name) => {
    fetch(`${API_BASE}/api/skills/soft/${encodeURIComponent(name)}`)
      .then((r) => r.json())
      .then((data) => setSelectedSoftDoc(data.ok ? data : { error: t('文档不可读取', 'Document is not readable') }))
      .catch((error) => setSelectedSoftDoc({ error: t(`文档不可读取：${error.message}`, `Document is not readable: ${error.message}`) }))
  }

  const createSoftSkill = () => {
    const name = softDraftName.trim()
    if (!name) return
    fetch(`${API_BASE}/api/skills/soft`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        content: `# ${name}\n\n## Summary\nAdvanced Skill profile for AeroWeaver collaborative missions; it can serve as a candidate template for combining basic actions.\n\n## Steps\n1. Read the world model and mission targets.\n2. Select UAV fleet members and payloads.\n3. Compose basic skills to complete collaborative actions.`,
      }),
    })
      .then((r) => r.json())
      .then((data) => {
        setOpResult(data.ok ? t(`高级 Skill 已创建：${data.name}`, `Advanced Skill created: ${data.name}`) : data.msg || t('创建未完成', 'Creation incomplete'))
        refreshSoft()
      })
      .catch((error) => setOpResult(t(`创建未完成：${error.message}`, `Creation incomplete: ${error.message}`)))
  }

  const deleteSoftSkill = (name) => {
    fetch(`${API_BASE}/api/skills/soft/${encodeURIComponent(name)}`, { method: 'DELETE' })
      .then((r) => r.json())
      .then((data) => {
        setOpResult(data.ok ? t(`高级 Skill 已移除：${data.name}`, `Advanced Skill removed: ${data.name}`) : data.msg || t('移除未完成', 'Removal incomplete'))
        refreshSoft()
      })
      .catch((error) => setOpResult(t(`移除未完成：${error.message}`, `Removal incomplete: ${error.message}`)))
  }

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{selectedUav?.id || 'UAV'} {t('能力图谱', 'Capability Map')}</strong>
          <span>{t('基础动作、高级编排、感知与交互', 'Basic actions, advanced orchestration, perception, and interaction')}</span>
        </div>
        <button onClick={refreshSoft}>{t('同步', 'Sync')}</button>
      </div>

      <div className="status-grid">
        <div className="status-card"><span>{t('基础', 'Basic')}</span><strong>{basic}</strong><small>{t('运动 / 状态', 'Motion / State')}</small></div>
        <div className="status-card"><span>{t('感知', 'Perception')}</span><strong>{perception}</strong><small>{t('视觉 / 融合', 'Vision / Fusion')}</small></div>
        <div className="status-card"><span>{t('高级', 'Advanced')}</span><strong>{soft}</strong><small>{t('可组合策略', 'Composable Strategy')}</small></div>
      </div>

      <div className="action-grid">
        <button onClick={() => runSoftAction('patterns')}>{t('复核模式', 'Review Patterns')}</button>
        <button onClick={() => runSoftAction('generate')}>{t('提升能力', 'Promote Capability')}</button>
        <button onClick={() => runSoftAction('retire')}>{t('健康检查', 'Health Check')}</button>
      </div>

      <div className="channel-form soft-form">
        <input value={softDraftName} onChange={(event) => setSoftDraftName(event.target.value)} placeholder={t('高级 Skill ID', 'Advanced Skill ID')} />
        <button onClick={createSoftSkill}>{t('手动创建', 'Create Manually')}</button>
      </div>

      <div className="scroll-box tall">
        {softSkills.slice(0, 10).map((skill) => {
          const display = localizedAdvancedSkill(skill, language)
          return (
            <div className="device-row" key={skill.name}>
              <div>
                <strong>{t('高级', 'Advanced')}</strong>
                <span>{display.title}{display.summary ? ` · ${display.summary}` : ''}</span>
                <small>{skill.name}</small>
              </div>
              <div className="inline-actions">
                <button onClick={() => viewSoftSkill(skill.name)}>{t('查看', 'View')}</button>
                <button onClick={() => deleteSoftSkill(skill.name)}>{t('移除', 'Remove')}</button>
              </div>
            </div>
          )
        })}
        {skills.slice(0, 24).map((skill) => (
          <div className="trace-row" key={skill.name}>
            <strong>{skillTypeText(skill.skill_type, language)}</strong>
            <span>{skillLabel(skill, language)} · {skillDescription(skill, language).replaceAll('software', 'system').slice(0, 90)}</span>
          </div>
        ))}
        {selectedSoftDoc && <p className="result-copy">{localizedAdvancedSkillDetail(selectedSoftDoc, language)}</p>}
        {opResult && <p className="result-copy">{opResult}</p>}
      </div>
    </div>
  )
}

function ModelWorkspace({ language }) {
  const t = useMemo(() => makeTranslator(language), [language])
  const [config, setConfig] = useState(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [providerDraft, setProviderDraft] = useState({
    name: 'field_reasoner',
    base_url: 'http://127.0.0.1:11434/v1',
    default_model: 'qwen2.5:7b',
    api_key: 'none',
    timeout: 60,
  })

  const refresh = useCallback(() => {
    fetch(`${API_BASE}/api/llm/config`).then((r) => r.json()).then((data) => {
      if (data.ok) setConfig(data)
      else setError(t('推理通道不可读取', 'Reasoning channel is not readable'))
    }).catch((event) => setError(event.message))
  }, [t])

  useEffect(() => {
    refresh()
  }, [refresh])

  const setActive = (provider) => {
    fetch(`${API_BASE}/api/llm/active`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider }),
    })
      .then((r) => r.json())
      .then((data) => setNotice(data.ok ? t(`已切换到 ${data.active_provider}`, `Switched to ${data.active_provider}`) : data.msg || t('切换未完成', 'Switch incomplete')))
      .then(refresh)
      .catch((event) => setError(event.message))
  }

  const setModule = (moduleName, provider, model) => {
    fetch(`${API_BASE}/api/llm/module/${moduleName}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model }),
    })
      .then((r) => r.json())
      .then((data) => setNotice(data.ok ? t(`${moduleName} 已指向 ${data.resolved_provider}/${data.resolved_model}`, `${moduleName} now points to ${data.resolved_provider}/${data.resolved_model}`) : data.msg || t('配置未完成', 'Configuration incomplete')))
      .then(refresh)
      .catch((event) => setError(event.message))
  }

  const saveProvider = () => {
    fetch(`${API_BASE}/api/llm/provider`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(providerDraft),
    })
      .then((r) => r.json())
      .then((data) => setNotice(data.ok ? t(`${data.name} 通道已${data.action || '更新'}`, `${data.action || 'Updated'} channel ${data.name}`) : data.msg || t('通道未保存', 'Channel not saved')))
      .then(refresh)
      .catch((event) => setError(event.message))
  }

  const deleteProvider = (name) => {
    fetch(`${API_BASE}/api/llm/provider/${encodeURIComponent(name)}`, { method: 'DELETE' })
      .then((r) => r.json())
      .then((data) => setNotice(data.ok ? t(`已移除 ${data.name}`, `Removed ${data.name}`) : data.msg || t('移除未完成', 'Removal incomplete')))
      .then(refresh)
      .catch((event) => setError(event.message))
  }

  const providers = config?.providers || {}
  const active = config?.active_provider
  const modules = config?.modules || {}

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{t('推理通道', 'Reasoning Channels')}</strong>
          <span>{active ? `${active} · ${providers[active]?.default_model || ''}` : t('等待配置', 'Waiting for configuration')}</span>
        </div>
        <button onClick={refresh}>{t('刷新', 'Refresh')}</button>
      </div>
      {(notice || error) && <p className={error ? 'empty-copy' : 'result-copy'}>{notice || error}</p>}

      <div className="scroll-box tall">
        {Object.entries(providers).map(([name, provider]) => (
          <div className={`provider-row ${name === active ? 'active' : ''}`} key={name}>
            <button className="provider-main" onClick={() => setActive(name)}>
              <strong>{name}</strong>
              <span>{provider.default_model || t('模型', 'Model')} · {(provider.base_url || '').replace(/^https?:\/\//, '')}</span>
            </button>
            <button className="mini-inline" onClick={() => deleteProvider(name)} disabled={name === active}>{t('移除', 'Remove')}</button>
          </div>
        ))}
        {!Object.keys(providers).length && <p className="empty-copy">{error || t('暂无通道配置。', 'No channel configuration yet.')}</p>}
      </div>

      <div className="mini-section">
        <div className="mini-section-title">{t('模块路由', 'Module Routing')}</div>
        <div className="scroll-box compact-box">
          {Object.entries(modules).map(([moduleName, moduleConfig]) => (
            <div className="module-row" key={moduleName}>
              <strong>{moduleName}</strong>
              <span>{moduleConfig.resolved_provider} · {moduleConfig.resolved_model}</span>
              <button onClick={() => setModule(moduleName, active || null, null)}>{t('使用全局', 'Use Global')}</button>
              <button onClick={() => setModule(moduleName, active, providers[active]?.default_model || null)} disabled={!active}>{t('绑定当前', 'Bind Current')}</button>
            </div>
          ))}
          {!Object.keys(modules).length && <p className="empty-copy">{t('暂无模块槽位。', 'No module slots yet.')}</p>}
        </div>
      </div>

      <div className="channel-form">
        <input value={providerDraft.name} onChange={(event) => setProviderDraft((prev) => ({ ...prev, name: event.target.value }))} placeholder={t('通道 ID', 'Channel ID')} />
        <input value={providerDraft.base_url} onChange={(event) => setProviderDraft((prev) => ({ ...prev, base_url: event.target.value }))} placeholder={t('通道 URL', 'Channel URL')} />
        <input value={providerDraft.default_model} onChange={(event) => setProviderDraft((prev) => ({ ...prev, default_model: event.target.value }))} placeholder={t('默认模型', 'Default Model')} />
        <input value={providerDraft.api_key} onChange={(event) => setProviderDraft((prev) => ({ ...prev, api_key: event.target.value }))} placeholder={t('凭据', 'Credential')} />
        <button onClick={saveProvider}>{t('保存通道', 'Save Channel')}</button>
      </div>
    </div>
  )
}

function DeviceWorkspace({ language, connected }) {
  const t = makeTranslator(language)
  const [devices, setDevices] = useState([])
  const [deviceTokens, setDeviceTokens] = useState({})
  const [lastToken, setLastToken] = useState('')
  const [opResult, setOpResult] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    fetch(`${API_BASE}/api/devices`).then((r) => r.json()).then((data) => setDevices(data.devices || [])).catch((event) => setError(event.message))
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const registerDemoDevice = () => {
    const existingIds = new Set(devices.map((device) => device.device_id))
    let sequence = devices.length + 1
    while (existingIds.has(`field_node_${String(sequence).padStart(5, '0')}`)) sequence += 1
    const id = `field_node_${String(sequence).padStart(5, '0')}`
    fetch(`${API_BASE}/api/device/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        device_id: id,
        device_type: 'UAV_PAYLOAD',
        protocol: 'aeroweaver-link-v1',
        capabilities: ['state_report', 'sensor_report', 'device_action'],
        sensors: ['camera', 'lidar', 'imu', 'gps'],
        metadata: { label: 'Field payload node' },
      }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.token) {
        setDeviceTokens((prev) => ({ ...prev, [id]: data.token }))
        setLastToken(data.token)
      }
        setOpResult(data.ok ? t(`节点 ${data.device_id} 已接入`, `Node ${data.device_id} connected`) : data.error || t('接入未完成', 'Connection incomplete'))
        refresh()
      })
      .catch((event) => setError(event.message))
  }

  const authHeaders = (deviceId) => ({
    'Content-Type': 'application/json',
    Authorization: `Bearer ${deviceTokens[deviceId] || ''}`,
  })

  const runDeviceOp = (device, kind) => {
    const id = device.device_id
    const token = deviceTokens[id]
    const authed = ['onboard', 'state', 'sensor', 'delete'].includes(kind)
    if (authed && !token) {
      setOpResult(t(`${id} 没有本地凭据，无法执行该操作`, `${id} has no local credential, so this action cannot be executed`))
      return
    }

    const configs = {
      skills: { url: `/api/device/${id}/skills`, method: 'GET', label: t('能力图谱', 'Capability Map') },
      onboard: { url: `/api/device/${id}/onboard`, method: 'POST', label: t('节点档案', 'Node Profile'), body: {} },
      state: {
        url: `/api/device/${id}/state`,
        method: 'POST',
        label: t('状态上报', 'State Report'),
        body: { status: 'idle', battery: device.state?.battery || 96, position: device.state?.position || [0, 0, 0] },
      },
      sensor: {
        url: `/api/device/${id}/sensor`,
        method: 'POST',
        label: t('载荷上报', 'Payload Report'),
        body: { sensor_type: 'camera', sensor_id: 'front', value: 'sample-ready' },
      },
      action: {
        url: `/api/device/${id}/action`,
        method: 'POST',
        label: t('动作派发', 'Action Dispatch'),
        body: { action: 'sync_payload', params: { source: 'front_panel' } },
      },
      delete: { url: `/api/device/${id}`, method: 'DELETE', label: t('撤销接入', 'Revoke Access') },
    }
    const config = configs[kind]
    if (!config) return

    fetch(`${API_BASE}${config.url}`, {
      method: config.method,
      headers: authed ? authHeaders(id) : { 'Content-Type': 'application/json' },
      body: config.body ? JSON.stringify(config.body) : undefined,
    })
      .then((r) => r.json())
      .then((data) => {
        const skillSummary = data.skills
          ? Object.entries(data.skills)
            .map(([key, value]) => `${key === 'hard' ? t('基础', 'Basic') : key === 'soft' ? t('高级', 'Advanced') : key === 'perception' ? t('感知', 'Perception') : key}:${(value || []).join('/') || t('无', 'None')}`)
            .join('; ')
          : ''
        setOpResult(`${config.label}: ${data.ok ? (skillSummary || t('完成', 'Complete')) : data.error || data.msg || t('未完成', 'Incomplete')}`)
        if (kind === 'delete' && data.ok) {
          setDeviceTokens((prev) => {
            const next = { ...prev }
            delete next[id]
            return next
          })
        }
        refresh()
      })
      .catch((event) => setOpResult(`${config.label}: ${event.message}`))
  }

  return (
    <div className="workspace-body">
      <div className="ops-summary">
        <div>
          <strong>{t('接入台', 'Access Desk')}</strong>
          <span>{connected ? t('链路在线，可接入外场节点', 'Link online; field nodes can be onboarded') : t('链路离线', 'Link offline')}</span>
        </div>
        <button onClick={registerDemoDevice}>{t('生成令牌', 'Generate Token')}</button>
      </div>
      {lastToken && <p className="result-copy">{t('新节点令牌', 'New node token')}: {lastToken.slice(0, 18)}...</p>}
      {opResult && <p className="result-copy">{opResult}</p>}
      <div className="scroll-box tall">
        {devices.map((device) => (
          <div className="device-row" key={device.device_id}>
            <div>
              <strong>{device.device_type}</strong>
              <span>{device.device_id} · {(device.capabilities || []).join(', ') || t('等待能力上报', 'Awaiting capabilities')}</span>
              <small>{deviceTokens[device.device_id] ? t('本地凭据可用', 'Local credential available') : t('只读状态', 'Read-only status')}</small>
            </div>
            <div className="inline-actions">
              <button onClick={() => runDeviceOp(device, 'skills')}>{t('图谱', 'Map')}</button>
              <button onClick={() => runDeviceOp(device, 'onboard')} disabled={!deviceTokens[device.device_id]}>{t('接入', 'Onboard')}</button>
              <button onClick={() => runDeviceOp(device, 'state')} disabled={!deviceTokens[device.device_id]}>{t('状态', 'State')}</button>
              <button onClick={() => runDeviceOp(device, 'sensor')} disabled={!deviceTokens[device.device_id]}>{t('载荷', 'Payload')}</button>
              <button onClick={() => runDeviceOp(device, 'action')}>{t('动作', 'Action')}</button>
              <button onClick={() => runDeviceOp(device, 'delete')} disabled={!deviceTokens[device.device_id]}>{t('撤销', 'Revoke')}</button>
            </div>
          </div>
        ))}
        {!devices.length && <p className="empty-copy">{error || t('暂无外场节点接入。', 'No field nodes connected yet.')}</p>}
      </div>
    </div>
  )
}

function TaskComposer({ language, disabled, onSubmit, taskArea, onClearTaskArea }) {
  const t = makeTranslator(language)
  const [value, setValue] = useState('')
  const [mode, setMode] = useState('auto')

  const submit = () => {
    const text = value.trim()
    if (!text || disabled) return
    onSubmit(text, mode)
    setValue('')
  }

  return (
    <section className="task-composer">
      <div className="composer-head">
        <h2>{t('消息输入', 'Operator Input')}</h2>
        <div className="composer-actions">
          {taskArea && mode !== 'chat' && (
            <div className="task-area-chip" title={taskAreaPrompt(taskArea, language)}>
              <span>{taskAreaSummary(taskArea, language)}</span>
              <button type="button" onClick={onClearTaskArea} aria-label={t('清除任务区域', 'Clear task area')}>×</button>
            </div>
          )}
          <div className="composer-mode">
            <button className={mode === 'auto' ? 'active' : ''} onClick={() => setMode('auto')}>{t('智能', 'Auto')}</button>
            <button className={mode === 'mission' ? 'active' : ''} onClick={() => setMode('mission')}>{t('执行', 'Execute')}</button>
            <button className={mode === 'chat' ? 'active' : ''} onClick={() => setMode('chat')}>{t('对话', 'Chat')}</button>
          </div>
        </div>
      </div>
      <div className="task-row">
        <textarea
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter' || event.shiftKey) return
            if (event.nativeEvent?.isComposing || event.keyCode === 229) return
            event.preventDefault()
            submit()
          }}
          enterKeyHint="send"
          placeholder={mode === 'chat'
            ? t('输入对话消息...', 'Type a conversation message...')
            : mode === 'mission'
              ? t('输入需要立即执行的任务...', 'Enter a mission to execute...')
              : t('输入问题或任务...', 'Ask a question or assign a mission...')}
          disabled={disabled}
        />
        <button onClick={submit} disabled={disabled || !value.trim()}>
          {disabled ? t('不可用', 'Unavailable') : t('发送', 'Send')}
        </button>
      </div>
    </section>
  )
}

export default function App() {
  const {
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
    submitAiTask,
    sendChat,
    stopExecution,
    initSystem,
    setMode,
    executeSkill,
    registerRobot,
    selectRobot,
    openCockpit,
    closeCockpit,
    getSocket,
  } = useSocket()

  const [now, setNow] = useState(() => new Date())
  const [language, setLanguage] = useState(() => {
    try {
      const saved = localStorage.getItem('aeroweaver-language')
      return saved === 'zh' ? 'zh' : DEFAULT_LANGUAGE
    } catch {
      return DEFAULT_LANGUAGE
    }
  })
  const [showFpv, setShowFpv] = useState(false)
  const [sceneMode, setSceneMode] = useState(false)
  const [activeFpv, setActiveFpv] = useState(null)
  const [activeSensor, setActiveSensor] = useState(null)
  const [selectedUavId, setSelectedUavId] = useState(null)
  const [showPayloadMenu, setShowPayloadMenu] = useState(false)
  const [skillPanelOpen, setSkillPanelOpen] = useState(false)
  const [rightPanelView, setRightPanelView] = useState('log')
  const [desiredUavCount, setDesiredUavCount] = useState(DEFAULT_UAV_COUNT)
  const [fleetSync, setFleetSync] = useState({ status: 'idle', message: '' })
  const [mapTools, setMapTools] = useState({ measure: false, area: false, layers: false, settings: false })
  const [layerOptions, setLayerOptions] = useState({ grid: true, roads: true, routes: false, labels: true })
  const [measurementPoints, setMeasurementPoints] = useState([])
  const [selectedTaskArea, setSelectedTaskArea] = useState(null)
  const [missionPrompt, setMissionPrompt] = useState('')
  const [mapPickRequest, setMapPickRequest] = useState(null)
  const [pickedMapPoint, setPickedMapPoint] = useState(null)
  const [trajectorySamples, setTrajectorySamples] = useState([])
  const [trajectoryRecording, setTrajectoryRecording] = useState(false)
  const [trajectorySource, setTrajectorySource] = useState('live')
  const [trajectorySessionId, setTrajectorySessionId] = useState(() => `track-${Date.now()}`)
  const trajectoryStartedAtRef = useRef(0)
  const trajectoryLastByUavRef = useRef(new Map())

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem('aeroweaver-language', language)
    } catch {
      // Local storage can be unavailable in private or embedded browser modes.
    }
  }, [language])

  const socket = getSocket()
  const sensorOptions = useMemo(() => localizedSensorOptions(language), [language])
  const missionUavCount = useMemo(
    () => inferMissionUavCount(logs, chatHistory, missionPrompt),
    [logs, chatHistory, missionPrompt],
  )
  const liveWorldUavCount = useMemo(
    () => Object.keys(worldState?.robots || {})
      .filter((robotId) => /^UAV/i.test(String(robotId).replace('_', '-')))
      .length,
    [worldState],
  )
  const uavs = useMemo(() => buildUavMarkers(worldState, missionUavCount, desiredUavCount), [worldState, missionUavCount, desiredUavCount])
  const trajectorySeries = useMemo(() => groupTrajectorySamples(trajectorySamples), [trajectorySamples])
  const selectedUav = useMemo(
    () => uavs.find((uav) => uav.id === selectedUavId) || null,
    [uavs, selectedUavId],
  )
  const timeline = useMemo(() => buildTimeline(logs, chatHistory, language), [logs, chatHistory, language])
  const fpvImage = cameraImage(sensorCamera, sensorCameras, activeFpv?.sensor)
  const sceneImage = sensorScene?.image
  const sceneImageUrl = sceneMode
    ? `${API_BASE}/api/sensor/relay/stream/scene`
    : ''
  const skillUav = selectedUav || uavs[0]
  const currentSkillRobot = skillUav?.robotId || systemStatus.current_robot || 'UAV_1'
  const currentSkillRobotAvailable = Boolean(skillUav?.canExecute && worldState?.robots?.[currentSkillRobot])
  const currentSkillRobotExecuting = Array.isArray(systemStatus.executing_robots)
    && systemStatus.executing_robots.includes(currentSkillRobot)

  useEffect(() => {
    if (!liveWorldUavCount || fleetSync.status === 'syncing') return
    setDesiredUavCount((current) => current === liveWorldUavCount ? current : liveWorldUavCount)
  }, [fleetSync.status, liveWorldUavCount])

  useEffect(() => {
    if (!trajectoryRecording) return

    const timestampMs = Date.now()
    if (!trajectoryStartedAtRef.current) trajectoryStartedAtRef.current = timestampMs
    const nextSamples = []

    for (const [robotId, robot] of Object.entries(worldState?.robots || {})) {
      if (!/^UAV/i.test(String(robotId).replace('_', '-'))) continue
      const position = readRobotPosition(robot)
      if (!position) continue

      const uavId = normalizeUavId(robotId)
      const previous = trajectoryLastByUavRef.current.get(uavId)
      const elapsedSincePrevious = previous ? timestampMs - Date.parse(previous.timestamp) : Infinity
      const movedSincePrevious = previous
        ? Math.hypot(
            position.north_m - previous.north_m,
            position.east_m - previous.east_m,
            position.down_m - previous.down_m,
          )
        : Infinity
      if (elapsedSincePrevious < 250 || (movedSincePrevious < 0.03 && elapsedSincePrevious < 1000)) continue

      const sample = {
        session_id: trajectorySessionId,
        timestamp: new Date(timestampMs).toISOString(),
        elapsed_s: Number(((timestampMs - trajectoryStartedAtRef.current) / 1000).toFixed(3)),
        uav_id: uavId,
        north_m: position.north_m,
        east_m: position.east_m,
        down_m: position.down_m,
        altitude_m: Number(Math.max(0, -position.down_m).toFixed(3)),
        speed_mps: readRobotSpeed(robot, previous, position, timestampMs),
        battery_percent: robot?.battery_percent ?? robot?.battery ?? null,
        source: 'live',
      }
      trajectoryLastByUavRef.current.set(uavId, sample)
      nextSamples.push(sample)
    }

    if (nextSamples.length) {
      setTrajectorySamples((previous) => [...previous, ...nextSamples].slice(-20000))
    }
  }, [trajectoryRecording, trajectorySessionId, worldState])

  useEffect(() => {
    if (selectedUavId && !uavs.some((uav) => uav.id === selectedUavId)) {
      setSelectedUavId(null)
      setShowPayloadMenu(false)
      setShowFpv(false)
      setActiveFpv(null)
      setActiveSensor(null)
    }
  }, [selectedUavId, uavs])

  const submitMission = (text, inputMode = 'auto') => {
    setRightPanelView('log')
    const areaContext = inputMode !== 'chat' ? taskAreaPrompt(selectedTaskArea, language) : ''
    const preparedText = areaContext ? `${text}

${areaContext}` : text

    if (inputMode === 'mission') {
      setMissionPrompt(preparedText)
      if (systemStatus.mode !== 'ai') setMode('ai')
      submitAiTask(preparedText, true)
      return
    }

    if (inputMode === 'auto' && systemStatus.mode !== 'ai') setMode('ai')
    sendChat(preparedText, inputMode, text)
  }

  const activateUav = (uav, openMenu = false) => {
    if (!uav) return null
    setSelectedUavId(uav.id)
    setShowPayloadMenu(openMenu)
    if (uav.canExecute) {
      if (systemStatus.current_robot !== uav.robotId) {
        selectRobot(uav.robotId)
      }
    } else if (systemStatus.initialized && registerRobot) {
      registerRobot({
        robot_id: uav.robotId,
        robot_type: 'UAV',
        initial_position: registrationPositionForUav(uav, desiredUavCount),
        battery: Math.max(55, 92 - Math.max(uavNumber(uav.id) - 1, 0) * 3),
      })
    }
    return uav
  }

  const openSensorMenu = (uav) => {
    activateUav(uav || selectedUav || uavs[0], true)
  }

  const openSensor = (uav, sensor = sensorOptions[0]) => {
    const target = activateUav(uav || selectedUav || uavs[0], sensor.type !== 'camera')
    if (!target) return
    setActiveSensor({ uavId: target.id, robotId: target.robotId, sensor: sensor.key, label: sensor.label, type: sensor.type })
    if (sensor.type === 'camera') {
      setActiveFpv({ uavId: target.id, robotId: target.robotId, sensor: sensor.key, label: sensor.label })
      setShowFpv(true)
      setShowPayloadMenu(false)
    } else {
      setShowFpv(false)
      setActiveFpv(null)
      setShowPayloadMenu(true)
    }
  }

  const openSkillVisualizer = (uav) => {
    const target = activateUav(uav || selectedUav || uavs[0])
    if (!target) return
    setSkillPanelOpen(true)
    setRightPanelView('skill')
    setShowPayloadMenu(false)
  }

  const openTrajectoryWorkspace = () => {
    setRightPanelView('tracks')
    setLayerOptions((previous) => ({ ...previous, routes: true }))
    setSceneMode(true)
    setShowPayloadMenu(false)
  }

  const startTrajectoryRecording = () => {
    const nowMs = Date.now()
    if (!trajectorySamples.length || trajectorySource !== 'live') {
      setTrajectorySamples([])
      setTrajectorySessionId(`track-${nowMs}`)
      trajectoryStartedAtRef.current = nowMs
      trajectoryLastByUavRef.current = new Map()
    } else if (!trajectoryStartedAtRef.current) {
      trajectoryStartedAtRef.current = Date.parse(trajectorySamples[0]?.timestamp) || nowMs
    }
    setTrajectorySource('live')
    setTrajectoryRecording(true)
    setLayerOptions((previous) => ({ ...previous, routes: true }))
    setRightPanelView('tracks')
  }

  const stopTrajectoryRecording = () => {
    setTrajectoryRecording(false)
  }

  const clearTrajectory = () => {
    setTrajectoryRecording(false)
    setTrajectorySamples([])
    setTrajectorySource('live')
    setTrajectorySessionId(`track-${Date.now()}`)
    trajectoryStartedAtRef.current = 0
    trajectoryLastByUavRef.current = new Map()
  }

  const loadDemoTrajectory = () => {
    const demo = createDemoTrajectorySamples()
    setTrajectoryRecording(false)
    setTrajectorySamples(demo)
    setTrajectorySource('demo')
    setTrajectorySessionId(demo[0]?.session_id || 'aeroweaver-demo-001')
    trajectoryStartedAtRef.current = Date.parse(demo[0]?.timestamp) || 0
    trajectoryLastByUavRef.current = new Map()
    setLayerOptions((previous) => ({ ...previous, routes: true }))
    setSceneMode(true)
    setRightPanelView('tracks')
  }

  const exportTrajectory = (format) => {
    if (!trajectorySamples.length) return
    const timestamp = new Date().toISOString().replaceAll(':', '-').replace(/\.\d{3}Z$/, 'Z')
    const baseName = `aeroweaver_trajectory_${timestamp}`
    const metadata = {
      sessionId: trajectorySessionId,
      source: trajectorySource,
      name: 'AeroWeaver multi-UAV trajectory',
    }
    if (format === 'csv') {
      downloadTextFile(`${baseName}.csv`, trajectoryCsv(trajectorySamples), 'text/csv;charset=utf-8')
      return
    }
    downloadTextFile(`${baseName}.json`, trajectoryJson(trajectorySamples, metadata), 'application/json;charset=utf-8')
  }

  const closeSkillVisualizer = () => {
    setSkillPanelOpen(false)
    setRightPanelView('log')
    setMapPickRequest(null)
  }

  const registerMissingUavs = (count) => {
    if (systemStatus.initialized && registerRobot) {
      for (let index = 0; index < count; index += 1) {
        const robotId = `UAV_${index + 1}`
        if (worldState?.robots?.[robotId]) continue
        const pos = fallbackUavPosition(index, count)
        registerRobot({
          robot_id: robotId,
          robot_type: 'UAV',
          initial_position: registrationPositionForUav(pos, count),
          battery: Math.max(55, 92 - index * 3),
        })
      }
    }
  }

  const applyDesiredUavCount = async (count) => {
    const nextCount = Math.round(clamp(Number(count) || DEFAULT_UAV_COUNT, 1, MAX_UAV_COUNT))
    const fleet = fleetRequestFromWorld(worldState, nextCount)
    setFleetSync({
      status: 'syncing',
      message: language === 'zh'
        ? `正在从 10 架备用池中激活 ${nextCount} 架无人机并同步位置。`
        : `Activating ${nextCount} UAVs from the 10-vehicle AirSim pool and synchronizing positions.`,
    })
    try {
      const response = await fetch(`${API_BASE}/api/fleet`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: nextCount, fleet }),
      })
      const result = await response.json()
      if (!response.ok || !result.ok) {
        throw new Error(result.error || `HTTP ${response.status}`)
      }
      setDesiredUavCount(nextCount)
      setFleetSync({
        status: 'success',
        message: language === 'zh'
          ? `编队已更新：${nextCount}/10 架无人机处于激活状态${result.restarted ? '，资源池已初始化。' : '，场景未重启。'}`
          : `Fleet updated: ${nextCount}/10 UAVs are active${result.restarted ? '; the vehicle pool was initialized.' : '; the scene was not restarted.'}`,
      })
    } catch (error) {
      setFleetSync({
        status: 'error',
        message: language === 'zh'
          ? `同步失败：${error.message}`
          : `Synchronization failed: ${error.message}`,
      })
    }
  }

  const toggleMapTool = (tool) => {
    setMapTools((prev) => ({
      measure: tool === 'measure' ? !prev.measure : false,
      area: tool === 'area' ? !prev.area : false,
      layers: tool === 'layers' ? !prev.layers : false,
      settings: tool === 'settings' ? !prev.settings : false,
    }))
    if (tool !== 'measure') setMeasurementPoints([])
    if (tool === 'measure' || tool === 'area') setMapPickRequest(null)
  }

  const toggleLayer = (layer) => {
    setLayerOptions((prev) => ({ ...prev, [layer]: !prev[layer] }))
  }

  const addMeasurementPoint = (point) => {
    setMeasurementPoints((prev) => {
      if (prev.length >= 2) return [point]
      return [...prev, point]
    })
  }

  return (
    <div className="mission-app">
      {cockpitOpen && (
        <CockpitView
          socket={socket}
          sensorCameras={sensorCameras}
          sensorLidar={sensorLidar}
          onClose={closeCockpit}
          initialView={cockpitInitialView}
          robotId={skillUav?.robotId || systemStatus.current_robot || 'UAV_1'}
          uavLabel={skillUav?.id || normalizeUavId(systemStatus.current_robot || 'UAV_1')}
          language={language}
        />
      )}

      <MissionHeader
        connected={connected}
        systemStatus={systemStatus}
        now={now}
        language={language}
        onLanguageChange={setLanguage}
        onInit={initSystem}
        onStop={stopExecution}
      />

      <main className="mission-main">
        <MissionMap
          uavs={uavs}
          selectedUavId={selectedUavId}
          activeFpv={activeFpv}
          activeSensor={activeSensor}
          language={language}
          sensorOptions={sensorOptions}
          showFpv={showFpv}
          sceneMode={sceneMode}
          onSetSceneMode={setSceneMode}
          showPayloadMenu={showPayloadMenu}
          skillPanelOpen={skillPanelOpen}
          mapTools={mapTools}
          layerOptions={layerOptions}
          measurementPoints={measurementPoints}
          selectedTaskArea={selectedTaskArea}
          desiredUavCount={desiredUavCount}
          fleetSync={fleetSync}
          onSelectUav={(uav) => activateUav(uav, true)}
          onOpenSensorMenu={openSensorMenu}
          onSelectSensor={openSensor}
          onOpenSkillPanel={openSkillVisualizer}
          onOpenTracks={openTrajectoryWorkspace}
          onOpenCockpit={(view) => openCockpit(view || activeFpv?.sensor || 'front')}
          onToggleMapTool={toggleMapTool}
          onToggleLayer={toggleLayer}
          onApplyUavCount={applyDesiredUavCount}
          onClearMeasurement={() => setMeasurementPoints([])}
          onMeasurePoint={addMeasurementPoint}
          onSelectTaskArea={(area) => {
            setSelectedTaskArea(area)
            setMapTools((previous) => ({ ...previous, area: false }))
          }}
          onClearTaskArea={() => setSelectedTaskArea(null)}
          onClosePayloadMenu={() => setShowPayloadMenu(false)}
          onCloseFpv={() => {
            setShowFpv(false)
            setActiveFpv(null)
          }}
          onExpandFpv={() => openCockpit(activeFpv?.sensor || 'front')}
          mapPickRequest={mapPickRequest}
          onMapPick={(point) => {
            if (!mapPickRequest) return
            const robot = worldState?.robots?.[mapPickRequest.robot]
            const rawPosition = robot?.position || robot?.pose || []
            const currentDown = Array.isArray(rawPosition)
              ? Number(rawPosition[2])
              : Number(rawPosition?.down ?? rawPosition?.d ?? rawPosition?.z)
            setPickedMapPoint({
              ...mapPickRequest,
              point: {
                ...point,
                d: Number.isFinite(currentDown) ? currentDown : undefined,
              },
              pickedId: Date.now(),
            })
            setMapPickRequest(null)
          }}
          onCancelMapPick={() => setMapPickRequest(null)}
          sceneImage={sceneImage}
          sceneImageUrl={sceneImageUrl}
          fpvImage={fpvImage}
          trajectorySeries={trajectorySeries}
          trajectoryRecording={trajectoryRecording}
          trajectorySampleCount={trajectorySamples.length}
          tracksWorkspaceOpen={rightPanelView === 'tracks'}
        />

        <aside className="right-column">
          <RightWorkspace
            activeView={rightPanelView}
            timeline={timeline}
            skillPanelOpen={skillPanelOpen}
            selectedUav={skillUav}
            language={language}
            sensorOptions={sensorOptions}
            sensorCameras={sensorCameras}
            sensorLidar={sensorLidar}
            activeSensor={activeSensor}
            worldState={worldState}
            skillCatalog={skillCatalog}
            connected={connected}
            systemStatus={systemStatus}
            aiThinking={aiThinking}
            aiThoughts={aiThoughts}
            aiStream={aiStream}
            lastAiPlan={lastAiPlan}
            lastAiReport={lastAiReport}
            trajectoryProps={{
              language,
              samples: trajectorySamples,
              recording: trajectoryRecording,
              source: trajectorySource,
              sceneMode,
              tracksVisible: layerOptions.routes,
              onStart: startTrajectoryRecording,
              onStop: stopTrajectoryRecording,
              onClear: clearTrajectory,
              onLoadDemo: loadDemoTrajectory,
              onExportCsv: () => exportTrajectory('csv'),
              onExportJson: () => exportTrajectory('json'),
              onSetSceneMode: setSceneMode,
              onToggleTracks: () => toggleLayer('routes'),
            }}
            skillPanelProps={{
              skillCatalog,
              currentRobot: currentSkillRobot,
              currentRobotAvailable: currentSkillRobotAvailable,
              language,
              worldState,
              isExecuting: currentSkillRobotExecuting,
              onExecuteSkill: executeSkill,
              lastResult: (
                lastSkillResult?.robot === currentSkillRobot
                || lastSkillResult?.robots?.includes(currentSkillRobot)
                || lastSkillResult?.output?.robots?.includes(currentSkillRobot)
              ) ? lastSkillResult : null,
              mapPickRequest,
              pickedMapPoint,
              onRequestMapPick: (request) => {
                setSelectedUavId(skillUav?.id || null)
                setMapTools((previous) => ({ ...previous, area: false, measure: false }))
                setMapPickRequest({ ...request, id: Date.now() })
              },
            }}
            onSetView={(view) => {
              setRightPanelView(view)
              if (view === 'tracks') {
                setLayerOptions((previous) => ({ ...previous, routes: true }))
                setSceneMode(true)
              }
            }}
            onShowLog={() => setRightPanelView('log')}
            onShowSkill={() => openSkillVisualizer(skillUav)}
            onSelectSensor={(sensor) => openSensor(skillUav, sensor)}
            onOpenCockpit={(view) => openCockpit(view || activeSensor?.sensor || 'front')}
            onSetMode={setMode}
            onCloseSkill={closeSkillVisualizer}
          />
          <TaskComposer
            language={language}
            disabled={!connected || !systemStatus.initialized}
            onSubmit={submitMission}
            taskArea={selectedTaskArea}
            onClearTaskArea={() => setSelectedTaskArea(null)}
          />
        </aside>
      </main>
    </div>
  )
}

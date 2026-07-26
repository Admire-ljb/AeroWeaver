/**
 * SkillPanel.jsx - UAV skill control panel.
 *
 * The UI follows the paper framing: basic skills are atomic control/sensing
 * capabilities, while advanced skills are task-level or compositional packages.
 */
import { useState, useCallback, useEffect, useRef } from 'react'

const DEFAULT_LANGUAGE = 'en'

function textFor(language, zh, en) {
  return language === 'zh' ? zh : en
}

function makeTranslator(language) {
  return (zh, en) => textFor(language, zh, en)
}

const LEVEL_COLORS = {
  basic: {
    bg: 'rgba(0,212,255,.06)',
    border: 'rgba(0,212,255,.24)',
    text: '#67e8f9',
    accent: '#00d4ff',
    badgeBg: 'rgba(0,212,255,.10)',
  },
  advanced: {
    bg: 'rgba(245,158,11,.08)',
    border: 'rgba(245,158,11,.32)',
    text: '#fbbf24',
    accent: '#f59e0b',
    badgeBg: 'rgba(245,158,11,.12)',
  },
}

const ADVANCED_SKILL_NAMES = new Set([
  'search_target',
  'rescue_person',
  'patrol_area',
  'scan_area',
  'fuse_perception',
  'swarm_rendezvous',
  'swarm_formation_hold',
  'swarm_orbit_hold',
])

const SKILL_PARAMS = {
  takeoff:          { altitude: 5.0 },
  land:             {},
  fly_to:           { target_position: [10, 0, -5], speed: 2.0 },
  hover:            { duration: 5.0 },
  get_position:     {},
  get_battery:      {},
  return_to_launch: {},
  change_altitude:  { altitude: 10.0 },
  fly_relative:     { forward: 5.0, right: 0.0, up: 0.0, speed: 5.0 },
  look_around:       { duration: 4.0 },
  mark_location:    { label: 'Observation point', priority: 'medium' },
  get_marks:        {},
  orbit_inspect:    { center: [10, 0], radius: 10.0, start_height: 8.0, end_height: 8.0, height_step: 5.0, points_per_layer: 4, speed: 5.0, focus: 'Inspect terrain and obstacles.' },
  swarm_rendezvous: { robot_ids: 'UAV_1,UAV_2,UAV_3', center_position: [40, 0, -25], formation: 'triangle', spacing: 12.0, speed: 6.0, post_action: 'hold', hold_duration: 0.0, duration: 12.0, angular_speed: 12.0 },
  swarm_formation_hold: { robot_ids: 'UAV_1,UAV_2,UAV_3', center_position: [40, 0, -25], formation: 'v', spacing: 12.0, speed: 6.0, hold_duration: 0.0 },
  swarm_orbit_hold: { robot_ids: 'UAV_1,UAV_2,UAV_3', center_position: [40, 0, -25], formation: 'circle', spacing: 12.0, speed: 6.0, duration: 12.0, angular_speed: 12.0 },
  observe:           { direction: 'front', focus: 'Describe the environment and nearby obstacles.' },
  perceive:          { direction: 'front', focus: 'Describe the environment and nearby obstacles.' },
  search_target:    { area_position: [50, 0, -20], scan_range: 30.0 },
  rescue_person:    { target_position: [120, 80, -15], rescue_position: [120, 80] },
  patrol_area:      { waypoints: [[0,0,-10],[20,0,-10],[20,20,-10],[0,20,-10]], scan_range: 25.0 },
  detect_object:    { target_label: 'person', camera_name: 'cam_front', confidence_threshold: 0.5 },
  recognize_speech: { transcript: '', language: 'zh-CN' },
  fuse_perception:  {},
  scan_area:        { center: [0, 0, -20], radius: 50.0 },
  get_sensor_data:  { sensor_types: ['all'] },
  run_python:       { code: 'print(sum(range(10)))' },
  http_request:     { url: 'https://example.com', method: 'GET' },
  read_file:        { path: 'robot_profile/WORLD_MAP.md' },
  write_file:       { path: 'data/operator_note.txt', content: 'AeroWeaver note' },
  report:           { content: 'Routine inspection completed.', severity: 'info' },
  alert:            { message: 'Operator test alert.', level: 'warning' },
  ask_user:         { question: 'Continue the current mission?' },
  update_map:       { landmark_name: 'Observation Point', description: 'Added from the skill panel.' },
}

const SKILL_ICONS = {
  takeoff:          '🚀',
  land:             '🛬',
  fly_to:           '✈️',
  hover:            '🔄',
  get_position:     '📍',
  get_battery:      '🔋',
  return_to_launch: '🏠',
  change_altitude:  '⬆️',
  fly_relative:     '↗️',
  look_around:      '🔄',
  mark_location:    '📌',
  get_marks:        '📋',
  orbit_inspect:    '🛰️',
  swarm_rendezvous: '◎',
  swarm_formation_hold: '△',
  swarm_orbit_hold: '↻',
  observe:          '📷',
  perceive:         '👁️',
  move_to:          '🚗',
  scan_lidar:       '📡',
  capture_image:    '📷',
  search_target:    '🔍',
  rescue_person:    '🚑',
  patrol_area:      '🗺️',
  detect_object:    '👁️',
  recognize_speech: '🎤',
  fuse_perception:  '🧠',
  scan_area:        '🌐',
  get_sensor_data:  '📡',
  run_python:       '⌨️',
  http_request:     '🌐',
  read_file:        '📖',
  write_file:       '💾',
  report:           '📝',
  alert:            '⚠️',
  ask_user:         '💬',
  update_map:       '🗺️',
}

const SKILL_LABELS = {
  takeoff:          'Take Off',
  land:             'Land',
  fly_to:           'Fly To',
  hover:            'Hover',
  get_position:     'Get Position',
  get_battery:      'Check Battery',
  return_to_launch: 'Return to Launch',
  change_altitude:  'Change Altitude',
  fly_relative:     'Relative Flight',
  look_around:      'Look Around',
  mark_location:    'Mark Location',
  get_marks:        'List Marks',
  orbit_inspect:    'Orbit Inspection',
  swarm_rendezvous: 'Swarm Rendezvous',
  swarm_formation_hold: 'Formation Hold',
  swarm_orbit_hold: 'Rotating Hold',
  observe:          'Observe',
  perceive:         'Active Perception',
  move_to:          'Move To',
  scan_lidar:       'LiDAR Scan',
  capture_image:    'Capture Image',
  search_target:    'Search Target',
  rescue_person:    'Rescue',
  patrol_area:      'Patrol Area',
  detect_object:    'Detect Object',
  recognize_speech: 'Recognize Speech',
  fuse_perception:  'Fuse Perception',
  scan_area:        'Scan Area',
  get_sensor_data:  'Sensor Data',
  run_python:       'Run Calculation',
  http_request:     'HTTP Request',
  read_file:        'Read File',
  write_file:       'Write File',
  report:           'Add Report',
  alert:            'Send Alert',
  ask_user:         'Ask Operator',
  update_map:       'Update Map',
  area_recon:       'Area Reconnaissance',
  building_inspect: 'Building Inspection',
  flight_safety:    'Flight Safety Experience',
  integrate_platform: 'Platform Integration',
  safe_approach:    'Safe Approach and Observation',
  smart_navigate:   'Smart Navigation',
}

const SKILL_LABELS_ZH = {
  takeoff:          '起飞',
  land:             '降落',
  fly_to:           '飞行到',
  hover:            '悬停',
  get_position:     '获取位置',
  get_battery:      '检查电量',
  return_to_launch: '返航',
  change_altitude:  '改变高度',
  fly_relative:     '相对飞行',
  look_around:      '环视',
  mark_location:    '标记位置',
  get_marks:        '查看标记',
  orbit_inspect:    '环绕巡检',
  swarm_rendezvous: '群体集合',
  swarm_formation_hold: '编队待命',
  swarm_orbit_hold: '旋转待命',
  observe:          '环境观察',
  perceive:         '主动感知',
  move_to:          '移动到',
  scan_lidar:       'LiDAR 扫描',
  capture_image:    '采集图像',
  search_target:    '搜索目标',
  rescue_person:    '救援',
  patrol_area:      '区域巡逻',
  detect_object:    '目标检测',
  recognize_speech: '语音识别',
  fuse_perception:  '感知融合',
  scan_area:        '区域扫描',
  get_sensor_data:  '传感器数据',
  run_python:       '运行计算',
  http_request:     '网络请求',
  read_file:        '读取文件',
  write_file:       '写入文件',
  report:           '添加报告',
  alert:            '发送警报',
  ask_user:         '询问操作员',
  update_map:       '更新地图',
  area_recon:       '环境侦察',
  building_inspect: '建筑巡检',
  flight_safety:    '飞行安全经验',
  integrate_platform: '平台接入',
  safe_approach:    '安全接近观察',
  smart_navigate:   '智能导航',
}

const SKILL_DESCRIPTIONS = {
  takeoff: 'Lift off to a target altitude.',
  land: 'Land safely at the current position.',
  fly_to: 'Fly to a specified NED position at the selected speed.',
  hover: 'Hold position for a specified duration.',
  get_position: 'Read the current vehicle position.',
  get_battery: 'Read battery percentage and power state.',
  return_to_launch: 'Return to the launch point.',
  change_altitude: 'Adjust altitude while keeping horizontal position.',
  fly_relative: 'Move forward, right, and vertically relative to the current heading.',
  look_around: 'Rotate in place once to inspect the surrounding scene.',
  mark_location: 'Save the current position as a named point of interest.',
  get_marks: 'List location marks saved by the current UAV.',
  orbit_inspect: 'Inspect a point or structure from a safe circular route.',
  swarm_rendezvous: 'Gather multiple UAVs around one center through separated altitude lanes, then hold or rotate.',
  swarm_formation_hold: 'Rearrange active UAVs into a safe triangle, circle, line, or V formation.',
  swarm_orbit_hold: 'Rotate a multi-UAV formation around one center while continuously monitoring separation.',
  observe: 'Capture and analyze a selected camera direction.',
  perceive: 'Ask a focused visual question about a selected camera direction.',
  move_to: 'Move toward a target point.',
  scan_lidar: 'Collect a LiDAR scan for nearby obstacles.',
  capture_image: 'Capture an image from the selected camera.',
  search_target: 'Search an area for a target.',
  rescue_person: 'Execute a rescue-oriented mission step.',
  patrol_area: 'Patrol a waypoint-defined area.',
  detect_object: 'Detect an object by label.',
  recognize_speech: 'Run speech recognition.',
  fuse_perception: 'Fuse perception inputs into a scene understanding.',
  scan_area: 'Scan a circular area around a center point.',
  get_sensor_data: 'Read payload sensor data.',
  run_python: 'Run a restricted calculation without file, network, or import access.',
  http_request: 'Fetch information from a public HTTP endpoint.',
  read_file: 'Read a UTF-8 file inside the server work directory.',
  write_file: 'Write a UTF-8 file inside the server work directory.',
  report: 'Add an observation to the live inspection report.',
  alert: 'Send a warning or critical notification to the operator.',
  ask_user: 'Ask the operator a question and wait for a reply.',
  update_map: 'Record a newly discovered landmark in the scene map.',
  area_recon: 'Survey an area to establish an overall understanding of the surrounding environment.',
  building_inspect: 'Inspect a building roof, facades, windows, and structural condition.',
  flight_safety: 'Apply validated flight-safety practices before and during vehicle movement.',
  integrate_platform: 'Generate, validate, and deploy an adapter for a new platform or device.',
  safe_approach: 'Approach a target incrementally while checking obstacles and flight safety.',
  smart_navigate: 'Plan a segmented route and avoid obstacles while flying to a distant target.',
}

const SKILL_DESCRIPTIONS_ZH = {
  takeoff: '起飞到目标高度。',
  land: '在当前位置安全降落。',
  fly_to: '按指定速度飞往 NED 目标位置。',
  hover: '在当前位置悬停指定时长。',
  get_position: '读取当前无人机位置。',
  get_battery: '读取电量百分比与供电状态。',
  return_to_launch: '返回起飞点。',
  change_altitude: '保持水平位置并调整高度。',
  fly_relative: '按当前航向向前、向右或垂直移动。',
  look_around: '在当前位置旋转一周观察周围环境。',
  mark_location: '将当前位置保存为命名兴趣点。',
  get_marks: '查看当前无人机保存的位置标记。',
  orbit_inspect: '沿安全圆形航线巡检目标或建筑。',
  swarm_rendezvous: '多架无人机通过独立高度通道在同一中心附近安全集合，并选择悬停或旋转待命。',
  swarm_formation_hold: '将当前无人机重排为三角形、圆形、直线或 V 形编队并保持待命。',
  swarm_orbit_hold: '多机编队围绕指定中心旋转，并持续监测无人机间距。',
  observe: '采集并分析指定方向的相机画面。',
  perceive: '针对指定相机方向提出视觉问题。',
  move_to: '向目标点移动。',
  scan_lidar: '采集附近障碍物的 LiDAR 扫描。',
  capture_image: '从指定相机采集图像。',
  search_target: '在区域内搜索目标。',
  rescue_person: '执行面向救援的任务步骤。',
  patrol_area: '按航点巡逻指定区域。',
  detect_object: '按标签检测目标。',
  recognize_speech: '执行语音识别。',
  fuse_perception: '融合感知输入形成场景理解。',
  scan_area: '扫描中心点周围的圆形区域。',
  get_sensor_data: '读取载荷传感器数据。',
  run_python: '在禁止文件、网络和导入的受限环境中运行计算。',
  http_request: '访问公网 HTTP 接口获取信息。',
  read_file: '读取服务器工作目录内的 UTF-8 文件。',
  write_file: '写入服务器工作目录内的 UTF-8 文件。',
  report: '向实时巡检报告添加一条观察记录。',
  alert: '向操作员发送警告或紧急通知。',
  ask_user: '向操作员提问并等待回复。',
  update_map: '将新发现的地标记录到场景地图。',
  area_recon: '快速了解指定区域，建立周边环境的整体认知。',
  building_inspect: '巡检建筑屋顶、外墙、窗户和结构状况。',
  flight_safety: '在无人机移动前和飞行过程中应用经过验证的安全经验。',
  integrate_platform: '为新平台或设备生成、验证并部署适配器。',
  safe_approach: '逐步接近目标，同时持续检查障碍物与飞行安全。',
  smart_navigate: '分段规划航线并避开障碍，安全飞往远距离目标。',
}

function filterTabs(language) {
  const t = makeTranslator(language)
  return [
    { key: 'all', label: t('全部', 'All') },
    { key: 'basic', label: t('基础技能', 'Basic Skills') },
    { key: 'advanced', label: t('高级技能', 'Advanced Skills') },
  ]
}

const PARAM_LABELS = {
  altitude: 'Target altitude',
  target_position: 'Target position [N, E, D]',
  speed: 'Flight speed',
  duration: 'Duration',
  forward: 'Forward distance',
  right: 'Right distance',
  up: 'Upward distance',
  label: 'Mark label',
  priority: 'Priority',
  start_height: 'Start height',
  end_height: 'End height',
  height_step: 'Height step',
  points_per_layer: 'Points per layer',
  focus: 'Analysis focus',
  direction: 'Camera direction',
  camera_name: 'Camera name',
  confidence_threshold: 'Confidence threshold',
  transcript: 'Speech transcript',
  language: 'Speech language',
  sensor_types: 'Sensor types',
  code: 'Calculation code',
  url: 'Public URL',
  method: 'HTTP method',
  data: 'Request body',
  path: 'Relative file path',
  content: 'Content',
  severity: 'Severity',
  message: 'Alert message',
  level: 'Alert level',
  question: 'Question',
  landmark_name: 'Landmark name',
  description: 'Description',
  scan_range: 'Scan range',
  camera_type: 'Camera type',
  area_position: 'Area position [N, E, D]',
  rescue_position: 'Rescue position [N, E]',
  waypoints: 'Waypoint list',
  target_label: 'Target label',
  center: 'Area center [N, E, D]',
  radius: 'Scan radius',
  sensor_type: 'Sensor type',
  robot_ids: 'Participating UAV IDs',
  center_position: 'Formation center [N, E, D]',
  formation: 'triangle | circle | line | v',
  spacing: 'Minimum spacing (m)',
  post_action: 'hold | orbit',
  hold_duration: 'Fixed hover duration',
  angular_speed: 'Rotation speed (deg/s)',
}

const PARAM_LABELS_ZH = {
  altitude: '目标高度',
  target_position: '目标位置 [N, E, D]',
  speed: '飞行速度',
  duration: '持续时间',
  forward: '向前距离',
  right: '向右距离',
  up: '向上距离',
  label: '标记名称',
  priority: '优先级',
  start_height: '起始高度',
  end_height: '结束高度',
  height_step: '高度间隔',
  points_per_layer: '每层航点数',
  focus: '分析重点',
  direction: '相机方向',
  camera_name: '相机名称',
  confidence_threshold: '置信度阈值',
  transcript: '语音转写文本',
  language: '语音语言',
  sensor_types: '传感器类型',
  code: '计算代码',
  url: '公网地址',
  method: 'HTTP 方法',
  data: '请求正文',
  path: '相对文件路径',
  content: '内容',
  severity: '严重程度',
  message: '警报内容',
  level: '警报等级',
  question: '问题',
  landmark_name: '地标名称',
  description: '描述',
  scan_range: '扫描范围',
  camera_type: '相机类型',
  area_position: '区域位置 [N, E, D]',
  rescue_position: '救援位置 [N, E]',
  waypoints: '航点列表',
  target_label: '目标标签',
  center: '区域中心 [N, E, D]',
  radius: '扫描半径',
  sensor_type: '传感器类型',
  robot_ids: '参与的无人机 ID',
  center_position: '编队中心 [N, E, D]',
  formation: 'triangle | circle | line | v',
  spacing: '最小间距（米）',
  post_action: 'hold | orbit',
  hold_duration: '固定悬停时长',
  angular_speed: '旋转速度（度/秒）',
}

function robotTypeFromId(robotId) {
  return /^UAV/i.test(String(robotId || '').replace('_', '-')) ? 'UAV' : 'UGV'
}

function skillLevel(skill) {
  const explicit = String(skill.skill_level || skill.level || skill.category || '').toLowerCase()
  if (explicit.includes('advanced') || explicit.includes('高级') || explicit.includes('mission') || explicit.includes('composite')) {
    return 'advanced'
  }
  if (explicit.includes('basic') || explicit.includes('基础') || explicit.includes('primitive') || explicit.includes('atomic')) {
    return 'basic'
  }
  if (ADVANCED_SKILL_NAMES.has(skill.name) || skill.skill_type === 'soft') return 'advanced'
  return 'basic'
}

function levelLabel(level, language = DEFAULT_LANGUAGE) {
  return level === 'advanced'
    ? textFor(language, '高级', 'Advanced')
    : textFor(language, '基础', 'Basic')
}

function levelStyles(level) {
  return LEVEL_COLORS[level] || LEVEL_COLORS.basic
}

function isPositionParam(key) {
  return /position|waypoints|center|point|coord/i.test(key)
}

export function skillLabel(skill, language = DEFAULT_LANGUAGE) {
  const labels = language === 'zh' ? SKILL_LABELS_ZH : SKILL_LABELS
  if (labels[skill.name]) return labels[skill.name]
  if (language === 'zh') return skill.name
  return String(skill.name || 'Skill')
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function paramLabel(key, language = DEFAULT_LANGUAGE) {
  const labels = language === 'zh' ? PARAM_LABELS_ZH : PARAM_LABELS
  return labels[key] || null
}

function schemaEntriesFor(skill, params, language = DEFAULT_LANGUAGE) {
  const rawSchema = skill.input_schema?.properties || skill.input_schema || {}
  const keys = new Set([...Object.keys(rawSchema), ...Object.keys(params || {})])
  return Array.from(keys).map((key) => {
    const raw = rawSchema[key]
    const schemaDescription = raw && typeof raw === 'object'
      ? (raw.description || raw.title || raw.type)
      : raw
    return [
      key,
      paramLabel(key, language) || schemaDescription || textFor(language, '参数', 'Parameter'),
    ]
  })
}

export function skillDescription(skill, language = DEFAULT_LANGUAGE) {
  if (language === 'zh') {
    return skill.description_zh || SKILL_DESCRIPTIONS_ZH[skill.name] || skill.description || skill.description_en || skill.name
  }
  const fallback = skill.description || skill.name
  return skill.description_en
    || SKILL_DESCRIPTIONS[skill.name]
    || (/[\u3400-\u9fff]/.test(String(fallback)) ? 'No English description available.' : fallback)
}

function robotStatusText(status, language = DEFAULT_LANGUAGE) {
  const normalized = String(status || 'idle').toLowerCase()
  const labels = {
    idle: ['空闲', 'Idle'],
    executing: ['执行中', 'Executing'],
    airborne: ['飞行中', 'Airborne'],
  }
  const [zh, en] = labels[normalized] || [status || '空闲', status || 'Idle']
  return textFor(language, zh, en)
}

function executionStatusText(status, language = DEFAULT_LANGUAGE) {
  if (!status || status === 'never') return textFor(language, '未执行', 'Not run')
  if (status === 'success') return textFor(language, '上次成功', 'Last success')
  return textFor(language, '上次失败', 'Last failed')
}

function mapPointToParamValue(key, currentValue, point) {
  const n = Number(point.n.toFixed(1))
  const e = Number(point.e.toFixed(1))
  const current = Array.isArray(currentValue) ? currentValue : []

  if (key === 'rescue_position' || current.length === 2) {
    return [n, e]
  }

  const pickedDown = Number(point.d)
  const altitude = Number.isFinite(pickedDown)
    ? Number(pickedDown.toFixed(1))
    : typeof current[2] === 'number'
      ? current[2]
    : key === 'area_position' || key === 'center'
      ? -20
      : -10
  const position = [n, e, altitude]

  if (key === 'waypoints') {
    const existing = Array.isArray(currentValue) && Array.isArray(currentValue[0]) ? currentValue : []
    return existing.length > 0 ? [...existing, position] : [position]
  }

  return position
}

function findCatalogSkills(skillCatalog, currentRobot, currentRobotType) {
  if (skillCatalog[currentRobot]) return skillCatalog[currentRobot]

  const normalized = String(currentRobot || '').replace('-', '_')
  if (skillCatalog[normalized]) return skillCatalog[normalized]

  const fallbackEntry = Object.entries(skillCatalog).find(([robotId]) => (
    robotTypeFromId(robotId) === currentRobotType
  ))
  return fallbackEntry?.[1] || []
}

function SkillBadge({ level, language }) {
  const colors = levelStyles(level)
  return (
    <span
      className="badge"
      style={{
        color: colors.text,
        background: colors.badgeBg,
        border: `1px solid ${colors.border}`,
        fontSize: 9,
      }}
    >
      {levelLabel(level, language)}
    </span>
  )
}

export default function SkillPanel({
  skillCatalog = {},
  currentRobot,
  currentRobotAvailable = true,
  language = DEFAULT_LANGUAGE,
  worldState = { robots: {} },
  isExecuting,
  onExecuteSkill,
  lastResult,
  pickedMapPoint,
  onRequestMapPick,
}) {
  const t = makeTranslator(language)
  const [selectedSkill, setSelectedSkill] = useState(null)
  const [params, setParams] = useState({})
  const [activeFilter, setActiveFilter] = useState('all')
  const [pendingMapParamKey, setPendingMapParamKey] = useState(null)
  const lastPickRef = useRef(null)

  const robotExists = Boolean(currentRobotAvailable && worldState.robots?.[currentRobot])
  const currentRobotType = worldState.robots?.[currentRobot]?.robot_type || robotTypeFromId(currentRobot)
  const robotSkills = robotExists ? findCatalogSkills(skillCatalog, currentRobot, currentRobotType) : []
  const filtered = activeFilter === 'all'
    ? robotSkills
    : robotSkills.filter((skill) => skillLevel(skill) === activeFilter)
  const tabs = filterTabs(language)
  const byLevel = (level) => level === 'all'
    ? robotSkills.length
    : robotSkills.filter((skill) => skillLevel(skill) === level).length

  const handleSelectSkill = useCallback((skill) => {
    setSelectedSkill(skill)
    setParams({ ...(SKILL_PARAMS[skill.name] || {}) })
    setPendingMapParamKey(null)
  }, [setSelectedSkill, setParams, setPendingMapParamKey])

  const handleExecute = useCallback(() => {
    if (!selectedSkill || isExecuting || !robotExists) return
    onExecuteSkill(currentRobot, selectedSkill.name, params)
  }, [selectedSkill, params, currentRobot, isExecuting, robotExists, onExecuteSkill])

  const requestMapPick = useCallback((paramKey) => {
    if (!selectedSkill || !onRequestMapPick || !robotExists) return
    setPendingMapParamKey(paramKey)
    onRequestMapPick({ robot: currentRobot, skill: selectedSkill.name, paramKey })
  }, [currentRobot, selectedSkill, robotExists, onRequestMapPick, setPendingMapParamKey])

  useEffect(() => {
    if (!pickedMapPoint || !selectedSkill) return
    if (pickedMapPoint.pickedId === lastPickRef.current) return
    if (pickedMapPoint.robot !== currentRobot || pickedMapPoint.skill !== selectedSkill.name) return

    const key = pickedMapPoint.paramKey || pendingMapParamKey
    if (!key) return

    lastPickRef.current = pickedMapPoint.pickedId
    setParams((prev) => ({
      ...prev,
      [key]: mapPointToParamValue(key, prev[key], pickedMapPoint.point),
    }))
    setPendingMapParamKey(null)
  }, [pickedMapPoint, selectedSkill, currentRobot, pendingMapParamKey])

  const robotStatus = worldState.robots?.[currentRobot]?.status || 'idle'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, height: '100%', overflow: 'hidden' }}>
      <div className="card" style={{ padding: '8px 12px', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 16 }}>{currentRobotType === 'UAV' ? '✈️' : '🚗'}</span>
          <span style={{ fontWeight: 600, color: 'var(--accent)' }}>{currentRobot}</span>
          <span className={`badge ${robotStatus}`}>
            {robotStatusText(robotStatus, language)}
          </span>
          <span className={`badge ${currentRobotType?.toLowerCase()}`}>{currentRobotType}</span>
          {!robotExists && <span className="badge failed">{t('未注册', 'Unregistered')}</span>}
          <span style={{ marginLeft: 'auto', color: 'var(--text-dim)', fontSize: 11 }}>
            {t('电量', 'Battery')}: {(worldState.robots?.[currentRobot]?.battery || 0).toFixed(0)}%
          </span>
        </div>
      </div>

      {!robotExists && (
        <div className="card" style={{
          padding: 10,
          color: '#fbbf24',
          borderColor: 'rgba(245,158,11,.35)',
          background: 'rgba(245,158,11,.06)',
          fontSize: 11,
          flexShrink: 0,
        }}>
          {t('该无人机尚未注册到 WorldModel。请在地图设置中确认无人机数量，或等待后端注册后再执行技能。', 'This UAV is not registered in the WorldModel yet. Apply the UAV count in map settings, or wait for backend registration before executing skills.')}
        </div>
      )}

      <div style={{ display: 'flex', gap: 4, flexShrink: 0, alignItems: 'center', flexWrap: 'wrap' }}>
        {tabs.map((tab) => {
          const count = byLevel(tab.key)
          const isActive = activeFilter === tab.key
          return (
            <button
              key={tab.key}
              onClick={() => setActiveFilter(tab.key)}
              style={{
                padding: '3px 10px',
                borderRadius: 99,
                border: `1px solid ${isActive ? 'var(--accent)' : 'var(--border)'}`,
                background: isActive ? 'rgba(0,212,255,.12)' : 'transparent',
                color: isActive ? 'var(--accent)' : 'var(--text-dim)',
                fontSize: 10,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              {tab.label}
              <span style={{
                background: isActive ? 'rgba(0,212,255,.2)' : 'rgba(255,255,255,.06)',
                borderRadius: 99,
                padding: '0 5px',
                fontSize: 9,
              }}>{count}</span>
            </button>
          )
        })}
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
          {t('位置参数可用地图取点填充', 'Position parameters can be filled by map picking')}
        </span>
      </div>

      <div style={{ flex: 1, minHeight: 0, display: 'flex', gap: 10, overflow: 'hidden' }}>
        <div style={{
          flex: 1,
          minWidth: 0,
          overflowY: 'auto',
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
          gap: 6,
          alignContent: 'start',
          paddingRight: 2,
        }}>
          {filtered.length === 0 && (
            <div style={{
              gridColumn: '1 / -1',
              textAlign: 'center',
              color: 'var(--text-muted)',
              fontSize: 12,
              padding: 20,
            }}>
              {t(`当前机器人没有可用技能（${currentRobotType}）`, `No available skills for the current robot (${currentRobotType})`)}
            </div>
          )}
          {filtered.map((skill) => {
            const level = skillLevel(skill)
            const colors = levelStyles(level)
            const isSelected = selectedSkill?.name === skill.name
            const statusColor = skill.last_execution_status === 'success' ? 'var(--success)'
              : skill.last_execution_status === 'failed' ? 'var(--danger)'
              : 'var(--text-muted)'

            return (
              <button
                key={skill.name}
                type="button"
                onClick={() => handleSelectSkill(skill)}
                style={{
                  padding: '10px 8px',
                  borderRadius: 'var(--radius)',
                  border: `1px solid ${isSelected ? colors.accent : colors.border}`,
                  background: isSelected ? colors.bg : 'var(--bg-card)',
                  cursor: 'pointer',
                  transition: 'all .15s',
                  boxShadow: isSelected ? `0 0 8px ${colors.accent}44` : 'none',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 4,
                  color: 'var(--text)',
                  textAlign: 'left',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                  <span style={{ fontSize: 16 }}>{SKILL_ICONS[skill.name] || '⚙️'}</span>
                  <SkillBadge level={level} language={language} />
                </div>
                <div style={{ fontWeight: 600, fontSize: 12, color: isSelected ? colors.text : 'var(--text)' }}>
                  {skillLabel(skill, language)}
                </div>
                <div style={{ fontSize: 10, color: 'var(--text-dim)', lineHeight: 1.4 }}>
                  {skillDescription(skill, language).slice(0, 42)}...
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{ width: 5, height: 5, borderRadius: '50%', background: statusColor }} />
                  <span style={{ color: statusColor, fontSize: 9 }}>
                    {executionStatusText(skill.last_execution_status, language)}
                  </span>
                </div>
              </button>
            )
          })}
        </div>

        {selectedSkill && (
          <SkillDetail
            skill={selectedSkill}
            params={params}
            currentRobot={currentRobot}
            language={language}
            robotAvailable={robotExists}
            isExecuting={isExecuting}
            pendingMapParamKey={pendingMapParamKey}
            onParamChange={setParams}
            onRequestMapPick={requestMapPick}
            onExecute={handleExecute}
            onClose={() => {
              setSelectedSkill(null)
              setPendingMapParamKey(null)
            }}
          />
        )}
      </div>

      {lastResult && <SkillResultCard result={lastResult} language={language} />}
    </div>
  )
}

function SkillDetail({
  skill,
  params,
  currentRobot,
  language,
  robotAvailable,
  isExecuting,
  pendingMapParamKey,
  onParamChange,
  onRequestMapPick,
  onExecute,
  onClose,
}) {
  const t = makeTranslator(language)
  const level = skillLevel(skill)
  const colors = levelStyles(level)

  return (
    <div className="card" style={{
      flex: '0 0 250px',
      minWidth: 230,
      padding: 12,
      overflowY: 'auto',
      borderColor: colors.border,
      background: 'rgba(14, 22, 38, 0.96)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
        <span style={{ fontSize: 18 }}>{SKILL_ICONS[skill.name] || '⚙️'}</span>
        <span style={{ fontWeight: 700, color: colors.text }}>
          {skillLabel(skill, language)}
        </span>
        <SkillBadge level={level} language={language} />
        <button className="btn" onClick={onClose} aria-label={t('关闭技能详情', 'Close skill details')} style={{ marginLeft: 'auto', padding: '3px 7px' }}>
          ✕
        </button>
      </div>

      <ParamEditor
        skill={skill}
        params={params}
        language={language}
        pendingMapParamKey={pendingMapParamKey}
        onChange={onParamChange}
        onRequestMapPick={onRequestMapPick}
      />

      <button
        className="btn primary"
        onClick={onExecute}
        disabled={isExecuting || !robotAvailable}
        style={{ width: '100%', marginTop: 12, justifyContent: 'center' }}
      >
        {!robotAvailable ? t('等待注册', 'Waiting for registration') : isExecuting ? t('执行中...', 'Running...') : t(`执行 [${currentRobot}]`, `Run [${currentRobot}]`)}
      </button>
    </div>
  )
}

function ParamEditor({ skill, params, language, pendingMapParamKey, onChange, onRequestMapPick }) {
  const t = makeTranslator(language)
  const entries = schemaEntriesFor(skill, params, language)
  const [speechStatus, setSpeechStatus] = useState('idle')

  if (entries.length === 0) {
    return (
      <div style={{ color: 'var(--text-dim)', fontSize: 11, padding: '4px 0' }}>
        {t('该技能无需输入参数', 'This skill does not require parameters')}
      </div>
    )
  }

  const updateParam = (key, rawValue) => {
    let value = rawValue
    try {
      value = JSON.parse(rawValue)
    } catch {
      // Keep user-entered text as-is.
    }
    onChange((prev) => ({ ...prev, [key]: value }))
  }

  const startSpeechRecognition = () => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SpeechRecognition) {
      setSpeechStatus('unsupported')
      return
    }
    const recognition = new SpeechRecognition()
    recognition.lang = String(params.language || (language === 'zh' ? 'zh-CN' : 'en-US'))
    recognition.interimResults = true
    recognition.continuous = false
    setSpeechStatus('listening')
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results)
        .map((result) => result[0]?.transcript || '')
        .join('')
      onChange((prev) => ({ ...prev, transcript }))
    }
    recognition.onerror = () => setSpeechStatus('error')
    recognition.onend = () => setSpeechStatus((status) => status === 'error' ? status : 'idle')
    recognition.start()
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {entries.map(([key, desc]) => {
        const val = params[key]
        const displayVal = val === undefined ? '' : (typeof val === 'object' ? JSON.stringify(val) : String(val))
        const canPick = isPositionParam(key)
        const picking = pendingMapParamKey === key
        const canSpeak = skill.name === 'recognize_speech' && key === 'transcript'
        const isLongText = ['code', 'content', 'description', 'focus', 'message', 'question'].includes(key)
        const selectOptions = key === 'formation'
          ? ['triangle', 'circle', 'line', 'v']
          : key === 'post_action'
            ? ['hold', 'orbit']
            : null
        const inputProps = {
          value: displayVal,
          onChange: (event) => updateParam(key, event.target.value),
          placeholder: String(desc).slice(0, 50),
          style: {
            fontSize: 11,
            minWidth: 0,
            flex: 1,
            resize: isLongText ? 'vertical' : undefined,
            minHeight: isLongText ? 58 : undefined,
          },
        }

        return (
          <div key={key}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3, gap: 8 }}>
              <span style={{ color: 'var(--accent)', fontSize: 11, fontWeight: 600 }}>{key}</span>
              <span style={{ color: 'var(--text-dim)', fontSize: 10, textAlign: 'right' }}>{String(desc).slice(0, 28)}</span>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              {selectOptions
                ? (
                  <select
                    {...inputProps}
                    onChange={(event) => updateParam(key, event.target.value)}
                  >
                    {selectOptions.map((option) => <option key={option} value={option}>{option}</option>)}
                  </select>
                )
                : isLongText
                  ? <textarea {...inputProps} rows={3} />
                  : <input {...inputProps} />}
              {canPick && (
                <button
                  className={picking ? 'btn primary' : 'btn'}
                  onClick={() => onRequestMapPick(key)}
                  style={{ flex: '0 0 72px', padding: '5px 6px', justifyContent: 'center', fontSize: 10 }}
                >
                  {picking ? t('取点中', 'Picking') : t('地图取点', 'Pick on Map')}
                </button>
              )}
              {canSpeak && (
                <button
                  className={speechStatus === 'listening' ? 'btn primary' : 'btn'}
                  type="button"
                  onClick={startSpeechRecognition}
                  title={t('使用浏览器麦克风识别语音', 'Recognize speech with the browser microphone')}
                  style={{ flex: '0 0 72px', padding: '5px 6px', justifyContent: 'center', fontSize: 10 }}
                >
                  {speechStatus === 'listening'
                    ? t('聆听中', 'Listening')
                    : speechStatus === 'unsupported'
                      ? t('不支持', 'Unavailable')
                      : t('麦克风', 'Microphone')}
                </button>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function SkillResultCard({ result, language }) {
  const t = makeTranslator(language)
  const ok = result.ok
  return (
    <div style={{
      padding: 10,
      borderRadius: 'var(--radius)',
      border: `1px solid ${ok ? 'rgba(34,197,94,.3)' : 'rgba(239,68,68,.3)'}`,
      background: ok ? 'rgba(34,197,94,.05)' : 'rgba(239,68,68,.05)',
      flexShrink: 0,
      animation: 'fadeIn .2s ease',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <span>{ok ? '✅' : '❌'}</span>
        <span style={{ fontWeight: 600, color: ok ? 'var(--success)' : 'var(--danger)', fontSize: 12 }}>
          {ok ? t('执行成功', 'Execution Succeeded') : t('执行失败', 'Execution Failed')}
        </span>
        <span style={{ color: 'var(--text-dim)', fontSize: 10, marginLeft: 'auto' }}>
          [{result.robot}] {result.skill} · {result.cost_time?.toFixed(2)}s
        </span>
      </div>
      {ok && result.output && Object.keys(result.output).length > 0 && (
        <div style={{ color: 'var(--text-dim)', fontSize: 10, fontFamily: 'monospace' }}>
          {Object.entries(result.output).map(([key, value]) => (
            <div key={key}>
              <span style={{ color: 'var(--accent)' }}>{key}</span>: {JSON.stringify(value)}
            </div>
          ))}
        </div>
      )}
      {!ok && result.error && (
        <div style={{ color: 'var(--danger)', fontSize: 11 }}>{result.error}</div>
      )}
    </div>
  )
}

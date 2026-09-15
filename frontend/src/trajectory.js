const TRAJECTORY_COLORS = [
  '#00d4ff',
  '#ff9f43',
  '#8b5cf6',
  '#22c55e',
  '#ff4fa3',
  '#f5d547',
  '#38bdf8',
  '#fb7185',
]

function finiteNumber(value, fallback = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function rounded(value, digits = 3) {
  return Number(finiteNumber(value).toFixed(digits))
}

function sampleTime(sample) {
  const parsed = Date.parse(sample?.timestamp)
  return Number.isFinite(parsed) ? parsed : 0
}

function distance3d(from, to) {
  return Math.hypot(
    finiteNumber(to.north_m) - finiteNumber(from.north_m),
    finiteNumber(to.east_m) - finiteNumber(from.east_m),
    finiteNumber(to.down_m) - finiteNumber(from.down_m),
  )
}

export function trajectoryColor(vehicleId) {
  const numericId = Number(String(vehicleId || '').match(/(\d+)/)?.[1])
  if (Number.isFinite(numericId) && numericId > 0) {
    return TRAJECTORY_COLORS[(numericId - 1) % TRAJECTORY_COLORS.length]
  }
  const hash = [...String(vehicleId || '')].reduce((total, char) => total + char.charCodeAt(0), 0)
  return TRAJECTORY_COLORS[hash % TRAJECTORY_COLORS.length]
}

export function readRobotPosition(robot) {
  const raw = robot?.position || robot?.pose || robot?.position_ned
  if (!raw) return null
  const values = Array.isArray(raw)
    ? raw
    : [
        raw.north ?? raw.n ?? raw.x,
        raw.east ?? raw.e ?? raw.y,
        raw.down ?? raw.d ?? raw.z,
      ]
  if (values.length < 2 || !Number.isFinite(Number(values[0])) || !Number.isFinite(Number(values[1]))) {
    return null
  }
  return {
    north_m: rounded(values[0]),
    east_m: rounded(values[1]),
    down_m: rounded(values[2]),
  }
}

export function readRobotSpeed(robot, previousSample, position, timestampMs) {
  const raw = robot?.velocity || robot?.linear_velocity
  if (raw) {
    const values = Array.isArray(raw)
      ? raw
      : [raw.north ?? raw.n ?? raw.x, raw.east ?? raw.e ?? raw.y, raw.down ?? raw.d ?? raw.z]
    if (values.some((value) => Number.isFinite(Number(value)))) {
      return rounded(Math.hypot(...values.map((value) => finiteNumber(value))))
    }
  }
  if (!previousSample) return 0
  const elapsedSeconds = Math.max((timestampMs - sampleTime(previousSample)) / 1000, 0.001)
  return rounded(distance3d(previousSample, position) / elapsedSeconds)
}

export function groupTrajectorySamples(samples) {
  const grouped = new Map()
  for (const sample of samples || []) {
    const vehicleId = sample.uav_id || 'UAV'
    if (!grouped.has(vehicleId)) grouped.set(vehicleId, [])
    grouped.get(vehicleId).push(sample)
  }
  return [...grouped.entries()]
    .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }))
    .map(([vehicleId, vehicleSamples]) => ({
      vehicleId,
      color: trajectoryColor(vehicleId),
      samples: vehicleSamples.sort((left, right) => sampleTime(left) - sampleTime(right)),
    }))
}

export function summarizeTrajectory(samples) {
  const series = groupTrajectorySamples(samples)
  const timestamps = (samples || []).map(sampleTime).filter(Boolean)
  const startedAtMs = timestamps.length ? Math.min(...timestamps) : 0
  const endedAtMs = timestamps.length ? Math.max(...timestamps) : 0
  const vehicleStats = series.map((item) => {
    let distanceM = 0
    for (let index = 1; index < item.samples.length; index += 1) {
      distanceM += distance3d(item.samples[index - 1], item.samples[index])
    }
    const latest = item.samples[item.samples.length - 1]
    return {
      uav_id: item.vehicleId,
      color: item.color,
      samples: item.samples.length,
      distance_m: rounded(distanceM, 1),
      latest_speed_mps: rounded(latest?.speed_mps, 1),
      latest_battery_percent: latest?.battery_percent == null ? null : rounded(latest.battery_percent, 1),
    }
  })
  return {
    vehicle_count: series.length,
    sample_count: (samples || []).length,
    duration_s: startedAtMs && endedAtMs ? rounded((endedAtMs - startedAtMs) / 1000, 1) : 0,
    total_distance_m: rounded(vehicleStats.reduce((sum, item) => sum + item.distance_m, 0), 1),
    started_at: startedAtMs ? new Date(startedAtMs).toISOString() : null,
    ended_at: endedAtMs ? new Date(endedAtMs).toISOString() : null,
    vehicles: vehicleStats,
  }
}

export function createDemoTrajectorySamples(options = {}) {
  const vehicleCount = Math.max(1, Math.min(Number(options.vehicleCount) || 4, 8))
  const stepCount = Math.max(12, Number(options.stepCount) || 181)
  const intervalMs = Math.max(250, Number(options.intervalMs) || 500)
  const startTimeMs = Number(options.startTimeMs) || Date.now() - (stepCount - 1) * intervalMs
  const sessionId = options.sessionId || 'aeroweaver-demo-001'
  const keyframePaths = [
    [[9, 20], [16, 14], [24, 18], [34, 26], [46, 34], [58, 44], [62, 53], [58, 72], [70, 82], [82, 89], [94, 75], [88, 62], [79, 52]],
    [[13, 10], [24, 15], [36, 22], [44, 31], [43, 44], [55, 43], [67, 49], [80, 55], [92, 64], [97, 69], [93, 74], [84, 71], [80, 57]],
    [[11, 18], [22, 22], [31, 27], [39, 29], [48, 37], [59, 45], [64, 53], [59, 60], [61, 77], [71, 85], [83, 81], [91, 71], [83, 58]],
    [[8, 23], [5, 14], [12, 7], [20, 6], [28, 14], [38, 20], [47, 29], [55, 37], [61, 45], [65, 53], [61, 58], [53, 50]],
  ]
  const interpolatePath = (keyframes, t, vehicleIndex) => {
    const scaled = Math.min(t, 0.999999) * (keyframes.length - 1)
    const segment = Math.floor(scaled)
    const progress = scaled - segment
    const from = keyframes[segment]
    const to = keyframes[Math.min(segment + 1, keyframes.length - 1)]
    const jitterEnvelope = Math.sin(Math.PI * t)
    const jitterX = jitterEnvelope * (
      1.5 * Math.sin(Math.PI * (18 * t + vehicleIndex * 0.7))
      + 0.6 * Math.sin(Math.PI * (43 * t + vehicleIndex))
    )
    const jitterY = jitterEnvelope * (
      1.2 * Math.sin(Math.PI * (21 * t + vehicleIndex * 0.5))
      + 0.5 * Math.cos(Math.PI * (37 * t + vehicleIndex * 0.8))
    )
    const xPct = from[0] + (to[0] - from[0]) * progress + jitterX
    const yPct = from[1] + (to[1] - from[1]) * progress + jitterY
    return {
      east: (xPct - 50) * 2,
      north: (50 - yPct) * 2,
      altitude: 18 + vehicleIndex * 2 + 2.2 * Math.sin(Math.PI * (2 * t + vehicleIndex * 0.25)),
    }
  }
  const rows = []
  const previousByVehicle = new Map()
  for (let step = 0; step < stepCount; step += 1) {
    const t = step / (stepCount - 1)
    const timestampMs = startTimeMs + step * intervalMs
    for (let vehicleIndex = 0; vehicleIndex < vehicleCount; vehicleIndex += 1) {
      const vehicleId = `UAV-${vehicleIndex + 1}`
      const keyframes = keyframePaths[vehicleIndex % keyframePaths.length]
      const point = interpolatePath(keyframes, t, vehicleIndex)
      const previous = previousByVehicle.get(vehicleId)
      const current = {
        north_m: rounded(point.north),
        east_m: rounded(point.east),
        down_m: rounded(-point.altitude),
      }
      const speed = previous
        ? distance3d(previous, current) / (intervalMs / 1000)
        : 0
      rows.push({
        session_id: sessionId,
        timestamp: new Date(timestampMs).toISOString(),
        elapsed_s: rounded(step * intervalMs / 1000, 1),
        uav_id: vehicleId,
        north_m: current.north_m,
        east_m: current.east_m,
        down_m: current.down_m,
        altitude_m: rounded(point.altitude),
        speed_mps: rounded(speed),
        battery_percent: rounded(96 - t * (7 + vehicleIndex * 1.4), 1),
        source: 'demo',
      })
      previousByVehicle.set(vehicleId, rows[rows.length - 1])
    }
  }
  return rows
}

export function trajectoryDocument(samples, metadata = {}) {
  const summary = summarizeTrajectory(samples)
  return {
    schema: 'aeroweaver.trajectory.v1',
    session: {
      id: metadata.sessionId || samples?.[0]?.session_id || `track-${Date.now()}`,
      name: metadata.name || 'AeroWeaver UAV trajectory',
      source: metadata.source || samples?.[0]?.source || 'live',
      coordinate_frame: 'NED',
      units: {
        position: 'm',
        elapsed: 's',
        speed: 'm/s',
        battery: '%',
      },
      started_at: summary.started_at,
      ended_at: summary.ended_at,
    },
    summary,
    samples: samples || [],
  }
}

function csvCell(value) {
  const text = value == null ? '' : String(value)
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

export function trajectoryCsv(samples) {
  const columns = [
    'session_id',
    'timestamp',
    'elapsed_s',
    'uav_id',
    'north_m',
    'east_m',
    'down_m',
    'altitude_m',
    'speed_mps',
    'battery_percent',
    'source',
  ]
  const rows = (samples || []).map((sample) => columns.map((column) => csvCell(sample[column])).join(','))
  return `\uFEFF${columns.join(',')}\n${rows.join('\n')}\n`
}

export function trajectoryJson(samples, metadata = {}) {
  return `${JSON.stringify(trajectoryDocument(samples, metadata), null, 2)}\n`
}

export function downloadTextFile(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

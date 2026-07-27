# AeroWeaver

<p align="right">
  <strong>English</strong> · <a href="https://github.com/Admire-ljb/AeroWeaver/tree/zh">中文</a>
</p>

[![Research foundation: TALKER](https://img.shields.io/badge/Research%20Foundation-TALKER-0A66C2)](https://doi.org/10.1109/LRA.2024.3511434)

AeroWeaver is a Web-based multi-UAV coordination system for operating,
observing, and orchestrating autonomous aerial vehicles. It combines live
telemetry, sensor views, direct skill execution, collision-aware formation
control, trajectory export, and optional LLM-driven mission planning in one
bilingual console.

The runtime supports AirSim, PX4/Gazebo, and a dependency-light mock adapter.
The same registered skill layer is available in both operator-controlled and
LLM-controlled workflows.

## Highlights

- Multi-UAV fleet synchronization, selection, status, position, and battery data
- Visible-light FPV, directional cameras, LiDAR, IMU, GPS, and distance sensors
- Basic and advanced skill catalog with map-based position selection
- Independent per-UAV execution with robot-level locking
- Collision-aware rendezvous, formation hold, and rotating standby skills
- Manual cockpit control and autonomous skill execution
- Trajectory recording, visualization, JSON/CSV export, and replay-ready data
- Chinese and English interface
- Remote AirSim camera relay support

## Web Console

![AeroWeaver Web console with three mock UAVs](docs/images/web-console.jpg)

The console combines the live fleet map, per-UAV selection, sensor and cockpit
entry points, skill visualization, trajectory tools, execution logs, and
mission input. The language switch in the header changes both UI controls and
runtime messages.

## Operating Modes

### Manual Mode

Manual mode does not require an LLM or an API key. The operator selects a UAV
from the map or fleet list, opens its payload or skill panel, enters parameters,
and executes the skill directly.

Use manual mode for:

- cockpit control and direct flight commands;
- validating sensors and vehicle mappings;
- testing one skill at a time;
- assigning different skills to different UAVs concurrently;
- executing multi-UAV rendezvous and formation skills with explicit parameters.

The backend only accepts direct skill execution while the system is in manual
mode. Each command is checked against the selected robot and the registered
skill schema before execution.

### LLM Mode

LLM mode accepts a natural-language mission from the mission input panel. The
configured model interprets the request, selects registered skills, generates a
plan, and dispatches actions through the same execution layer used by manual
mode.

Use LLM mode for:

- natural-language mission decomposition;
- multi-step reconnaissance and inspection tasks;
- skill selection and parameter generation;
- plan reflection and mission progress reporting;
- coordinated task assignment across multiple UAVs.

The model does not bypass the runtime. Robot reservations, skill registration,
parameter validation, adapter checks, interrupt handling, and flight safety
guards still apply. An OpenAI-compatible endpoint or a local Ollama endpoint can
be configured from `.env` or from the Web console.

## Architecture

```text
Web console (React + Socket.IO)
        |
Flask coordination server
        |
Manual dispatcher or LLM planner
        |
Registered basic, advanced, and swarm skills
        |
Per-UAV execution channels and safety guards
        |
Mock | AirSim | PX4/Gazebo adapters
```

## Quick Start With Mock Vehicles

Requirements:

- Python 3.10 or newer
- Node.js 20 or newer
- npm 10 or newer

```bash
git clone https://github.com/Admire-ljb/AeroWeaver.git
cd AeroWeaver

python -m venv .venv
source .venv/bin/activate
pip install -r requirements-mock.txt

cd ui
npm ci
npm run build
cd ..

SIM_ADAPTER=mock AEROWEAVER_UAV_COUNT=3 python server.py
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-mock.txt

Set-Location ui
npm ci
npm run build
Set-Location ..

$env:SIM_ADAPTER = "mock"
$env:AEROWEAVER_UAV_COUNT = "3"
python server.py
```

Open [http://127.0.0.1:5001](http://127.0.0.1:5001).

## AirSim

Set AirSim to listen on an address reachable by the AeroWeaver backend. The
vehicle names should follow `Drone_1`, `Drone_2`, and so on. The current fleet
manager supports a reserve pool of up to ten vehicles and exposes only the
active subset in the Web console.

```bash
cp .env.example .env
```

Configure at least:

```dotenv
SIM_ADAPTER=airsim
AIRSIM_HOST=127.0.0.1
AIRSIM_PORT=41451
AEROWEAVER_UAV_COUNT=3

# Optional browser camera relay running near AirSim
AIRSIM_CAMERA_RELAY_ENABLED=true
AIRSIM_CAMERA_RELAY_URL=http://127.0.0.1:8765
```

Then build the UI and start the server:

```bash
cd ui && npm ci && npm run build && cd ..
python server.py
```

For a remote AirSim instance, set `AIRSIM_HOST` to its reachable address. Keep
the RPC port and camera relay behind a trusted network or tunnel; they are not
designed as public Internet services.

## Enabling LLM Mode

Copy the example environment file and configure an OpenAI-compatible endpoint:

```dotenv
ACTIVE_PROVIDER=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=replace-with-your-key
LLM_MODEL=gpt-4o

VLM_BASE_URL=https://api.openai.com/v1
VLM_API_KEY=replace-with-your-key
VLM_MODEL=gpt-4o
```

Local Ollama is also supported:

```dotenv
ACTIVE_PROVIDER=ollama_local
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=qwen2.5:7b
```

After startup:

1. Open the Web console.
2. Confirm the adapter and fleet are online.
3. Configure or select the model in the model settings panel.
4. Switch from **Manual** to **LLM** mode.
5. Enter a mission in the mission input panel.
6. Monitor planning, robot reservations, skill execution, and results in the
   execution log.

API keys and runtime model settings are local files and are ignored by Git.

## Swarm Skills

| Skill | Purpose |
| --- | --- |
| `swarm_rendezvous` | Gather selected UAVs around a map-selected center |
| `swarm_formation_hold` | Form a triangle, circle, line, or V and hold |
| `swarm_orbit_hold` | Rotate a formation around a center while monitoring separation |

The swarm coordinator uses altitude-layered approach paths, independent AirSim
control channels, minimum-separation monitoring, terrain-aware altitude
leveling, and final slot verification.

## Real Examples

The following screenshot is from an AirSim-connected three-UAV formation test.
It shows synchronized vehicle positions and a live FPV sensor window during
swarm execution.

![AirSim-connected multi-UAV formation test](docs/images/airsim-multi-uav.webp)

### 1. Map-Selected Flight

1. Switch to **Manual** mode and select `UAV-1`.
2. Open **Visualize Skill**, select `fly_to`, and click **Pick on Map**.
3. Choose a point and execute with `speed=15`.
4. AeroWeaver sends the command only to `UAV_1`, preserves a terrain-safe
   altitude, and updates its map position and FPV view from telemetry.

Equivalent skill input:

```json
{
  "target_position": [41, 62, -8],
  "speed": 15
}
```

### 2. Collision-Aware Three-UAV Orbit

Select the three active UAVs and execute `swarm_rendezvous` with:

```json
{
  "robot_ids": "UAV_1,UAV_2,UAV_3",
  "center_position": [41, 62, -8],
  "formation": "triangle",
  "spacing": 8,
  "speed": 15,
  "post_action": "orbit",
  "duration": 20,
  "angular_speed": 8
}
```

The coordinator assigns separate slots and altitude-layered approach paths,
moves the UAVs concurrently, monitors minimum separation, and rotates the
completed formation around the selected center.

### 3. Natural-Language Mission

After configuring an LLM, switch to **LLM** mode and submit:

> Send UAV-1, UAV-2, and UAV-3 to rendezvous around the selected clearing,
> form an 8-meter triangle, then orbit for 20 seconds while maintaining safe
> separation.

The planner maps the request to registered swarm skills. The same parameter
validation, per-UAV execution channels, adapter checks, and safety guards used
by Manual mode remain active.

## Development

Backend tests:

```bash
python -m pytest
```

Focused multi-UAV regression suite:

```bash
python -m unittest \
  tests.test_swarm_skills \
  tests.test_multi_uav_adapter_context \
  tests.test_basic_skills
```

Frontend:

```bash
cd ui
npm ci
npm run lint
npm run build
```

## Repository Layout

```text
adapters/       Vehicle and simulator adapters
brain/          LLM planning and conversational mission control
memory/         World, episodic, skill, and experience memory
perception/     Passive perception and vision-language analysis
runtime/        Plan and skill execution runtime
skills/         Basic, advanced, perception, and swarm skills
sim/            Gazebo sensor bridge and simulation assets
swarm/          Distributed coordination helpers
tests/          Backend, adapter, protocol, and safety tests
ui/             React operations console
```

## Research Foundation

AeroWeaver continues the research direction established by **TALKER**:
task-activated LLM reasoning for UAV missions, reusable action primitives and
skills, and knowledge extension through interaction. AeroWeaver develops this
line further as an independently maintained multi-UAV operations system with
manual and LLM modes, simulator adapters, per-UAV execution channels, swarm
skills, and a bilingual Web console.

> J. Lou, R. Shi, Y. Lin, Q. Wang, and W. Wu, "TALKER: A Task-Activated
> Language Model Based Knowledge-Extension Reasoning System," *IEEE Robotics
> and Automation Letters*, vol. 10, no. 2, pp. 1026-1033, 2025.
> [doi:10.1109/LRA.2024.3511434](https://doi.org/10.1109/LRA.2024.3511434)

```bibtex
@article{lou2025talker,
  author  = {Lou, Jiabin and Shi, Rongye and Lin, Yuxin and Wang, Qunbo and Wu, Wenjun},
  title   = {TALKER: A Task-Activated Language Model Based Knowledge-Extension Reasoning System},
  journal = {IEEE Robotics and Automation Letters},
  year    = {2025},
  volume  = {10},
  number  = {2},
  pages   = {1026--1033},
  doi     = {10.1109/LRA.2024.3511434}
}
```

## Safety

This is a research system. Validate all commands in simulation before using any
physical aircraft. Configure geofencing, altitude limits, emergency stop
behavior, and operator supervision independently of the LLM.

## License

MIT. See [LICENSE](LICENSE).

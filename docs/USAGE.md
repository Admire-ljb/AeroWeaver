# AeroWeaver User Guide

[← Project overview](../README.md) · [中文指南](USAGE_CN.md)

Setup, configuration, and examples for the AeroWeaver runtime. For task definitions,
reward functions, and evaluation details, see the [paper appendix](PAPER_APPENDIX.md).
Run commands from the repository root unless a step changes directories.

- [Mock setup, including Windows](#quick-start-with-mock-vehicles)
- [Operating modes](#operating-modes) and [architecture](#architecture)
- [AirSim](#airsim) and [LLM configuration](#enabling-llm-mode)
- [Swarm skills](#swarm-skills) and [worked examples](#real-examples)
- [Experience and MPE2 scenarios](#role-centric-experience-and-mpe2-scenarios)
- [PX4/Gazebo and Docker](#px4gazebo-and-docker)
- [Development](#development) and [repository layout](#repository-layout)

## Quick Start With Mock Vehicles

Requirements:

- Python 3.10 or newer
- Node.js 22.12 or newer (Node.js 20.19+ is also supported)
- npm 10 or newer

```bash
git clone https://github.com/Admire-ljb/AeroWeaver.git
cd AeroWeaver

python -m venv .venv
source .venv/bin/activate
pip install -r requirements/mock.txt

cd frontend
npm ci
npm run build
cd ..

SIM_ADAPTER=mock AEROWEAVER_UAV_COUNT=3 python backend/server.py
```

On Windows PowerShell, clone the repository and enter its directory first, then run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements/mock.txt

Set-Location frontend
npm ci
npm run build
Set-Location ..

$env:SIM_ADAPTER = "mock"
$env:AEROWEAVER_UAV_COUNT = "3"
python backend/server.py
```

Open [http://127.0.0.1:5001](http://127.0.0.1:5001).

Mock vehicles use a three-axis point-mass model inspired by MPE: physical damping, bounded acceleration, speed limiting, and fixed-step position integration. Flight skills therefore produce continuous trajectories instead of teleporting. Set `AEROWEAVER_MOCK_REALTIME_FACTOR` to control simulated time relative to wall-clock time (default: `2.0`).

## Operating Modes

The Web console labels these modes **Manual** and **Autonomous**.

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

The **Autonomous** mode accepts a natural-language mission. Commander initializes
the task and assigns roles and local goals. Each UAV agent uses its own context
to select registered skills and acts through its own execution channel.

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
Manual dispatcher or role-conditioned local agents
        |
Registered basic, advanced, and swarm skills
        |
Per-UAV execution channels and safety guards
        |
Mock | AirSim | PX4/Gazebo adapters
```

## AirSim

The default server path is the dependency-light MPE-style mock runtime. To
connect an optional UE4/AirSim backend, start AirSim on an address reachable by
the AeroWeaver backend, open the map `Settings` panel, enter the UE4 IP and RPC
port (normally `41451`), and click `Connect AirSim`. A failed connection keeps
the mock runtime active. The vehicle names should follow `Drone_1`, `Drone_2`,
and so on; the fleet manager exposes only the active subset in the Web console.

For headless startup, the same backend can still be selected through `.env`:

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
cd frontend && npm ci && npm run build && cd ..
python backend/server.py
```

For a remote AirSim instance, set `AIRSIM_HOST` to its reachable address. Keep
the RPC port and camera relay behind a trusted network or tunnel; they are not
designed as public Internet services.

## Enabling LLM Mode

Provide an OpenAI-compatible endpoint through environment variables or the
model settings panel:

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
4. Switch from **Manual** to **Autonomous** mode.
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

![AirSim-connected multi-UAV formation test](images/airsim-multi-uav.webp)

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

After configuring an LLM, switch to **Autonomous** mode and submit:

> Send UAV-1, UAV-2, and UAV-3 to rendezvous around the selected clearing,
> form an 8-meter triangle, then orbit for 20 seconds while maintaining safe
> separation.

The planner maps the request to registered swarm skills. The same parameter
validation, per-UAV execution channels, adapter checks, and safety guards used
by Manual mode remain active.

## Role-Centric Experience and MPE2 Scenarios

Experience records are keyed by a semantic `role` such as `searcher`, `tracker`,
or `pursuer`; `agent_id` is retained only as an execution-instance and audit field.
This lets interchangeable UAV instances share outcomes without treating `UAV_1`
and `UAV_6` as different capabilities. The API exposes the catalog at
`GET /api/environments/mpe` and can probe one local environment with
`POST /api/environments/mpe/<scenario_id>/probe`.

The optional `requirements/mpe2.txt` adapter registers the official Farama MPE2
suite. It is the reproducible boundary for the MPE family; arbitrary research
forks derived from MPE do not share one guaranteed package or entry-point API.

| Scenario | Family | Semantic roles |
| --- | --- | --- |
| `simple` | debug | agent |
| `simple_adversary` | deception | good_agent, adversary |
| `simple_crypto` | communication | sender, receiver, eavesdropper |
| `simple_formation` | formation | formation_member |
| `simple_line` | formation | formation_member |
| `simple_push` | interaction | pusher, adversary |
| `simple_reference` | communication | speaker, listener |
| `simple_speaker_listener` | communication | speaker, listener |
| `simple_spread` | coverage | searcher |
| `simple_tag` | pursuit | pursuer, evader |
| `simple_world_comm` | partial observation | leader_adversary, adversary, good_agent |
| `collect_treasure` | transport | collector, depositor |

Install the adapter only when these environments are needed:

```powershell
pip install -r requirements/mpe2.txt
```

Experience is stored in SQLite at `backend/data/swarm_experience/trajectories.sqlite3`
by default; set `AEROWEAVER_EXPERIENCE_PATH` to use a different location. Reuse is
restricted to matching tasks and semantic roles with observed rewards. Provider
log-probabilities and retrieved skill advantages guide selection without changing
model weights. The console's Memory workspace exposes the recorded trajectories.

## PX4/Gazebo and Docker

- [PX4/Gazebo quickstart](../scripts/sim_quickstart.sh): local simulator setup and startup.
- [PX4/Gazebo diagnostics](../scripts/doctor_gazebo.sh): dependency and runtime checks.
- [Mock Compose configuration](../deploy/compose.mock.yml) and
  [Gazebo Compose configuration](../deploy/compose.gazebo.yml): container deployments.
- [Example environment configuration](../.env.example): available runtime settings.

## Development

Backend tests:

```bash
pip install -r requirements/mock.txt -r requirements/experiments.txt pytest
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
cd frontend
npm ci
npm run lint
npm run build
```

## Repository Layout

```text
backend/        Python service, adapters, agents, skills, and simulation assets
frontend/       React operations console
deploy/         Dockerfiles and Compose definitions
docs/           Documentation, screenshots, and the Chinese README source
requirements/   Python dependency groups
scripts/        Startup, diagnostics, and repository maintenance
tests/          Backend, adapter, protocol, and safety tests
```

## Safety

This is a research system. Validate all commands in simulation before using any
physical aircraft. Configure geofencing, altitude limits, emergency stop
behavior, and operator supervision independently of the LLM.

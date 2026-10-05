# <img src="docs/images/aeroweaver-wordmark.svg" alt="AeroWeaver" width="360" style="background-color: transparent;">

**An embodied-agent harness for distributed, adaptive UAV swarms.**

[<u>Project Homepage</u>](https://anonymous.4open.science/w/AeroWeaver-D028/) · [<u>Demo Video</u>](https://anonymous.4open.science/w/AeroWeaver-D028/#demo-video)

AeroWeaver turns natural-language missions into coordinated multi-UAV execution.
It connects LLM agents to registered skills, gives each UAV its own decision
context and execution channel, and reuses role-specific experience to adapt skill
selection without retraining the model.

![AeroWeaver autonomous swarm execution with live inter-UAV communication](docs/images/aeroweaver-swarm-console.png)

## Core Capabilities

- **Skill grounding** — Registered skills connect model decisions to executable actions, with parameter validation and runtime guards.
- **Distributed execution** — Commander assigns roles and local goals; each UAV agent observes, communicates, and controls its own vehicle.
- **Experience reuse** — Observed rewards from matching tasks and roles refine future skill selection without updating model weights.
- **Operator console** — A bilingual Web interface for manual control, autonomous missions, live telemetry, sensor views, and trajectory inspection.

Supports **Mock**, **AirSim**, and **PX4/Gazebo** adapters. Start with Mock to explore the console without an external simulator or API key.

## Quick Start

Requires **Python 3.10+**, **Node.js 22.12+** (or 20.19+), and **npm 10+**.

Download and extract the [anonymous source ZIP](https://anonymous.4open.science/api/repo/AeroWeaver-D028/zip), then open the project directory containing `backend/` and `frontend/` and run:

```bash
python -m venv .venv
```

Activate the environment: `source .venv/bin/activate` on Linux/macOS, or `.\.venv\Scripts\Activate.ps1` in Windows PowerShell. Then run:

```bash
pip install -r requirements/mock.txt
npm --prefix frontend ci
npm --prefix frontend run build
python backend/server.py
```

Open **[http://127.0.0.1:5001](http://127.0.0.1:5001)**. A fresh checkout starts in Mock and Manual mode. Select a UAV and try a skill from the console.

For language-driven missions, [configure a model](docs/USAGE.md#enabling-llm-mode), switch to **Autonomous**, and enter a task such as:

> Have UAV-1, UAV-2, and UAV-3 form a triangle with 8-meter spacing, then orbit for 20 seconds.

## Documentation

| Need | Read |
| --- | --- |
| Setup, operating modes, AirSim, LLMs, and skill examples | [User guide](docs/USAGE.md) |
| Task definitions, rewards, runtime evidence, and evaluation | [Paper appendix](docs/PAPER_APPENDIX.md) |
| Tests and repository structure | [Development guide](docs/USAGE.md#development) |
| PX4/Gazebo and container deployment | [Deployment options](docs/USAGE.md#px4gazebo-and-docker) |

## Safety & License

Research software: validate in simulation before physical flight, with independent geofencing, emergency stop, and operator supervision. [MIT License](LICENSE).

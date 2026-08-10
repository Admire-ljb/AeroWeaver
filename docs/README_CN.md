# AeroWeaver

<p align="right">
  <a href="https://github.com/Admire-ljb/AeroWeaver/tree/main">English</a> · <strong>中文</strong>
</p>

[![研究基础：TALKER](https://img.shields.io/badge/Research%20Foundation-TALKER-0A66C2)](https://doi.org/10.1109/LRA.2024.3511434)

AeroWeaver 是一个面向多无人机协同控制的 Web 系统，集成无人机状态同步、传感器画面、驾驶舱控制、技能执行、编队控制、轨迹导出和可选的 LLM 任务规划。

系统支持 AirSim、PX4/Gazebo 和 Mock 适配器，并提供中英文界面。

## Web 控制台

![包含三架 Mock 无人机的 AeroWeaver Web 控制台](docs/images/web-console.jpg)

控制台将实时机队地图、逐机选择、传感器与驾驶舱入口、技能可视化、轨迹工具、执行日志和任务输入集中在同一界面。页面顶部的语言开关会同时切换操作控件和运行消息。

## 两种运行模式

### 手动模式

手动模式不需要配置 LLM。操作者从地图或左下角机队列表选择无人机，然后直接打开传感器、驾驶舱或技能面板。

适用于：

- 直接控制指定无人机；
- 检查摄像头、LiDAR、IMU、GPS 和底部测距；
- 通过地图取点执行飞行技能；
- 同时向不同无人机下发独立技能；
- 显式设置无人机列表、集合点、编队和安全间距。

手动技能只能在手动模式下执行。后端会检查目标无人机、技能参数、机器人占用状态和适配器连接状态。

### LLM 模式

LLM 模式接收自然语言任务，由配置的模型解析任务、选择已注册技能、生成计划并通过统一执行层下发。

适用于：

- 自然语言任务分解；
- 多步骤侦察、巡检和搜索；
- 技能选择与参数生成；
- 多无人机任务分配；
- 任务执行反思和状态汇报。

LLM 不会绕过安全控制。机器人占用锁、技能注册检查、参数校验、中断机制、地形保护和编队防碰撞仍然生效。

## Mock 快速启动

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

Mock 无人机使用受 MPE 启发的三轴点质量运动学模型，包含物理阻尼、加速度限制、速度上限和固定时间步位置积分，因此飞行技能会生成连续轨迹而不是瞬移。可通过 `AEROWEAVER_MOCK_REALTIME_FACTOR` 调整仿真时间相对于实际时间的倍率，默认值为 `2.0`。

浏览器打开 [http://127.0.0.1:5001](http://127.0.0.1:5001)。

## 接入 AirSim

打开 AirSim 即可，AeroWeaver 后端启动后会连接仿真器。只有需要修改仿真器地址、活动无人机数量或相机中继时，才需要调整以下可选配置：

```dotenv
SIM_ADAPTER=airsim
AIRSIM_HOST=127.0.0.1
AIRSIM_PORT=41451
AEROWEAVER_UAV_COUNT=3
AIRSIM_CAMERA_RELAY_ENABLED=true
AIRSIM_CAMERA_RELAY_URL=http://127.0.0.1:8765
```

AirSim 中的无人机名称使用 `Drone_1`、`Drone_2` 等形式。系统可以维护最多十架备用无人机，网页只显示当前激活的无人机。

## 配置 LLM

OpenAI 兼容接口：

```dotenv
ACTIVE_PROVIDER=openai
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=replace-with-your-key
LLM_MODEL=gpt-4o
```

本地 Ollama：

```dotenv
ACTIVE_PROVIDER=ollama_local
OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
OLLAMA_MODEL=qwen2.5:7b
```

启动后在网页模型设置中确认模型，然后切换到 **LLM 模式**，在任务输入框中输入自然语言任务。

## 群体技能

- `swarm_rendezvous`：多机防碰撞集合；
- `swarm_formation_hold`：三角形、圆形、直线或 V 字编队保持；
- `swarm_orbit_hold`：保持安全间距并围绕中心旋转待命。

群体控制器采用分层进场、独立控制通道、最小间距监测、地形安全高度统一和最终槽位校验。

## 真实示例

下图来自接入 AirSim 后的三机编队测试，展示了群体技能执行期间同步的无人机位置和实时 FPV 传感器窗口。

![AirSim 三机编队联调](docs/images/airsim-multi-uav.webp)

### 1. 地图取点飞行

1. 切换到**手动模式**并选择 `UAV-1`。
2. 打开**可视化 Skill**，选择 `fly_to`，然后点击**地图取点**。
3. 在地图选择目标，并以 `speed=15` 执行。
4. AeroWeaver 只向 `UAV_1` 下发指令，在保持地形安全高度的同时，通过遥测更新地图位置和 FPV 画面。

对应技能参数：

```json
{
  "target_position": [41, 62, -8],
  "speed": 15
}
```

### 2. 三机防碰撞旋转编队

选择三架活动无人机，执行 `swarm_rendezvous`：

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

协调器会为各无人机分配独立槽位和分层进场路径，并发移动无人机、监测最小间距，最后围绕地图选定中心旋转编队。

### 3. 自然语言任务

配置 LLM 后切换到 **LLM 模式**，输入：

> 让 UAV-1、UAV-2 和 UAV-3 在选定空地区域集合，组成间距 8 米的三角编队，然后在保持安全间距的情况下旋转待命 20 秒。

规划器会把任务映射到已注册的群体技能。手动模式使用的参数校验、逐机执行通道、适配器检查和安全保护仍然全部生效。

## 测试

```bash
python -m pytest

cd frontend
npm ci
npm run lint
npm run build
```

## 仓库结构

```text
backend/        Python 服务、适配器、智能体、技能与仿真资源
frontend/       React Web 操作界面
deploy/         Dockerfile 与 Compose 部署定义
docs/           文档、界面截图与中文版 README 源文件
requirements/   按用途拆分的 Python 依赖
scripts/        启动、诊断与仓库维护脚本
tests/          后端、适配器、协议与安全测试
```

## 研究基础

AeroWeaver 延续了 **TALKER** 所建立的研究方向，包括面向无人机任务的任务激活式 LLM 推理、可复用动作原语与技能，以及基于交互的知识扩展。在此基础上，AeroWeaver 进一步发展为独立维护的多无人机操作系统，提供手动与 LLM 两种模式、仿真器适配、逐机并发执行、群体技能和中英文 Web 控制台。

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

## 安全说明

本项目是研究系统。接入真实无人机前必须先在仿真环境验证，并独立配置地理围栏、高度限制、紧急停止和人工监督。

## 许可证

MIT，详见 [LICENSE](LICENSE)。

# AeroWeaver

AeroWeaver 是一个面向多无人机协同控制的 Web 系统，集成无人机状态同步、传感器画面、驾驶舱控制、技能执行、编队控制、轨迹导出和可选的 LLM 任务规划。

系统支持 AirSim、PX4/Gazebo 和 Mock 适配器，并提供中英文界面。

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
pip install -r requirements-mock.txt

cd ui
npm ci
npm run build
cd ..

SIM_ADAPTER=mock AEROWEAVER_UAV_COUNT=3 python server.py
```

浏览器打开 [http://127.0.0.1:5001](http://127.0.0.1:5001)。

## 接入 AirSim

复制配置文件：

```bash
cp .env.example .env
```

填写 AirSim 地址和无人机数量：

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

## 测试

```bash
python -m pytest

cd ui
npm ci
npm run lint
npm run build
```

## 安全说明

本项目是研究系统。接入真实无人机前必须先在仿真环境验证，并独立配置地理围栏、高度限制、紧急停止和人工监督。

## 许可证

MIT，详见 [LICENSE](LICENSE)。

# AeroWeaver

**面向分布式、自适应无人机集群的具身智能体运行框架。**

[![arXiv](https://img.shields.io/badge/arXiv-2609.18520-b31b1b.svg)](https://arxiv.org/abs/2609.18520)

[English](https://github.com/Admire-ljb/AeroWeaver/blob/main/README.md) · [论文](https://arxiv.org/abs/2609.18520) · [论文附录](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/PAPER_APPENDIX.md) · [使用指南](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/USAGE_CN.md)

AeroWeaver 将自然语言任务转化为多无人机协同行动：通过已注册技能执行模型决策，
让每架无人机拥有独立的决策上下文与执行通道，并复用相同任务和角色的执行经验来改进技能选择，无需重新训练模型。

![AeroWeaver 多无人机自主协同与实时通信控制台](https://raw.githubusercontent.com/Admire-ljb/AeroWeaver/main/docs/images/aeroweaver-swarm-console.png)

## 核心能力

- **技能约束**：将模型决策映射到已注册的可执行技能，经过参数校验与运行时保护。
- **分布式执行**：Commander 分配角色与局部目标，各无人机智能体独立观测、通信并控制本机。
- **经验复用**：利用相同任务和角色的已观测奖励调整后续技能选择，不更新模型权重。
- **操作控制台**：中英文 Web 界面，集成手动控制、自主任务、实时遥测、传感器画面和轨迹查看。

支持 **Mock、AirSim、PX4/Gazebo** 适配器。首次体验可使用 Mock，无需外部仿真器或 API Key。

## 快速启动

需要 **Python 3.10+**、**Node.js 22.12+**（或 20.19+）和 **npm 10+**。

```bash
git clone https://github.com/Admire-ljb/AeroWeaver.git
cd AeroWeaver
python -m venv .venv
```

激活环境：Linux/macOS 使用 `source .venv/bin/activate`，Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`，然后运行：

```bash
pip install -r requirements/mock.txt
npm --prefix frontend ci
npm --prefix frontend run build
python backend/server.py
```

打开 **[http://127.0.0.1:5001](http://127.0.0.1:5001)**。全新检出的项目默认使用 Mock 和手动模式，选择一架无人机即可尝试执行技能。

若要使用自然语言任务，先[配置模型](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/USAGE_CN.md#配置-llm)，切换到**自主**模式，再输入例如：

> 让 UAV-1、UAV-2 和 UAV-3 组成间距 8 米的三角编队，然后旋转待命 20 秒。

## 文档导航

| 需要了解 | 文档 |
| --- | --- |
| 安装、运行模式、AirSim、LLM 和技能示例 | [使用指南](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/USAGE_CN.md) |
| 任务定义、奖励函数、运行记录与实验细节 | [论文附录（英文）](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/PAPER_APPENDIX.md) |
| 测试命令与仓库结构 | [开发说明](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/USAGE_CN.md#测试) |
| PX4/Gazebo 和容器部署 | [部署选项](https://github.com/Admire-ljb/AeroWeaver/blob/main/docs/USAGE_CN.md#px4gazebo-与-docker) |

## 引用

如果你在研究中使用 AeroWeaver，请引用[我们的论文](https://arxiv.org/abs/2609.18520)。

```bibtex
@misc{lou2026aeroweaver,
  title         = {{AeroWeaver}: An Embodied-Agent Harness for Weaving Aerial Skills into Distributed, Adaptive Swarm Execution},
  author        = {Jiabin Lou and Yirong Yang and Haopeng Wang and Xuxin Lv and Xinyu Liu and Diyuan Hou and Xuehong Liu and Rongye Shi and Wenjun Wu},
  year          = {2026},
  eprint        = {2609.18520},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  url           = {https://arxiv.org/abs/2609.18520}
}
```

## 安全与许可

本项目为研究软件；实际飞行前须在仿真中验证，并独立配置地理围栏、紧急停止和人工监督。采用 [MIT 许可证](https://github.com/Admire-ljb/AeroWeaver/blob/main/LICENSE)。

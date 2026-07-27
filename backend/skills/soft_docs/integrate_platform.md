# 接入新平台/设备

## 概述

当用户说"帮我接入 XXX"、"把 YYY 设备接进来"、"支持 ZZZ 协议"时，执行本策略。
目标：生成 Adapter、沙箱验证、部署上线，全程不影响已在线设备。

---

## 触发条件

- 用户明确说出设备/平台名称，并期望 AeroWeaver 能控制它
- 示例：
  - "帮我接入大疆 Mini 4 Pro"
  - "我有一个 Arduino 机械臂，怎么接进来？"
  - "我想用 ROS2 把我的地面车接进 AeroWeaver"
  - "新来了一台 Unitree Go2，帮我适配"

---

## 执行步骤

### Step 1 · 分析设备

**目标**：搞清楚设备的通信方式、能力、传感器。

需要向用户确认或主动推断：
- 通信协议：MAVLink / ROS2 / HTTP API / WebSocket / 串口 / 自定义
- 传输介质：WiFi / USB / UART / 蓝牙
- 设备类型：UAV / UGV / ARM / SENSOR / CUSTOM
- 能力列表：fly / move / grip / camera / lidar / ...
- 传感器列表：gps / imu / camera_front / ultrasonic / ...
- 已有 SDK 或文档？

**输出**：一份设备分析摘要，格式如下：
```
设备: <名称>
协议: <协议>
传输: <Transport 类型>
类型: <UAV/UGV/ARM/SENSOR/CUSTOM>
能力: [<cap1>, <cap2>, ...]
传感器: [<sensor1>, <sensor2>, ...]
参考: <SDK 文档链接或说明>
```

如果信息不足，先询问用户再继续。

---

### Step 2 · 生成 Adapter

**目标**：基于 `adapters/base_adapter.py` 生成设备专属适配器。

使用硬技能：`dynamic_skill_gen`（代码生成）

必须实现的方法：
- `connect() / disconnect()`
- `send_command(action, params)` → 映射到设备原生 API
- `get_state()` → 返回标准化状态字典
- `get_sensor_data(sensor_id)` → 返回传感器数据
- `heartbeat_loop()` → 定期上报心跳

必须遵守的规范：
- 继承 `adapters.base_adapter.BaseAdapter`
- 错误用 `core/errors.py` 中的异常（`AdapterConnectionError`、`AdapterTimeoutError`）
- 日志用 `core.logger.get_logger(__name__)`
- 传输层用 `core/transport.py` 中对应的 Transport 类

保存路径：`adapters/<设备名小写>_adapter.py`

---

### Step 3 · 沙箱测试

**目标**：在不连接真实设备的情况下验证 Adapter 逻辑正确。

使用硬技能：`sandbox_exec`（隔离执行）

沙箱测试检查清单：
- [ ] 模拟连接/断连，无异常抛出
- [ ] `send_command('ping', {})` 不崩溃
- [ ] `get_state()` 返回合法字典（含 status 字段）
- [ ] 心跳超时后状态变为 offline
- [ ] 所有异常均为 `AeroWeaverError` 子类

**安全要求**：
- 沙箱超时限制：30 秒
- 禁止网络访问真实设备（使用 MockTransport）
- 禁止写入 `core/`、`adapters/` 以外的目录
- 发现 `SandboxExecutionError` / `SandboxTimeoutError` 时，先修复再重跑，不要跳过

---

### Step 4 · 部署

**目标**：将 Adapter 注册到 AdapterFactory，使其可通过协议字段自动加载。

操作步骤：
1. 编辑 `adapters/adapter_factory.py`，在工厂映射中添加新 Adapter
2. 更新 `adapters/__init__.py`，导出新 Adapter 类
3. 如有需要，在 `config.py` 添加默认配置项（端口、波特率等）
4. 运行 `python scripts/preflight.py` 确认无启动错误

**零停机要求**：
- 不修改已有 Adapter 的接口
- 不改动 `core/device_manager.py` 的公共 API
- 新增配置项必须有默认值，避免现有部署报错

---

### Step 5 · 验证

**目标**：端到端确认设备可以通过 AeroWeaver 正常控制。

验证检查清单：
- [ ] `POST /api/device/register` 成功，拿到 token
- [ ] WebSocket 心跳稳定（连续 3 次无超时）
- [ ] `GET /api/device/<id>/state` 返回正确字段
- [ ] 发送一条简单指令（如 `ping` 或 `get_battery`）收到成功响应
- [ ] 模拟断网 → Failsafe 策略触发 → 设备安全处置
- [ ] 重连后状态恢复 online

若验证失败，返回 Step 2 重新分析，不要直接修改 core 层代码。

---

## 需要用到的模块

| 阶段 | 模块/硬技能 |
|------|------------|
| Step 1 分析 | 用户提供的文档、`robot_profile/` 中的已有档案 |
| Step 2 生成 | `adapters/base_adapter.py`、`core/transport.py`、`core/errors.py` |
| Step 3 测试 | `sandbox_exec`、`adapters/mock_adapter.py` |
| Step 4 部署 | `adapters/adapter_factory.py`、`scripts/preflight.py` |
| Step 5 验证 | `core/device_manager.py`、`core/failsafe.py`、`server.py` API |

---

## 成功判断标准

满足以下全部条件视为接入成功：

- Adapter 文件存在于 `adapters/` 目录
- `adapter_factory.py` 中已注册
- 沙箱测试全部通过（0 错误）
- 端到端验证 5 项全部通过
- `scripts/preflight.py` 无报错

## 失败判断标准

出现以下任一情况视为失败，需人工介入：

- 设备协议无文档且用户无法提供通信格式
- 沙箱测试连续 3 次 `SandboxExecutionError`
- 端到端验证中设备无响应超过 60 秒
- 需要修改 `core/` 层公共 API 才能接入（应提交 Issue 而非硬改）

---

## 注意事项

### 安全限制
- 新 Adapter 必须经过 Failsafe 策略覆盖，禁止裸连设备无保护运行
- 飞行类设备（UAV）必须配置 `FailsafePolicy`，`then` 默认为 `return_to_launch`
- 地面/机械臂类设备（UGV/ARM）`then` 默认为 `land`（停止动作）
- 禁止在 Adapter 中硬编码 IP/密码，使用 `config.py` 统一管理

### 沙箱要求
- 所有新代码必须先过沙箱，再写入 `adapters/` 目录
- 沙箱中使用 `MockTransport` 替代真实 Transport，禁止真实网络调用
- 沙箱执行时间上限 30 秒，超时即为失败

### 用户沟通
- Step 1 信息不足时，主动提问，不要凭空假设协议
- Step 5 失败时，向用户展示具体失败项，不要只说"验证失败"
- 接入完成后，给用户一段接入成功的摘要，包括设备 ID、支持的能力和下一步使用示例

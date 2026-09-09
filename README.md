# School Running Data Generator (V2.0.0beta)

> 校园跑步数据生成服务 — 胶囊形跑道轨迹拟合，生成兼容 Keep / Garmin Connect 的 FIT 文件

一个轻量级 Web 应用：填写（或一键快速）运动参数后，按所选距离在"胶囊形跑道"（两条直道 + 两端半圆）上绕圈采样，生成带真实 GPS 轨迹与自然速度波动的 `.fit` 文件，可导入 Keep、Garmin Connect 等主流运动 APP。

由 https://github.com/Jnix-memory/SchoolRunV1 升级而来，生成方法参考 SchoolRunV1 改造技术报告（FIT_GENERATOR_TECH_REPORT）中的胶囊跑道拟合 / 速度曲线 / 动力学人设方法论。

## 功能特性

- **胶囊跑道轨迹拟合** — 以 `data/standard.fit` 为母版，经 PCA + 抗抖动外包络自动拟合跑道几何（直道长、半径、朝向），每次按目标距离**绕圈重新采样**生成全新轨迹（不再复用同一条模板路线），折线实测长度与目标误差 < 0.1%
- **横向真实感** — 低频平滑横向抖动（峰值 ≤10m，主体硬阻尼）、**起点跨次一致**、终点自然偏离跑道外侧 10~30m、整体长度双重标定
- **拟真速度曲线** — 时间按速度曲线分配、总时长精确保持；**3km 档为"阶梯式下降"**：切成若干段长与降幅都不均匀的台阶，全程总降幅约 **35%**（首段 100% → 末段约 65%，快慢阶梯分明）；5km 及以上平缓递减（20% + 每公里 ±5%）
- **动力学人设** — 步频 / 功率 / 触地时间随速度逐点自然波动；跑步步频按 Garmin 惯例存"半值"（App 显示约 158 步/分），功率约 160W、触地约 247ms
- **5km / 3km 双档提交** — "提交数据"与"快速提交"均拆分为左右两个按钮（小字标注）；距离每次**纯随机**：5km 档 = 5±(-0.09~+0.4)km（约 4.91~5.40km），3km 档 = 3±(-0.09~+0.4)km（约 2.91~3.40km）
- **快速提交** — 自动编号（Cookie 记忆），按北京时间自动选择过去时段（15:00 前 → 昨天 21-23 点；15:00 后 → 今天 13:30-15:30），随机时长：3km 档 19'10"-20'53"、5km 档 26'00"-35'00"
- **下载列表带距离** — 提交记录显示实际距离，点"下载"即生成该里程的 FIT 文件
- **公告系统** — 版本化公告（当前 V2.0.0beta），新公告自动闪烁提醒
- **访客记录** — 记录每次提交与下载的 IP 与时间
- **Web 界面** — 苹果液态玻璃风格 UI，支持深色模式

## 快速开始

### 环境要求

- Python 3.7+
- pip

### 安装与运行

```bash
# 克隆项目
git clone https://github.com/Jnix-memory/SchoolRunV2.git
cd SchoolRunV2

# 安装依赖
pip install -r requirements.txt

# 启动服务
python backend/server.py
```

服务启动后访问 http://localhost:5005

### 页面说明

| 页面 | 路径 | 功能 |
|------|------|------|
| 数据填写（含快速提交） | `/form.html` | 5km/3km 双档手动填写或一键快速提交 |
| 数据下载 | `/download.html` | 查看记录（含实际距离）并下载 FIT 文件 |

两个路径指向同一单页应用（页签切换）。

## 项目结构

```
SchoolRunV2/
├── backend/
│   └── server.py              # HTTP 服务器（含 distance 入库/校验与透传生成）
├── frontend/
│   └── form.html              # 前端单页应用（双档提交 + 下载 + 公告）
├── tools/
│   ├── capsule_track.py       # 胶囊跑道拟合/绕圈采样/横向抖动/速度曲线
│   └── generate_fit.py        # FIT 生成编排（距离/速度/动力学人设）
├── data/
│   ├── activities.db           # SQLite 数据库（自动生成，含 distance 列）
│   ├── standard.fit            # GPS 母版（用于跑道拟合）
│   └── Call.txt                # 公告内容（首行为版本号）
├── requirements.txt            # Python 依赖
├── LICENSE                     # 开源协议
├── .gitignore                  # Git 忽略规则
├── README.md                   # 项目说明
└── 技术报告.md                  # 详细技术文档
```

## API 接口

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/activities` | 获取所有活动记录（含距离） | — |
| GET | `/api/announcement` | 获取公告内容和版本号 | — |
| POST | `/api/submit` | 提交运动数据（记录入库） | `{ user_id, date, start_time, duration, distance? }` |
| POST | `/api/generate` | 生成并下载 FIT 文件 | `{ user_id, date, start_time, duration, distance? }` |

### 请求参数格式

| 参数 | 格式 | 示例 | 说明 |
|------|------|------|------|
| `user_id` | 3位数字 | `021` | 个人编号 |
| `date` | YYYYMMDD | `20260414` | 运动日期 |
| `start_time` | HHMM | `1315` | 开始时间（24小时制） |
| `duration` | MMSS | `2103` | 运动时长（21分03秒） |
| `distance` | 公里（可选） | `5.17` | 运动距离；须 `0<km<=100`，缺省/空时按 3.00~3.29km 随机 |

### 响应示例

**POST /api/submit**
```json
{ "success": true, "id": 1, "distance": 5.17 }
```

**GET /api/activities**
```json
[
  {
    "id": 1,
    "user_id": "021",
    "date": "20260414",
    "start_time": "1315",
    "duration": "2103",
    "distance": 5.17,
    "visitor_ip": "192.168.1.100",
    "created_at": "2026-06-12 13:15:00"
  }
]
```

## 命令行生成

```bash
python tools/generate_fit.py <个人编号> <日期> <开始时间> <运动总时长> [距离公里]
# 示例：python tools/generate_fit.py 021 20260414 1315 2103 5.2
# 不带距离时随机 3.00~3.29km（三公里为主）
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python `http.server` + `socketserver.ThreadingMixIn` |
| 数据库 | SQLite（标准库内置，activities 含 distance 列） |
| 轨迹几何 | 自研 `capsule_track.py`（PCA + 解析几何 + 弧长采样） |
| FIT 生成 | [fit-tool](https://pypi.org/project/fit-tool/) |
| 前端 | 原生 HTML5 + CSS3 + JavaScript |
| UI 风格 | Glassmorphism（液态玻璃） |

## 依赖

```
fit-tool>=0.9.0
```

仅一个第三方依赖，其余全部使用 Python 标准库。

## 许可证

本项目采用 [MIT License](LICENSE) 开源。

## 致谢

- GPS 轨迹母版基于真实校园跑步路线（`data/standard.fit`）
- 生成方法移植自 SchoolRunV1 改造技术报告（胶囊跑道/速度曲线/动力学人设/长度标定经验）
- 运动参数参考《国民体质测定标准》（国家体育总局）
- UI 设计参考 Apple Human Interface Guidelines
- 项目使用 mimocode 和 Xiaomi mimo-V2.5-pro 完成改进

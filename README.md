# School Running Data Generator

> 校园跑步数据生成服务 — 基于 GPS 轨迹模板生成 FIT 格式运动文件

一个轻量级 Web 应用，通过浏览器填写运动参数后自动生成包含真实 GPS 轨迹的 `.fit` 文件，兼容 Keep、Garmin Connect 等主流运动 APP 导入。

由https://github.com/Jnix-memory/SchoolRunV1用AI改进而来。

## 功能特性

- **GPS 轨迹生成** — 基于真实跑步轨迹模板，生成包含完整 GPS 数据的 FIT 文件
- **运动参数自定义** — 支持设置日期、开始时间、运动时长
- **快速提交** — 一键生成，自动根据当前时间选择合理的运动时段
- **智能数据** — 步频、配速、功率等指标参考《国民体质测定标准》设定
- **Web 管理界面** — 苹果液态玻璃风格 UI，支持深色模式
- **公告系统** — 版本化公告，新公告自动提醒
- **访客记录** — 记录每次提交和下载的 IP 与时间

## 快速开始

### 环境要求

- Python 3.7+
- pip

### 安装与运行

```bash
# 克隆项目
git clone <repository-url>
cd V2O

# 安装依赖
pip install -r requirements.txt

# 启动服务
python3 backend/server.py
```

服务启动后访问 http://localhost:5005

### 页面说明

| 页面 | 路径 | 功能 |
|------|------|------|
| 数据填写 | `/form.html` | 填写运动参数或快速提交 |
| 数据下载 | `/download.html` | 查看记录并下载 FIT 文件 |

## 项目结构

```
V2O/
├── backend/
│   └── server.py              # HTTP 服务器主程序
├── frontend/
│   └── form.html              # 前端单页应用（表单 + 下载 + 公告）
├── tools/
│   └── generate_fit.py        # FIT 文件生成器
├── data/
│   ├── activities.db           # SQLite 数据库（自动生成）
│   ├── standard.fit            # GPS 轨迹模板文件
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
| GET | `/api/activities` | 获取所有活动记录 | — |
| GET | `/api/announcement` | 获取公告内容和版本号 | — |
| POST | `/api/submit` | 提交运动数据 | `{ user_id, date, start_time, duration }` |
| POST | `/api/generate` | 生成并下载 FIT 文件 | `{ user_id, date, start_time, duration }` |

### 请求参数格式

| 参数 | 格式 | 示例 | 说明 |
|------|------|------|------|
| `user_id` | 3位数字 | `021` | 个人编号 |
| `date` | YYYYMMDD | `20260414` | 运动日期 |
| `start_time` | HHMM | `1315` | 开始时间（24小时制） |
| `duration` | MMSS | `2103` | 运动时长（21分03秒） |

### 响应示例

**POST /api/submit**
```json
{ "success": true, "id": 1 }
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
    "visitor_ip": "192.168.1.100",
    "created_at": "2026-06-12 13:15:00"
  }
]
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python `http.server` + `socketserver.ThreadingMixIn` |
| 数据库 | SQLite（标准库内置） |
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

- GPS 轨迹数据基于真实校园跑步路线
- 运动参数参考《国民体质测定标准》（国家体育总局）
- UI 设计参考 Apple Human Interface Guidelines
- 项目使用mimocode和Xiaomi mimo-V2.5-pro完成改进
---
kind: dependency_management
name: Python/Node.js 双栈依赖管理策略
category: dependency_management
scope:
    - '**'
source_files:
    - backend/requirements.txt
    - backend/start.sh
    - frontend/package.json
    - frontend/package-lock.json
    - .gitignore
---

## 系统概览
PolyStudio 采用前后端分离架构，分别使用 Python 和 Node.js 两套独立的依赖管理体系：
- **后端**：基于 `requirements.txt` + Conda 虚拟环境（conda activate polystudio）
- **前端**：基于 `package.json` + `package-lock.json`（npm v3 lockfile）

## 关键文件与工具
- `backend/requirements.txt` — Python 依赖声明，固定版本为主（如 fastapi==0.104.1、langchain==1.0.0），部分包使用范围约束（pydantic>=2.7.4,<3.0.0）
- `backend/.venv/` — 本地虚拟环境目录（被 .gitignore 忽略）
- `backend/start.sh` — 通过 conda 激活 `polystudio` 环境并启动 uvicorn
- `frontend/package.json` — 前端依赖声明，使用语义化版本范围（^18.2.0 等）
- `frontend/package-lock.json` — npm 锁定文件，记录完整依赖树及 sha512 integrity
- `.gitignore` — 统一忽略 `venv/`、`.venv`、`node_modules/`

## 架构约定
- **后端**：未使用 pipenv/poetry，仅用 requirements.txt 做依赖清单；通过 start.sh 脚本强制使用 conda 环境，避免全局污染
- **前端**：使用 Vite + TypeScript 构建链，依赖以 ^ 范围声明，lockfile 由 npm 自动生成
- **无私有源配置**：未发现 pypi.org 镜像或 npm registry 自定义配置，默认走公共源
- **无 vendoring**：未将第三方库直接提交到仓库

## 开发者规范
1. 新增 Python 依赖时同步更新 `backend/requirements.txt`，优先固定版本或使用 `<major` 上限保护
2. 新增前端依赖后需提交 `package-lock.json` 变更，确保团队安装一致性
3. 运行后端前必须执行 `start.sh` 以激活 conda 环境，勿直接使用系统 Python
4. 不要提交 `node_modules/`、`.venv/` 或 `venv/` 到版本库
---
kind: frontend_style
name: 原生 CSS + data-theme 双主题风格系统
category: frontend_style
scope:
    - '**'
source_files:
    - frontend/src/index.css
    - frontend/src/App.css
    - frontend/src/components/HomePage.css
    - frontend/src/components/SettingsPage.css
    - frontend/src/components/ChatInterface.css
    - frontend/src/components/Model3DViewer.css
    - frontend/package.json
---

## 1. 使用的系统与工具
- **样式方案**：纯原生 CSS（无 Sass/Less、无 Tailwind、无 CSS-in-JS），每个 React 组件配套一个同名 `.css` 文件，采用 BEM 风格命名（如 `home__card`、`settings-page__tab--active`）。
- **构建与打包**：Vite + `@vitejs/plugin-react`，CSS 随组件按需导入，由 Vite 自动处理。
- **图标与 UI 依赖**：使用 `lucide-react` 作为图标库；3D 相关使用 `three` + `@react-three/fiber` + `@react-three/drei`；画板使用 `@excalidraw/excalidraw`；Markdown 渲染使用 `react-markdown`。这些是功能依赖，不充当 UI 组件库。

## 2. 关键文件与位置
- 全局基础样式与主题变量入口：
  - `frontend/src/index.css` — 定义 `:root` 下的 `--app-bg` / `--app-fg`，以及 `[data-theme="dark"]` / `[data-theme="light"]` 两套根级变量。
  - `frontend/src/App.css` — 应用容器引用 `var(--app-bg)` / `var(--app-fg)`。
- 页面/组件样式（均位于 `frontend/src/components/`）：
  - `HomePage.css`、`SettingsPage.css`、`ChatInterface.css`、`Model3DViewer.css`
- 第三方库主题覆盖集中在 `ChatInterface.css` 中，通过选择器前缀 `.excalidraw-host` 对 Excalidraw 的 DOM 进行深度覆盖。

## 3. 架构与设计约定
### 3.1 主题切换机制（data-theme）
- 在 `<html>` 或 `<body>` 上设置 `data-theme="dark" | "light"` 即可切换全局主题。
- 所有颜色、阴影、边框等视觉值都通过 CSS 自定义属性（`--bg-color`、`--text-primary`、`--shadow-lg` 等）暴露，并在 `[data-theme="light"]` 块中提供浅色覆盖。
- 默认深色主题以硬编码色值书写，浅色主题仅写差异覆盖，避免重复定义。

### 3.2 设计令牌（Design Tokens）
- 全局通用令牌集中在 `index.css`：`--app-bg`、`--app-fg`。
- 组件级令牌集中在各自 CSS 顶部，例如 `ChatInterface.css` 中的 `--primary-color`、`--radius-*`、`--bg-color`、`--panel-bg`、`--text-primary`、`--border-color`、`--shadow-sm/md/lg`。
- 未引入统一的设计 token 文件，各组件自行维护自己的变量集合。

### 3.3 视觉风格关键词
- **深色优先**：默认背景 `#070a12`，文字 `#e5e7eb`，强调色 `#2563eb`（蓝紫渐变）。
- **玻璃拟态**：大量使用 `backdrop-filter: blur()` + 半透明背景（`rgba(..., 0.92)`）+ 多层 `box-shadow` 营造悬浮面板效果（聊天面板、Excalidraw 右键菜单等）。
- **彩色光晕背景**：通过多层 `radial-gradient` + `filter: blur(24px)` 在页面/面板背后制造柔和的蓝/紫/绿/红光晕。
- **圆角体系**：`--radius-sm: 0.5rem`、`--radius-md: 0.75rem`、`--radius-lg: 1rem`，按钮、卡片、输入框统一使用。
- **动画与微交互**：`fadeIn`、`slideDown`、`breathing`、`pulse-dot`、`skill-appear` 等 keyframes，配合 `transition: all 0.2s ease` 实现轻量动效。

### 3.4 响应式策略
- 基于 `@media (max-width: ...)` 的断点适配，主要覆盖 `1020px` 和 `640px` 两个断点，调整网格列数、搜索框宽度、内边距等。
- 布局层面大量使用 Flexbox 与 CSS Grid（如 `home__grid` 的 `repeat(3, minmax(0, 1fr))`）。

### 3.5 第三方库集成方式
- Excalidraw：通过 `.excalidraw-host` 选择器链直接覆盖其内部 DOM 结构，隐藏侧边栏、菜单等，并为其右键菜单注入项目风格的容器样式。
- Three.js / R3F：`Model3DViewer.css` 仅做容器尺寸与边框适配，材质/光照逻辑在 TSX 中完成。

## 4. 开发者应遵循的规则
1. **新增组件必须附带同名 `.css` 文件**，并在组件中 `import './xxx.css'` 引入，保持样式与组件一一对应。
2. **主题扩展**：新增颜色/阴影/圆角时，先在组件 CSS 顶部声明为 CSS 变量（`--xxx`），再在 `[data-theme="light"]` 块中补充浅色覆盖，不要硬编码颜色。
3. **命名规范**：沿用 BEM 风格 `block__element--modifier`（如 `home__btn--primary`、`settings-page__tab--active`），避免全局污染。
4. **禁止使用 Tailwind 类名或 CSS-in-JS**，本项目未配置任何原子化 CSS 或 JS 内联样式框架。
5. **覆盖第三方库样式时**，使用足够具体的选择器前缀（如 `.excalidraw-host`），并通过 `!important` 仅在必要时使用，尽量只改“观感”而非布局结构。
6. **响应式**：新增布局变化时使用 `@media` 断点，优先保证桌面端体验，再逐步适配平板/手机。
7. **动画与过渡**：统一使用 `0.2s ease` 左右的 transition，复杂动效用 keyframes 集中定义在文件顶部，避免散落在各处。
8. **字体与排版**：正文使用系统字体栈 `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, ...`，代码使用 `source-code-pro/Menlo/Monaco` 等 monospace 字体，不要在组件内重复声明 font-family。

## 5. 总结
该项目采用「原生 CSS + CSS 变量 + data-theme」的轻量主题方案，配合 BEM 命名的组件级样式文件，形成一套深色优先、带玻璃拟态与彩色光晕的现代化 UI 风格。没有引入外部 UI 组件库或原子化 CSS 框架，所有视觉一致性依靠统一的变量命名与覆盖约定来维持。
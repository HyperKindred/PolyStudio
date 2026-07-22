import { useEffect, useState } from 'react'
import ChatInterface from './components/ChatInterface'
import HomePage from './components/HomePage'
import SettingsPage from './components/SettingsPage'
import './App.css'

/**
 * 应用支持的两种主题。联合类型可以阻止其他任意字符串被传入主题相关 Props。
 */
type ThemeMode = 'dark' | 'light'

/**
 * 从当前 URL 读取画布 ID。
 *
 * 例如访问 /?canvasId=canvas-123 时返回 "canvas-123"；没有该参数时返回空字符串。
 * 本项目没有引入 React Router，而是用 URL 查询参数决定当前显示哪个页面。
 */
function getCanvasIdFromUrl() {
  try {
    // window.location.href 是浏览器当前页面的完整地址，URL 类便于可靠地解析查询参数。
    const url = new URL(window.location.href)
    return url.searchParams.get('canvasId') || ''
  } catch {
    // URL 解析失败时回到“未选择画布”状态，避免应用启动失败。
    return ''
  }
}

/** 读取显式页面参数，例如 /?page=settings。 */
function getPageFromUrl() {
  try {
    const url = new URL(window.location.href)
    return url.searchParams.get('page') || ''
  } catch {
    return ''
  }
}

/**
 * 读取用户上次选择的主题。
 * localStorage 会跨刷新和浏览器重启保存；没有合法记录时使用深色主题。
 */
function readInitialTheme(): ThemeMode {
  try {
    const stored = localStorage.getItem('polystudio:theme')
    // localStorage 返回普通 string，需要显式判断后才能收窄为 ThemeMode。
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    // 某些隐私模式或受限环境可能禁用 localStorage，此时继续使用默认主题。
  }
  return 'dark'
}

function App() {
  // 函数形式是 useState 的惰性初始化：这些读取操作只在组件首次创建时执行一次。
  const [canvasId, setCanvasId] = useState<string>(() => getCanvasIdFromUrl())
  const [page, setPage] = useState<string>(() => getPageFromUrl())
  const [theme, setTheme] = useState<ThemeMode>(() => readInitialTheme())

  // 监听浏览器前进/后退，URL 历史发生变化后重新计算当前页面状态。
  useEffect(() => {
    const onPop = () => {
      setCanvasId(getCanvasIdFromUrl())
      setPage(getPageFromUrl())
    }
    window.addEventListener('popstate', onPop)

    // 组件卸载时移除监听器，避免重复注册或继续修改已经卸载的组件。
    return () => window.removeEventListener('popstate', onPop)
    // 空依赖数组表示只在挂载时注册、卸载时清理，而不是每次渲染都执行。
  }, [])

  // theme 每次变化后，同时更新页面样式入口和本地持久化记录。
  useEffect(() => {
    try {
      // 生成 <html data-theme="dark|light">，CSS 可通过 [data-theme] 选择器切换变量。
      document.documentElement.dataset.theme = theme
      localStorage.setItem('polystudio:theme', theme)
    } catch {
      // 即使 DOM 或存储访问失败，也不应阻止 React 页面继续渲染。
    }
  }, [theme])

  // 使用函数式更新读取最新状态，避免依赖当前渲染闭包里的旧 theme 值。
  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))

  return (
    <div className="app">
      {/*
        页面选择优先级：
        1. page=settings 时显示设置页；
        2. 否则，有 canvasId 时显示对应的聊天/画布编辑器；
        3. 两者都没有时显示首页。
      */}
      {page === 'settings' ? (
        <SettingsPage theme={theme} onToggleTheme={toggleTheme} />
      ) : canvasId ? (
        <ChatInterface
          // 编辑器用这个 ID 加载对应项目，并建立该画布的 WebSocket 订阅。
          initialCanvasId={canvasId}
          theme={theme}
          onToggleTheme={toggleTheme}
          // 除了简单切换外，编辑器还可以直接指定主题，因此向下传递 setTheme。
          onSetTheme={setTheme}
        />
      ) : (
        <HomePage theme={theme} onToggleTheme={toggleTheme} />
      )}
    </div>
  )
}

// main.tsx 导入该默认导出，并把 App 渲染到页面根节点。
export default App








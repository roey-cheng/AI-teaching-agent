import { useEffect, useState } from 'react'

type ConnectionState =
  | { kind: 'loading' }
  | { kind: 'success' }
  | { kind: 'error'; message: string }

export default function App() {
  // connection 保存当前连接状态；setConnection 负责更新状态、记录检查结果，
  // 并让 React 根据新状态更新页面。这里仅保存在页面内存中，不会写入数据库。
  const [connection, setConnection] = useState<ConnectionState>({ kind: 'loading' })
  const [checkNumber, setCheckNumber] = useState(0)

  // 打开页面或点击重新检查时，发送一次真实的后端请求。
  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    let timedOut = false
    const timeout = window.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, 5000)

    async function checkBackend() {
      try {
        // fetch 负责“问后端”：向 /health 发送请求，等待后端回答。
        // 相对路径会先到 Vite，再由开发代理转发到 FastAPI。
        const response = await fetch('/health', {
          signal: controller.signal,
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        })
        if (!response.ok) throw new Error(`后端返回 HTTP ${response.status}，请检查后端终端。`)

        const body: unknown = await response.json()
        if (
          typeof body !== 'object' || body === null ||
          !('status' in body) || body.status !== 'ok'
        ) {
          throw new Error('后端响应格式不正确，预期收到 {"status":"ok"}。')
        }

        if (!disposed) setConnection({ kind: 'success' })
      } catch (error) {
        if (disposed) return
        const message = timedOut
          ? '等待超过 5 秒，请确认后端已启动，然后重新检查。'
          : error instanceof Error
            ? error.message
            : '无法连接后端，请确认后端已启动。'
        setConnection({ kind: 'error', message })
      } finally {
        window.clearTimeout(timeout)
      }
    }

    void checkBackend()
    return () => {
      disposed = true
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [checkNumber])

  function checkAgain() {
    setConnection({ kind: 'loading' })
    setCheckNumber((previous) => previous + 1)
  }

  // return 里面描述页面显示什么：根据 connection 的状态，
  // 分别显示“正在连接后端…”、“后端连接成功”或“后端连接失败”。
  return (
    <main className="page">
      <header>
        <p className="eyebrow">AI TEACHING ASSISTANT</p>
        <h1>先让前端与后端说上话。</h1>
        <p className="intro">这是项目的第一张测试页面，用一次真实请求检查连接。</p>
      </header>

      <section className="card" aria-labelledby="connection-title">
        <p className="label" id="connection-title">连接检查 · GET /health</p>
        <div role="status" aria-live="polite" aria-atomic="true">
          <h2 className={`status ${connection.kind}`}>
            {connection.kind === 'loading' && '正在连接后端…'}
            {connection.kind === 'success' && '后端连接成功'}
            {connection.kind === 'error' && '后端连接失败'}
          </h2>
          <p className="detail">
            {connection.kind === 'loading' && '网页正在等待 FastAPI 回答，最多等待 5 秒。'}
            {connection.kind === 'success' && <>后端已返回 <code>{'{"status":"ok"}'}</code>，前后端请求链路已接通。</>}
            {connection.kind === 'error' && connection.message}
          </p>
        </div>
        <button onClick={checkAgain} disabled={connection.kind === 'loading'}>
          {connection.kind === 'loading' ? '检查中…' : '重新检查连接'}
        </button>
      </section>

      <section className="explanation" aria-labelledby="request-title">
        <h2 id="request-title">这次请求经过哪里？</h2>
        <ol>
          <li><strong>浏览器里的 React 页面</strong><span>发出 GET /health 请求。</span></li>
          <li><strong>Vite 开发服务 · 5173</strong><span>把请求转发给本机后端。</span></li>
          <li><strong>FastAPI 后端 · 8000</strong><span>返回状态，页面据此显示连接结果。</span></li>
        </ol>
      </section>
      <p className="note">当前仅检查网页与后端的连接，不代表数据库或大模型已连接。结果是本次检查的快照，不会自动持续监测。</p>
    </main>
  )
}

# 前端

第一版网页已经接通全部 11 个后端业务接口。页面使用 React + TypeScript + Vite，整体采用简洁、以对话为中心的布局，支持桌面和手机宽度。登录页为单列居中布局，不再展示左侧宣传海报。

## 用户现在能做什么

- 注册、登录、退出；登录凭据由后端放在 HttpOnly Cookie 中，前端不保存密码或 Token。
- 新建、切换和重命名对话；左侧栏读取当前用户的真实会话。
- 读取完整历史，发送多行文字并逐步显示 SSE 回答。
- 分开展示 Agent 工作进度、模型 API 返回的思考文字和最终回答。
- 展示 Markdown、列表、表格和代码块。
- 查看失败摘要，并重试后端允许重试的最后一个失败问题。
- 打开 Profile Memory 抽屉，查看 Agent 为当前账号保存的长期记忆。
- 在中文和英文界面之间切换；选择会保存在当前浏览器中。
- 在窄屏上使用抽屉式会话栏。

## 代码是怎样分工的

| 文件 | 主要职责 |
|---|---|
| `src/App.tsx` | 页面总指挥：恢复登录、加载会话与历史、协调发送和重试 |
| `src/api/client.ts` | 调用 11 个 HTTP 接口，把后端错误转成前端可处理的对象 |
| `src/api/sse.ts` | 将网络分片重新拼接成完整 SSE 事件；网络分片不等于一个事件 |
| `src/types.ts` | 后端请求、响应和 SSE 事件在 TypeScript 中的形状 |
| `src/components/AuthScreen.tsx` | 登录和注册页面；注册成功后再调用一次登录接口 |
| `src/components/Sidebar.tsx` | 会话列表、新建、切换、重命名、记忆入口和退出 |
| `src/components/ChatPanel.tsx` | 历史消息、实时进度/思考/回答、失败重试和输入框 |
| `src/components/MemoryDrawer.tsx` | 读取并展示当前用户的 Profile Memory |
| `src/i18n.tsx` | 中英文案、当前语言状态和浏览器持久化 |
| `src/components/Markdown.tsx` | 安全地把模型的 Markdown 内容渲染成页面结构 |
| `src/styles.css` | 桌面/手机布局、颜色、排版和动画 |

React 负责根据状态更新页面，TypeScript 在开发阶段检查数据形状，Vite 负责本地开发服务和打包。`react-markdown` 与 `remark-gfm` 只负责展示回答；它们不调用模型。Vitest 用来测试 SSE 分片解析。

## 本地启动

需要两个终端保持运行。

终端一启动后端：

```bash
cd /Users/roey/projects/AI-teaching-agent/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

终端二启动前端：

```bash
cd /Users/roey/projects/AI-teaching-agent/frontend
npm ci
npm run dev
```

然后访问 <http://localhost:5173/>。Vite 会把浏览器发往 `/api/v1/...` 和 `/health` 的请求转发到本机 8000 端口。这个代理只服务于本地开发，正式部署还需在网站服务器配置同站点转发。

## 一次聊天怎样到达页面

```text
用户按 Enter
  → 前端生成 client_message_key，并 POST 问题
  → 后端验证 Cookie、保存 USER 消息、运行 Deep Agents
  → SSE 依次送来进度、思考片段、回答片段和完成事件
  → 前端逐步更新当前气泡
  → 完成后重新查询历史与会话列表，以数据库结果为准
```

前端使用 `fetch()` 读取 POST 返回的流，不使用只能方便发送 GET 的原生 `EventSource`。若网络结束前没有收到成功或失败终态，页面不会猜测结果，而是重新查询历史。相同 `client_message_key` 的网络重发由后端识别，不会重复生成。

## 检查和打包

```bash
cd /Users/roey/projects/AI-teaching-agent/frontend
npm run test
npm run typecheck
npm run build
npm audit
```

`npm run build` 会依次执行自动测试、类型检查和生产打包。生成的 `dist/` 是构建产物，不提交 Git，也不要直接修改。

## 配置与秘密

前端代码和 `VITE_*` 配置最终会被浏览器用户看到，因此不能放数据库密码、模型 API Key、LangSmith Key 或任何服务端秘密。当前前端只使用同站点相对路径，不需要在 `.env` 中保存后端密钥。

思考文字只展示模型 API 明确返回的内容，不伪造也不声称是模型完整的内部思维。思考与进度不写入聊天历史；最终回答和长期记忆由后端持久化。

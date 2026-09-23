# 前端

已经建立 React + TypeScript + Vite 的最小前端。当前只有连接检查页面，还没有登录、聊天或 Profile Memory 页面。本次是在已有目录中逐个建立配置和入口文件，没有用初始化命令覆盖已有文件。

## 先认识工具

| 名称 | 在这个项目里做什么 |
|---|---|
| React | 帮我们组织页面，并根据连接状态更新文字和按钮 |
| TypeScript | 编写前端逻辑，开发时检查类型错误；检查通过不等于运行时一定正确 |
| React DOM | 将 React 描述的页面显示到浏览器中 |
| Vite | 运行开发服务、处理浏览器需要的代码，并提供生产打包功能 |
| Node.js | 在电脑上运行 Vite 等开发工具，不是本项目的 Python 后端 |
| npm | 安装依赖，并执行 package.json 中定义的命令 |

本机验证环境：Node.js 26.8.2、npm 11.19.1。初始依赖包括 React 19.3.0、TypeScript 7.0.2、Vite 8.3.0；完整的具体版本以 `package-lock.json` 为准。项目使用的 Vite 要求 Node.js 20.19+（20 系列）或 22.12+。

## 文件是做什么的？

| 文件或目录 | 作用 |
|---|---|
| `package.json` | 前端依赖清单和命令表，类似后端的依赖声明 |
| `package-lock.json` | 记录具体依赖版本，作用类似后端的 uv.lock；需要提交 Git |
| `node_modules/` | npm 下载的依赖，自动生成，不提交 Git |
| `index.html` | 浏览器加载的 HTML 入口，提供 root 元素并引入 main.tsx |
| `src/main.tsx` | 启动 React，把 App 页面放进 root 元素 |
| `src/App.tsx` | 连接检查页面、请求后端和显示检查结果的逻辑 |
| `src/styles.css` | 页面的颜色、字号、间距等样式 |
| `tsconfig.json` | TypeScript 的检查规则 |
| `vite.config.ts` | Vite 的端口和请求转发配置 |
| `dist/` | 打包生成的网页文件，不提交 Git，也不要直接修改 |

`.tsx` 是可以同时编写 TypeScript 逻辑和 JSX 页面描述的文件。浏览器不是直接执行我们写的 TypeScript，开发时由 Vite 处理成浏览器能够运行的代码。

## 怎样启动：两个终端，分别保持运行

### 终端一：启动后端

```bash
cd /Users/roey/projects/AI-teaching-agent/backend
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

第一次同步或更换依赖后需要 `uv sync --locked`；平时可直接运行最后一条命令。`app.main:app` 表示使用 `app/main.py` 里的 `app` 对象。`--reload` 表示修改 Python 文件后自动重启开发服务。

### 终端二：启动前端

```bash
cd /Users/roey/projects/AI-teaching-agent/frontend
npm ci
npm run dev
```

- 本次第一次安装使用 `npm install`，它生成了锁定文件。
- 后续下载项目或需要重建依赖时，使用 `npm ci` 按锁定文件安装；它会重建 node_modules，不需要每次启动都执行。
- `npm run dev` 执行 package.json 中名为 dev 的命令，也就是启动 Vite。
- 看到开发地址后，打开 <http://localhost:5173/>。`localhost` 指当前电脑，不是已发布的公网地址。
- 前后端必须同时运行。终端一直显示日志、没有返回输入提示符，是服务正在运行，不是卡住了。
- 停止某个服务时，在运行它的终端按 `Ctrl+C`。
- 若提示端口被占用，先确认是不是已经启动过。配置了 strictPort，Vite 不会悄悄改用另一个端口。

以上是本机路径，换电脑时需要改成自己的项目位置。服务只监听本机，不开放给局域网或公网。

## 页面怎样知道“后端连接成功”？

```text
浏览器打开 localhost:5173，加载 React 页面
    ↓ 页面发送 GET /health
Vite 开发服务（5173）
    ↓ 按代理配置转发，路径仍是 /health
FastAPI 后端（127.0.0.1:8000）
    ↓ 返回 HTTP 200 和 {"status":"ok"}
Vite 把响应传回浏览器
    ↓ 页面检查响应状态和内容
显示“后端连接成功”
```

“代理”在这里就是帮忙转发请求。浏览器只向前端同一地址发请求，Vite 再访问另一个端口的后端，因此本地这条链路不需要把后端设置为允许所有跨域来源。

页面打开时自动检查，也可点击“重新检查连接”。等待期间按钮禁用；超过 5 秒、HTTP 错误或响应格式错误会显示失败，不会假装成功。检查结果只是当时的快照，不会持续自动监控；后端停止后需要重新检查才能更新结果。

开发时启用了 React StrictMode，可能看到额外的检查请求，这是开发检查和清理机制，不是业务消息重复发送。

`/health` 只检查后端 HTTP 应用能否响应，不检查数据库和模型，也不算原设计的 11 个业务接口之一。

## 自己验证成功与失败

1. 同时启动前后端，打开页面，确认出现“后端连接成功”。
2. 在后端终端按 Ctrl+C 停止后端，前端保持运行。
3. 点击“重新检查连接”，应显示失败，而不是停留在成功。
4. 再次启动后端，点击重新检查，应恢复成功。

也可以在终端检查代理：

```bash
curl http://localhost:5173/health
```

后端运行时应返回 `{"status":"ok"}`。这是请求链路检查，不代替浏览器里的页面检查。

## 类型检查与打包

在 frontend 目录执行：

```bash
npm run typecheck
npm run build
```

第一条检查 TypeScript；第二条先检查，再将页面打包进 dist。打包不是启动服务，也不是自动部署到互联网。当前代理仅用于 Vite 开发服务；正式部署还需要配置网站服务的请求转发。

## 配置与秘密

`.env.example` 中的 `VITE_API_BASE_URL=/api/v1` 预留给后续业务接口代码；当前健康检查固定请求 `/health`，尚未读取这个变量，不需要创建 `.env` 就能运行页面。开发代理已经配置 `/health` 和 `/api/v1`，但后端暂时只实现 `/health`，业务路径仍会返回 404。

`VITE_` 开头的配置可以进入浏览器代码，只能放允许公开的信息，不能放数据库密码、模型 API Key 或其他秘密。

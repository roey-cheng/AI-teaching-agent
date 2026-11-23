# 临时公网演示

本方案通过 localhost.run 免费 SSH 隧道提供 HTTPS 地址。程序和 MySQL 仍在本机运行；电脑睡眠、断网、SSH 断开或服务退出时，网址会停止工作。免费网址可能变化，不适合作为长期固定地址。

公网请求依次经过：浏览器 → localhost.run 的 HTTPS 入口 → SSH 隧道 → 本机 `127.0.0.1:8080` → FastAPI。FastAPI 同时提供 `frontend/dist` 打包网页和原有 API，SSE 不经过 Vite 或额外缓冲代理。

模型调用仍消耗原有 DeepSeek 余额。拿到链接的人可以注册和聊天，所以只分享给演示对象。使用的仍是本机数据库：现有账号可直接登录，各用户的数据继续由原有业务权限隔离。隧道服务负责终止 HTTPS，流量会经过该第三方。

## 启动

先确保 Docker 中的 MySQL 正在运行，且停止原来的后端或终端聊天程序。同一个数据库只能有一个聊天后端进程。

构建网页：

```bash
cd /Users/roey/projects/AI-teaching-agent/frontend
npm run build
```

终端一建立隧道并保持运行：

```bash
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -o IdentityAgent=none -o IdentitiesOnly=yes \
  -R 80:127.0.0.1:8080 nokey@localhost.run
```

复制输出中的 `https://…lhr.life` 地址。终端二启动演示服务，下面的示例域名必须替换成刚拿到的真实地址，末尾不加 `/`：

```bash
cd /Users/roey/projects/AI-teaching-agent/backend
uv run python -m app.demo https://YOUR-TUNNEL.lhr.life
```

演示入口固定使用 Secure / HttpOnly 登录 Cookie，只允许这个 HTTPS 来源写入，静态目录仅为 `frontend/dist`。原有 `.env` 不需要修改。由于来源和 Cookie 配置按公网 HTTPS 设置，演示期间请从公网地址登录，不从旧 `localhost:5173` 页面操作。

## 停止和恢复本地开发

在隧道终端按 Ctrl+C 关闭公网访问，再在演示服务终端按 Ctrl+C 正常退出后端。停止不删除数据库或聊天数据。

恢复原来的本地服务：

```bash
cd /Users/roey/projects/AI-teaching-agent/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

前端仍按 `frontend/README.md` 使用 `npm run dev`。隧道重连后若域名变了，需要用新地址重启 `app.demo`。

## 验证范围

已验证免费 HTTPS 隧道能把间隔 2 秒的两个 SSE 测试事件逐段送达；这项测试不调用模型。另验证公网网页与资源、注册、Secure Cookie 登录、当前用户、空会话与记忆查询、退出登录，以及错误来源拒绝。自动测试覆盖静态文件边界和 HTTPS 配置。临时生成的验收账号与登录记录在测试后清理，不影响已有账号。

完整真实模型聊天可用已有账号手动验收，会消耗模型额度。

参考：[localhost.run 文档](https://localhost.run/docs/)、[HTTP 隧道](https://localhost.run/docs/http-tunnels/)、[免费域名限制](https://localhost.run/docs/faq/)。

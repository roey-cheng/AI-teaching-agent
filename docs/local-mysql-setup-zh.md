# 本地 MySQL：从启动配置到连接验证

## 当前状态

本机已填写密码，并通过 compose.yaml 下载、初始化和启动 MySQL。2026-09-21 实际验证结果：

- MySQL 版本：8.4.11（使用 mysql:8.4 镜像）。
- 容器：ai-teaching-assistant-db-1；只映射本机 127.0.0.1:3306。
- 数据库：ai_teaching_assistant；账号：ai_teaching_app。
- 数据卷：ai-teaching-assistant_mysql_data，挂载到 /var/lib/mysql。
- 容器内和 Mac 本机的客户端均成功登录，SELECT 1 返回 1。
- 账号权限已检查：仅项目库内的全部权限，没有全局管理权限。
- 查询时项目数据库的业务表数量为 0。之后已安装 PyMySQL 并准备 backend/.env、配置读取和独立连接检查代码；真实 Python 连接测试尚未执行。

这些是本次验证时的结果，不表示服务永远保持运行。下面保留首次准备、启动和日常使用步骤，换电脑时需重新配置。

本次只准备数据库服务，不创建用户表、会话表等业务表。之后仍按设计表 → Schema → Migration 的顺序实施。

## 1. 三个文件分别负责什么

| 文件 | 用途 | 是否提交 Git |
|---|---|---|
| compose.yaml | 告诉 Docker 使用哪个 MySQL、开放哪个端口、数据放哪里 | 是 |
| 根目录 .env.example | 展示需要填写哪些密码，不含真实值 | 是 |
| 根目录 .env | 保存自己电脑上真实的数据库密码 | 否 |

本机 .env 已填写两个密码，.env.example 仍保留空值。不要用模板覆盖已经填写的本地 .env，也不要公开密码。

根目录 .env 供 Docker Compose 使用，不会自动被 Python 后端读取。backend/.env 是给后端程序使用的另一个配置文件，目前已创建，并将应用账号密码复制到 DB_PASSWORD；没有复制管理员密码。二者不自动同步。

## 2. 先在本地填写密码

用编辑器打开根目录 .env，填写等号后面的值：

- MYSQL_ROOT_PASSWORD：数据库管理员密码；不是 Mac 密码。
- MYSQL_PASSWORD：ai_teaching_app 账号密码；不是网站用户的登录密码。

使用两组不同的强密码，建议用密码管理器生成至少 20 位字母数字随机串。不要把真实密码写入 .env.example、聊天、截图或 Git。密码含有 $、# 等特殊字符时，用单引号包住整个值可避免 Compose 插值或注释歧义；若密码本身含引号，需按 dotenv 语法正确转义。

Compose 也可能从终端的同名环境变量读取值，并优先于 .env；正常按本文操作时，不需要在终端另行设置这些变量。

## 3. 检查配置并启动

打开 Docker Desktop，确保它正常运行。以下命令在 Mac 终端执行，不是在 MySQL 内执行：

```bash
cd /Users/roey/projects/AI-teaching-agent
docker compose config --quiet
docker compose up -d db
```

- 第一条进入项目目录；换电脑时改为自己的项目路径。
- 第二条只检查配置是否能解析，不启动服务；--quiet 避免把含密码的展开配置打印出来。它不是强密码检查器。
- 第三条在后台启动 db 服务。如果本机没有 mysql:8.4 镜像，会先下载镜像。不要动原有的 welcome-to-docker 容器。

密码缺失或为空时，配置会报错，而不是使用空密码启动。首次启动需要等待数据库初始化完成。

查看运行状态和最近日志：

```bash
docker compose ps
docker compose logs --tail=50 db
```

容器运行中不等于数据库已经可以登录，以后面的实际连接测试为准。若 3306 端口冲突，先找出占用者，不要直接停止其他项目的服务。

## 4. 连接项目数据库

服务首次初始化时将创建：

- 数据库：ai_teaching_assistant。
- 数据库账号：ai_teaching_app。
- 该账号获得项目数据库范围内的权限，不是全局 root 管理员。当前开发初始化授予项目库内全部权限；后续若拆分迁移账号和日常运行账号，再进一步收窄权限。

在项目根目录执行：

```bash
docker compose exec db mysql -u ai_teaching_app -p ai_teaching_assistant
```

看到密码提示时，输入 MYSQL_PASSWORD 对应的密码。不要把密码紧跟在 -p 后写进命令；交互输入时终端不显示字符是正常现象。

进入 MySQL 后执行：

```sql
SELECT DATABASE();
SELECT 1;
EXIT;
```

前两条应分别返回 ai_teaching_assistant 和 1。EXIT 只退出客户端，不停止数据库。

再从 Mac 本机的客户端测试端口映射：

```bash
mysql --protocol=TCP -h 127.0.0.1 -P 3306 -u ai_teaching_app -p ai_teaching_assistant
```

登录后执行相同的 SQL。客户端连接已验证；目前 Python 驱动、backend/.env 和连接检查代码也已准备好，下一步实际执行命令见 [后端说明](../backend/README.md)。真实 Python 连接测试尚未执行。当前 /health 不检查数据库，不能用它的成功代替数据库测试。

## 5. 数据放哪里？怎样停止？

数据存放在 Docker 管理的具名卷中，挂载到容器内 /var/lib/mysql。不要手动修改数据库内部文件。

```bash
# 暂停使用：停止服务，保留数据
docker compose stop db

# 恢复已经创建的服务
docker compose start db
```

普通停止、启动以及保留原数据卷的容器重建不会清空数据；删除数据卷、Docker 清理存储或执行 docker compose down -v 会有数据丢失风险。数据卷不是备份。

首次初始化后，修改 .env 不会自动修改数据库账号密码。不要为了改密码删除数据卷；届时使用数据库的密码修改操作并同步配置。

本机只开放 127.0.0.1:3306。数据库不直接暴露给浏览器；前端通过 Python 后端访问业务数据，不能拿到数据库密码。

参考：[MySQL 官方镜像](https://hub.docker.com/_/mysql)、[Docker 数据卷](https://docs.docker.com/engine/storage/volumes/)。

# ADR-023: 沙箱在部署环境真正启用（backend 挂宿主机 Docker 套接字）

状态：已接受

## 背景

用户实机截图显示「工具与配置 → 执行边界」为 **docker 不可用**，问是什么原因。核实结论：
**沙箱代码是完整的，缺的是部署里的一根挂载。**

- `app/sandbox/docker_runtime.py` 的隔离参数一应俱全（`read_only` + `mem_limit` + `nano_cpus` +
  `pids_limit` + `network_disabled` + `user=nobody` + `no-new-privileges`），`DeniedSandbox`
  兜底也不降级；但 `deploy/compose.yaml` 的 backend **没有挂 `/var/run/docker.sock`**，
  而沙箱是**通过守护进程创建容器**的，于是 `build_sandbox().available()` 恒为 `False`。
  ADR-020 当初只把这件事做成**可见的**，明确写了「本 ADR 不解决」。
- 探测本身也在骗人：接口层的 `_sandbox_availability` 只在 `available()` **抛异常**时才回具体原因，
  而 `available()` 把所有异常都吞成 `False`——那段 `except` 是死代码。界面因此永远显示
  一句猜测的兜底文案，真实原因（`FileNotFoundError`）只在容器日志里。
- 环境约束（实测）：本机 Docker Hub 直连超时、没有配置任何 registry 镜像，**拉不到新镜像**；
  宿主机上也没有代码默认的 `python:3.12-slim`。但 backend 镜像里有 `/usr/local/bin/python 3.12.12`。
- `docker.from_env()` 认 `DOCKER_HOST`，所以**换传输不需要改一行沙箱代码**。

## 决策

**给 backend 挂宿主机套接字（Docker-outside-of-Docker），沙箱容器成为 backend 的兄弟容器。**

配套四条：

1. `deploy/compose.yaml` 的 backend 增 `volumes: - ${SANDBOX_DOCKER_SOCKET:-/var/run/docker.sock}:
   /var/run/docker.sock`；镜像用 `SANDBOX_IMAGE` 指到**本项目自建镜像**——compose 一定会把它构建
   出来，离线/内网也一定能用。能拉到官方镜像的环境删掉这行即可回落 `python:3.12-slim`。
2. 沙箱容器补两条硬化：`cap_drop=['ALL']`（默认能力集里的 `CHOWN`/`SETUID`/`NET_RAW` 在
   无权、只读、无网的一次性容器里一样都用不上），以及给 `/app` 挂一个 1 MB 的空 tmpfs——
   复用本项目镜像时，这能把平台源码从监狱里遮掉。
3. 探测改为 `Sandbox.unavailable_reason() -> str | None`，**同时查守护进程与镜像**。
   只 `ping()` 不看镜像会给出「绿灯、但第一次执行代码就失败」的假象（容器建在宿主机守护进程上，
   镜像必须先存在于宿主机）。接口层原样透传这个原因，不再自己编。
4. 镜像缺失时给**可行动**的错误（`docker pull ...` / 改 `SANDBOX_IMAGE` / 开
   `SANDBOX_AUTO_PULL_IMAGE`），而不是 docker 库的 `ImageNotFound` 原文。
   自拉默认**关**：拉取是可能长时间阻塞的动作（本机实测直连十分钟不返回），安全边界组件应当
   失败得快、原因得准。

## 安全边界（这一条必须说清楚）

挂上套接字等于**把宿主机的 root 等价权限间接交给 backend 容器**。接受它的理由与代价都写在这里：

- 要防的威胁是**LLM 生成的代码**（提示注入之类）。它永远拿不到套接字：先过语言层策略
  （禁 `open`、禁 `import`、禁 shell 危险模式），再进一个无权、只读、无网、限内存/CPU/进程数、
  丢弃全部能力的一次性容器。实测里 `import os` 在起容器之前就被拒。
- 残余风险是 **API 进程自身**被攻破（依赖链漏洞、未预期入口）。那时套接字就是宿主 root。
  缓解只有一条：**8000 端口只暴露给可信网络**，不要放到公网上。这条写进 README。
- **不引入 socket-proxy 的理由**（本来是最想做的方案，两个原因都不成立）：
  一是本机拉不到那个镜像；二是**它其实挡不住**——沙箱合法需要的 `/containers/create` 本身就能
  提交一个挂载宿主根目录的 privileged 容器，代理只是减少可用端点，不改变「谁能 create 谁就是
  宿主 root」这件事。真要继续收窄，得换成远程/rootless 守护进程，而不是加一个转发层。
- 给挂载加 `:ro` 是**无意义的**：对 unix socket 的只读挂载只挡住创建/删除 socket 文件，
  不挡连接与写入。这里不写它，免得看起来像一层防护。

## 备选方案

- **保持不可用**。否决：模块 3「代码执行」在部署里永远是死的，演示时无从展示；只读面板会长期
  挂着红字，而那行红字说的其实只是「少挂了一个卷」。
- **socket-proxy sidecar**。暂缓，理由见上。将来要做与本次决策不冲突：把 `DOCKER_HOST` 指向
  代理即可，**零代码改动**（`docker.from_env()` 认这个变量）。
- **在 backend 进程内执行代码**。否决：违反 ADR-012 定的执行边界，也比现在弱得多。
- **另起一个常驻 sidecar「监狱」服务，backend 通过 HTTP 让它跑代码**。否决：它的网络不能关
  （backend 要连得上），于是被执行的代码重新拿到网络与 compose 网络内的横向可达性，
  比现在**更弱**；一次性文件系统也没了。

## 影响

- `deploy/compose.yaml`（成员 B 的目录，本次为跨线的必要改动）：backend 增一个卷与两个环境变量。
- `app/sandbox/config.py`（`auto_pull_image` + `image` 注释）、`app/sandbox/runtime.py`
  （协议增 `unavailable_reason`）、`app/sandbox/docker_runtime.py`（探测、硬化、镜像缺失处理）、
  `app/api/main.py::_sandbox_availability`。
- 新增 `tests/unit/test_sandbox_docker_runtime.py`（10 例）：用假 client 钉住探测与硬化参数，
  真实执行仍归 §4 的实机验收。
- `doc/api.md` §5.15 补「原因语义 + 镜像前置条件」，README 补安全代价与关闭方式。
- **实机证据**（容器重建后实测）：`GET /config/sandbox` → `available=true`、`reason=null`；
  容器内探针 —— 真实执行 `print(sum(range(1,101)))` → `exit=0, stdout=5050, 317 ms`；
  `import os` → `SandboxViolation: 禁止导入模块: os`；超时用例 → `timed_out=true, exit=124`、
  3.4 s 后容器被终止；工具层 `CodeExecutionTool.invoke` 同样返回 `docker/exit 0`；
  运行后 `docker ps -a --filter label=macp.role=tool-sandbox` **无残留**。
- **未解决**：能跑代码 ≠ Agent 能读写文件。策略仍禁 `open` 与 `import`，也没有任何文件工具
  （`app/tools` 只有 calculator / web_search / sql_query / code_execution），沙箱没有挂载卷。
  「让 Agent 自己读文件」要动的是 `app/tools` + `app/sandbox` 的策略，属成员 C 的范围。

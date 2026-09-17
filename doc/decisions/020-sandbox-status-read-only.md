# ADR-020: 执行边界只读（沙箱状态不做运行期可改开关）

状态：已接受

## 背景

用户问到「沙箱配置在前端有吗，需不需要安排 UI」。核实结果：**前端完全没有沙箱配置**。

- `app/sandbox/config.py` 的 `SandboxSettings` 只有 8 个字段（`backend`、`image`、
  `timeout_seconds`、`memory_limit`、`cpu_limit`、`pids_limit`、`network_enabled`、
  `output_limit_chars`、`max_code_chars`），全部走 `SANDBOX_` 环境变量，**没有任何 API**；
- 前端 `grep SANDBOX|sandbox` 只命中 `InlineConfirm` 的注释（讲的是宿主 iframe 的 sandbox，
  与这个无关）；
- 内置工具（`TOOL_*`）同样只有环境变量，也没有 API。

与此同时还有一个更要紧的事实：**部署环境里沙箱根本不可用**。`app/sandbox` 的隔离实现是完整的
（`read_only` + `mem_limit` + `nano_cpus` + `pids_limit` + `network_disabled` + `user=nobody` +
`no-new-privileges`，且 `DeniedSandbox` 兜底**不降级**为宿主进程执行），但 `deploy/compose.yaml`
的 backend 没有挂载 `/var/run/docker.sock`、也没有 privileged，因此
`build_sandbox().available()` 恒为 `False`，代码执行的工具调用必然以
`SandboxUnavailable: Docker 守护进程不可用` 失败。

## 决策

**只做只读展示，不做运行时可改的编辑界面。**

新增 `GET /api/v1/config/sandbox`（`doc/api.md` §5.15）与「工具与配置 → 执行边界」分区，
返回「当前生效限额 + 可用性探测 + 不可用原因」，**没有任何写接口**。

理由分两层：

1. **可编辑沙箱配置是安全反模式**。这些参数的语义是「限制这个容器能做什么」。做成运行时可改的
   界面，等价于让一个 Web 操作者放宽自己所在容器的隔离——攻击面从「运维能改部署」扩大到
   「任何能打开这个页面的人都能改」。前端有写入口的既有先例（§5.7/§5.8/§5.11）都是**业务
   配置**（模型、路由、工具登记），不是**安全边界**，两者不该套同一个模式。
2. **在沙箱不可用的当前状态下，做出来的会是一个假开关**。改 `SANDBOX_*` 不会让
   `available()` 变成 `True`（缺的是 docker.sock，不是限额），用户点完保存后唯一的可见变化是
   「设置生效了但工具还是不能用」——这个项目已经因为同一个病根吃过两次亏
   （`allowAutoExecution` 只存不用、`decision_agent_id` 假选择，见 ADR-018）。

只读视图解决的是**真正的信息缺口**：「代码完整但部署不可用」这件事此前只能翻容器日志才能发现。
把它变成界面上的一行原因，比加一个改不动的输入框有用。

## 备选方案

- **整块不做**（沿用「前端没有就不做」）。否决：`available()` 恒为 `False` 是个隐蔽的部署事实，
  评审和演示时问「代码执行能不能用」，答案是「不能，而且界面上看不出来」——这是可观测性缺口，
  与做不做开关是两件事。
- **做完整读写 + 加权限边界**。暂缓：需要引入「谁能改安全边界」的鉴权模型，而本项目的写入接口
  当前是凭 token 边界（ADR-015 已取消）。在沙箱本身不可用的阶段引入这套复杂度，收益为负。
- **把沙箱限额接进 `provider_configs` 那样的双存储（PG 事实源 + Redis 镜像）**。否决：
  环境变量是**进程启动期**读入的（`get_sandbox_settings` 带 `lru_cache`），改库不会影响已启动的
  容器，做成「存了但要重启才生效」的设置只会制造误解。

## 影响

- 新增 `GET /api/v1/config/sandbox`（只读）与 `frontend/src/config/SandboxPanel.tsx`；
  「工具与配置」从三分区变四分区（ADR-018 的「三块视图职责互斥」原则不变，新分区同样只读
  且不重复渲染别处的数据）。
- `SandboxPanel` 按显式 props 导出 `SandboxBoundary`，纳入 `config-smoke.tsx` 与
  `ui-preview.html` 的断言（含「分区内不得出现任何 `input`/`select`/`textarea`」）。
- 集成测试断言 `PUT /api/v1/config/sandbox` 返回 `405`——**写接口是被显式测掉的**，
  以后要加必须先改这条用例和本 ADR。
- **未解决**：`deploy/compose.yaml` 未挂 docker.sock 的问题本 ADR **不解决**。要真正跑通代码
  执行，需要给 backend 挂 `/var/run/docker.sock`（等于把宿主 Docker 控制权交给容器，安全代价
  不小）或改用无 Docker 的隔离后端（如 Wasm/`nsjail`）。这属成员 C 的 `app/sandbox` 与成员 B 的
  `deploy/` 边界，本 ADR 只保证这件事**是可见的**。

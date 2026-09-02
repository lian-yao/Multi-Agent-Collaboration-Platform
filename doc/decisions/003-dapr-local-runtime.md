# ADR-003: Dapr 本地运行时初始化方式

状态：已接受

## 背景

Docker Hub 在当前网络环境不可达，默认 `dapr init` 无法完成镜像拉取。

## 决策

使用 GitHub Container Registry 初始化：

```bash
DAPR_DEFAULT_IMAGE_REGISTRY=ghcr dapr init --runtime-version 1.18.2
```

## 影响

- Dapr 控制面镜像从 GHCR 拉取。
- Redis 与 Zipkin 镜像从可用镜像站预拉并打标准 tag。
- 网络恢复后仍可使用默认 `dapr init`。

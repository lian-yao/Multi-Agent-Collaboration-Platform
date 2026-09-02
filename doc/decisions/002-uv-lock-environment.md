# ADR-002: 环境以 uv.lock 为准

状态：已接受

## 背景

项目同时存在 `doc/requirements.txt` 与 `pyproject.toml`，需要明确哪个文件决定实际环境。

## 决策

实际环境以 `pyproject.toml + uv.lock` 为准，安装命令为 `uv sync`。`doc/requirements.txt` 只作为给人看的依赖清单。

## 影响

- 依赖变更需同步维护 `pyproject.toml`、`uv.lock`、`doc/requirements.txt`。
- 环境可复现性由 `uv.lock` 保证。
- 不再使用 `uv pip install -r doc/requirements.txt` 作为标准安装方式。

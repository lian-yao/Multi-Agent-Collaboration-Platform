# 工程规范

## 文档优先

- `doc/` 是项目文档目录，设计文档是事实源。
- 实现必须能追溯回文档条款，不能自由发挥。
- 发现文档错误时，先修改文档，再修改代码。
- 重要决策必须写入 `doc/decisions/`，不能只留在对话中。

## 环境与依赖

- 使用 UV 管理 Python 虚拟环境。
- 实际环境以 `pyproject.toml + uv.lock` 为准，安装命令为 `uv sync`。
- `doc/requirements.txt` 是给人看的依赖清单。
- 依赖变更时同步维护 `pyproject.toml`、`uv.lock`、`doc/requirements.txt` 三处。
- 不直接依赖系统 Python 环境。

## 代码规范

- Python 3.12，后端使用 FastAPI + Pydantic。
- 使用类型注解，数据边界使用 Pydantic Schema。
- 只添加解释复杂逻辑的注释，避免空注释。
- 结构化解析优先使用成熟库/API，避免 ad-hoc 字符串处理。
- 不引入未在文档中出现的能力或抽象。

## Git 规范

- 提交按逻辑单元拆分，commit message 说明原因。
- 不删除或回退用户已有的未提交改动。
- 禁止破坏性 git 操作，除非用户明确要求。
- 提交前检查 `git diff`，确保无无关文件。

## 完成定义

- 改动有文档依据，且与文档一致。
- 相关测试通过，关键链路有覆盖。
- `pyproject.toml`、`uv.lock`、`doc/requirements.txt` 保持一致。
- 向用户汇报时说明做了什么、验证了什么、哪些未完成。

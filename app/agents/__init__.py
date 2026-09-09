"""Agent 角色定义（成员 C D5-6 内容层：角色 Prompt 与场景数据）。

目录归属说明：本目录按分工 §1 归成员 A（Agent 角色与团队定义），
角色 Prompt 内容按分工 §2/§3 由成员 C 编写。二者通过文件级隔离：
- 成员 A：图结构、团队与角色分配；
- 成员 C：`roles.py` 的角色 Prompt 内容。

跨目录改动，合并前需 A 确认（协作机制 §4）。决策见 ADR-006。
"""

from app.agents.roles import (
    ROLE_DEFINITIONS,
    RoleDefinition,
    RoleId,
    get_role,
    role_ids,
)

__all__ = [
    "ROLE_DEFINITIONS",
    "RoleDefinition",
    "RoleId",
    "get_role",
    "role_ids",
]

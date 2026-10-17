"""DAG 词汇表 —— 画布节点定义、节点类型注册表、规划数据契约。

这一层曾经是**第二套执行引擎**（LangGraph 编排 + 拓扑并行 + 事件聚合 + IPC +
守护进程）。那段执行面从未真正投入使用：2026-10-17 连同 8 个模块
（``daemon_runner`` / ``engine`` / ``event_aggregator`` / ``ipc_adapter`` /
``error_classifier`` / ``version_manager`` / ``validator`` /
``prompt_validator``）与 9 个只覆盖它们的测试一并删除。

留下的是三样**仍被生产代码使用**的东西：

1. **models** —— 默认 DAG 的节点 id 与顺序，是前端画布与运行时状态投影的
   单一数据源（``application/engine/narrative_projection/linkage_kernel.py``）。
2. **registry** —— ``node_type`` → CPMS 提示词键的权威映射，提示词广场返回的
   ``dag_bindings`` 直接用它（``interfaces/api/v1/workbench/llm_control.py``）。
3. **plan** —— 章前执行计划的 schema 与生成函数。``context_builder.py``、
   ``beat_projection.py``、``engine/runtime/legacy_writing_delegate.py``
   把它们当**数据契约**用，不是当执行能力用。

历史
----
``DAGDaemonRunner`` 与 ``POST /dag/{id}/run`` 曾有真实实现，2026-05-16
00:08:55 引入（``d037b657``），7 秒后被同一次批量导入里的下一个 commit
（``fc5de029``「后端路由精简为纯展示层」）拆掉。生产写作路径自 2026-05-21
（``52aa0d20``，引入 ``PLOTPILOT_USE_STORY_PIPELINE``）起统一走
``engine/runtime`` 的 ``EngineDaemon`` → ``StoryPipelineRunner`` →
``BaseStoryPipeline``。

``nodes/`` 下各节点的 ``execute()`` 同样从未被执行过；节点模块现在只为
``@NodeRegistry.register`` 的注册副作用与 ``meta`` 元数据而存在。
"""

# 核心模型
from application.engine.dag.models import (
    DAGDefinition,
    DAGMetadata,
    DAGRunResult,
    EdgeCondition,
    EdgeDefinition,
    NodeCategory,
    NodeConfig,
    NodeDefinition,
    NodeEvent,
    NodeMeta,
    NodePort,
    NodeResult,
    NodeRunState,
    NodeStatus,
    NovelWorkflowState,
    PortDataType,
    get_default_dag,
)

# 节点类型注册表
from application.engine.dag.registry import BaseNode, NodeRegistry

__all__ = [
    # 模型
    "DAGDefinition",
    "DAGMetadata",
    "DAGRunResult",
    "EdgeCondition",
    "EdgeDefinition",
    "NodeCategory",
    "NodeConfig",
    "NodeDefinition",
    "NodeEvent",
    "NodeMeta",
    "NodePort",
    "NodeResult",
    "NodeRunState",
    "NodeStatus",
    "NovelWorkflowState",
    "PortDataType",
    "get_default_dag",
    # 注册表
    "BaseNode",
    "NodeRegistry",
]

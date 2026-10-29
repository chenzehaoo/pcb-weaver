# 文档导航

[返回项目首页](../README.md)

## 使用与架构

| 阅读目标 | 文档 |
| --- | --- |
| 项目做什么、输入是什么、两条 CAD 链路有何区别 | [项目首页](../README.md) |
| 系统分层、流水线、版本证据与执行模型 | [技术架构（五张矢量图）](ARCHITECTURE.zh-CN.md) |
| 从克隆仓库到 MCP 调用和 Worker 执行 | [部署与 MCP 接入](GETTING-STARTED.zh-CN.md) |
| 机器令牌、授权范围、幂等与数据交换 | [本机机器接口](INTEGRATION.zh-CN.md) |
| 原生工具与执行配置 | [工具链说明](../tools/README.md) |
| Altium 静态检查、任务、租约与边界 | [Altium 服务](ALTIUM-SERVICE-BETA.zh-CN.md) |
| 新机器的 Altium 配置 | [Altium 配置与发布说明](GITHUB-RELEASE.zh-CN.md) |
| 修改架构图或用于技术报告 | [架构图源与生成](assets/architecture/README.md) |

## 验证与研究记录

以下文档保留各自实验条件、版本与结果。它们是特定范围的记录，不是所有板型、所有工具版本或默认公开入口的能力保证。部分原生证据保存在本机，不随仓库提供；复现时应重新验证。

| 主题 | 记录 |
| --- | --- |
| 固定布局整板链路 | [固定布局验收](FIXED-WHOLEBOARD-VALIDATION.md) |
| 系统级输入、布局与原生检查 | [系统验证](SYSTEM_VALIDATION.zh-CN.md)、[系统基准](SYSTEM_BENCHMARK.md) |
| 有限完成与局部修复 | [完成流程](COMPLETION-VALIDATION.md)、[修复验证](REPAIR_VALIDATION.zh-CN.md)、[自主修复](AUTONOMOUS-REPAIR-VALIDATION.md) |
| 网络连接与多网修复 | [连接完成](CONNECTION-FINISH-VALIDATION.md)、[多网修复](MULTINET-REPAIR-VALIDATION.md)、[联合修复](JOINT-REPAIR-VALIDATION.md) |
| 早期实现与演进 | [历史契约](IMPLEMENTATION_CONTRACT.md)、[系统阶段方案](SYSTEM_PLAN.zh-CN.md)、[v3 验证](V3_VALIDATION.zh-CN.md) |
| Altium 阶段实验 | [开发者内测](ALTIUM-DEVELOPER-BETA.md)、[原生实验](ALTIUM-NATIVE-PHASE1.md)、[修复与重设计](ALTIUM-REPAIR-AND-REDESIGN.md) |
| 调研依据 | [技术研究](RESEARCH.zh-CN.md) |

当前模块与默认入口请以[技术架构](ARCHITECTURE.zh-CN.md)、启动脚本和对应服务代码为准。历史实验文档中的阶段目标，不应直接读成已经交付的产品能力。

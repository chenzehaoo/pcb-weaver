# 系统版落地与验收方案

本轮目标是将 v0.1 的四器件演示链路升级为能处理真实复杂工程的本地 PCB 工程平台。不能以页面复杂度、MCP 工具数量或测试用例数量代替板级能力；下表均要求具体文件、规则和原生工具证据。

## 技术路线

| 部分 | 实施路线 | 验收对象 |
| --- | --- | --- |
| 复杂工程输入 | KiCad 原理图层次、板、项目规则、本地库、来源许可的不可变快照 | 真实 MCU/CPLD 控制板；源文件保持不变 |
| 布局引擎 | 小板 SLSQP、大板分块坐标优化、冲突分组最小位移合法化；固定器件、区域、邻近及间距复验 | 160 器件真实几何；分别报告可行性、位移、时间和 HPWL |
| 多层与封装 | 2/4/6/8 层约束 Schema；真实焊盘尺寸、类型、铜层；逐项测试背面与孔几何 | 四层真实基准与原生坐标对比；其他层数另列测试范围 |
| 网络规则 | 按具体网络编译 netclass，保留原规则并核对原生加载值 | 混合线宽实际 DSN、SES 和最终铜线，不能只查 JSON |
| 布线 | KiCad 原生 DSN/SES 与本地 Freerouting；保留真实操作日志 | 未连接数量、最终 DRC、版图/网络身份不变性 |
| 工程工作台 | HTTP API、交互式 canvas、图层控制、网络高亮、器件搜索、候选与检查视图 | 真实板数据、桌面/手机截图、像素和交互测试 |
| 长任务 | SQLite 队列、原生阶段记录、取消请求、单活工作进程、重启中断状态 | 多客户端不会重复领取作业，不把中断或阻断变成成功 |
| Agent 接口 | Skill + MCP 保留直接工具，加入持久化作业工具 | MCP 协议与 HTTP 调用共享业务门禁 |
| 发布 | 新鲜 ERC/DRC、连通性、声明约束、制造输出、BOM/CPL 与哈希归档 | 通过板真实导出；未布线及篡改版本拒绝 |

## 基准选择

从 KiCad 官方源码镜像的固定提交 `286b0611feca00727bf70bfa184ec2c28a745dc3` 比较 `interf_u`、`kit-dev-coldfire-xilinx_5213`、`video` 等示例。选中 ColdFire/CPLD 工程：160 个器件、825 个物理焊盘、278 个网络、4 个铜层，包含 MCU、CPLD、CAN、RS232 和电源模块。原理图保持真实电气引脚类型，不构造全被动假电路。

基准是保留来源的衍生测试工程，不是全新参考电路，也不是原厂量产批准设计。去除原铜以测试重新布线、规则迁移或库修复等改动必须逐项记录于 `examples/system-controller/PROVENANCE.json`，不能把已有人工走线当成自动生成结果。

四器件板保留作快速回归。更大的 `video` 和 1508 器件、12 层的 VME 示例仅作为未来规模参照；本轮未验证前不得宣称支持该级别。

## 软件架构

MCP 与 HTTP 都调用同一 `EngineeringService`。布局、路由、验证和发布仍以精确 revision 为单位。`JobQueue` 承接长任务，每个阶段保存结果文件及数据库事件，前端只呈现实际状态，不估造布线百分比。

队列使用数据库事务领取任务，以操作系统锁保持单活工作进程。工作台或独立 `pcb_weaver.worker` 承载执行，MCP 会话仅提交和查询，避免会话退出终止后续排队任务。取消发生在工程阶段边界，不强杀正在保存文件的 KiCad。前一工作进程退出后，残留运行态标为 `interrupted`，不自动重放可能已产生新版本的修改。

工作台仅监听 `127.0.0.1`，不上传 PCB；校验 Host、Origin 和写请求标识，制造下载只提供账本登记且哈希未变化的归档。它是单用户本地部署，不冒充有企业身份认证、权限隔离和高可用保障的公网多租户平台。

## 判断标准

正向验收必须显示实际器件与封装规模、优化候选、最终走线/过孔/层数、DRC/ERC 未连接和错误数、原理图一致性、制造文件集合。任何失败要定位修复输入或实现，不得关闭关键规则换取通过。

负向验收包括原始未连接板发布、规则禁用、旧版证据、不可行约束、文件篡改、任务取消/中断、跨站调用和归档篡改。纯模拟、原生集成、真实基准、界面测试分别统计。

ERC/DRC 通过不代表功能、时序、阻抗、回流、电源完整性、热、EMC、安规或量产良率通过。工业导入仍需要规格合同、硬件工程签核和实物验证，本项目不提供未执行检查的结论。

## 一手材料

- [KiCad 官方复杂示例源码](https://github.com/KiCad/kicad-source-mirror/tree/286b0611feca00727bf70bfa184ec2c28a745dc3/demos/kit-dev-coldfire-xilinx_5213)：真实多模块工程来源。
- [KiCad 9 PCB 文档](https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html)：规则、铜层与路由概念；最终以安装版本实测为准。
- [Freerouting 架构](https://github.com/freerouting/freerouting/blob/master/docs/architecture.md)：全局布线与后优化的职责分离。默认分支资料用于路线研究，不当作固定 JAR 的能力保证。
- [Freerouting 命令参数](https://github.com/freerouting/freerouting/blob/master/docs/command_line_arguments.md)：路由轮数、线程和层参数的研究入口；具体调用必须通过固定版本验证。
- [固定使用的 Freerouting 2.0.1](https://github.com/freerouting/freerouting/tree/v2.0.1)：本地路由引擎与来源。当前主线能力不能无验证地套用到旧版本。
- [Freerouting 1.9.0](https://github.com/freerouting/freerouting/tree/v1.9.0)：新增隔离执行配置，用同一 DSN 与 2.0.1 对照；实际结果见系统验证记录，不把版本回退本身当作布通证明。
- [Freerouting 2.4.1 发布](https://github.com/freerouting/freerouting/releases/tag/v2.4.1)：2026-09-03 发布，统一 GUI/CLI/API 执行流程，使用 Java 25。本项目以官方固定 JAR 和独立 JRE 做额外对照，不能把上游基准宣称当作本板通过证据。
- `docs/RESEARCH.zh-CN.md`：上一轮 Konnect、NiRuLabs 与 Freerouting 的精确阅读范围和业务调用链。

“所有已知材料”不是一个可证明读完的集合。材料范围、固定提交、选择原因和未研究部分应可审查；技术组合的独立设计也不等同于全球首创。

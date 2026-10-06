# 实际验收记录

这是 v0.1 四器件样板的历史验收，不代表 v0.2 复杂系统板通过。当前状态见 [系统验证](SYSTEM_VALIDATION.zh-CN.md)。

验收日期：2026-09-07。交付目录：本机历史目录（公开版本已脱敏）。

## 结论

已通过真实 MCP stdio 从桌面安装执行一条完整的小板软件工程链路：导入、读取、三套布局候选、应用、DSN 导出、Freerouting 布线、SES 回导、DRC/ERC、原理图一致性、约束复验、制造导出、ZIP 校验、ECO 和报告。

这证明该版本不是静态页面或模拟脚本，但不证明任意复杂 PCB、实物电路功能或工业量产适用性。验收对象只有原创的 4 器件、8 焊盘、3 网络双层小板，不是客户产品或合格参考设计。

## 桌面端到端结果

| 项目 | 实测结果 |
| --- | --- |
| MCP 服务 | 桌面独立 Python 环境启动，真实握手，14 个工具可发现 |
| 项目 | `desktop-acceptance` |
| 最终版本 | `r-bc141fe65a664551` |
| 布局 | J1/J2 保持固定；HPWL 从 110 mm 降至约 77.600004 mm，约减少 29.45% |
| 实际铜线 | 19 段走线、0 过孔，最终 PCB 最小线宽 0.4 mm |
| 原生 DRC | 0 错误、0 警告、0 未连接；执行了 schematic parity |
| 原生 ERC | 0 错误、0 警告 |
| 额外一致性 | XML 网表器件、值、封装与焊盘连接检查通过 |
| 几何约束 | 通过；不存在尚未检查的阻断性几何项 |
| 制造发布 | `release-689b3a353300`，新鲜复验后真实导出并打包 |
| ZIP 校验 | 工件集合和逐文件 SHA-256 校验通过 |
| 发布包 SHA-256 | `0260e213d4b4ce7ef047d98674c42981b18d71bee77d4594cf1fc824a5f37b67` |
| 审阅报告 | 桌面 1440x1000、手机 390x844 无页面溢出，实际 8 个焊盘可见 |

HPWL 是布局估计，不是最终铜线长度；优化器没有证明全局最优。

制造包包含真实 Gerber、钻孔、位置文件、原理图 BOM、工程 BOM、设计快照、约束、原始检查及 manifest。BOM/位置文件的已装配位号集合已检查；仍不能直接提交自动贴片生产，物料号、旋转、工厂工艺和 CAM 必须审阅。

完整协议调用及结果：[desktop-acceptance.json](validation/desktop-acceptance.json)。
审阅入口：本机历史 `REVIEW.html`；公开源码仓库不包含该含绝对路径的报告。
发布包：[release-689b3a353300.zip](../data/projects/desktop-acceptance/releases/release-689b3a353300.zip)。

## 负向验收

对同一项目的原始未布线版本直接调用制造发布：返回 `blocked`，真实 DRC 检出 5 项未连接，几何约束也未满足，没有生成该版本的正式发布包。证据：[unrouted-release-blocked.json](validation/unrouted-release-blocked.json)。

回归测试另覆盖：旧计划、文件篡改、关键网络、已有走线、缺报告、未知几何、关闭关键检查、子页/库越界、DNP/位置不一致、BOM 原生输出哈希、归档篡改等拒绝场景。其中模拟执行器测试只证明业务规则，不当作真实 EDA 结果。

## 测试分层

- 最终完整默认回归：**277 passed，4 skipped**，29.46 秒。包含真实 stdio 握手与导入、布局、应用、ECO、关键网络阻断和报告调用。机器可读记录：[pytest.xml](validation/pytest.xml)。
- 默认跳过 2 项需要显式开启的 WSL 实机测试，以及 2 项因当前 Windows 权限不能创建符号链接的测试。跳过不计为通过。
- 原生适配器独立实机测试：**64 passed**，包括上述 2 项 WSL 测试。本次开启 KiCad 与 JAR 路径后实际执行；记录在 [native-toolchain](validation/native-toolchain)。
- 最终桌面 MCP 全链验收另外执行，不依赖模拟工具；正向发布及负向拒绝结果见上节。
- Skill 格式验证及插件 manifest 验证均通过。独立代理进行了轻量 Skill 流程演练；不是大样本模型行为评测。报告在 `validation/board-agent/FORWARD_TEST.md`，随后补充了缺路由器和布线前未连接的决策说明。
- HTML 报告通过 Playwright 实际 Chrome 截图和几何可见性检查；记录及截图在 `docs/validation/`。

## 实际工具版本

Windows Python 通过 WSL Ubuntu 调用 KiCad **9.0.9**、其 pcbnew 原生绑定、Java **21**、Freerouting **2.0.1**。JAR SHA-256 和来源见 `tools/README.md`。

规则适配修复了缺失 `net_settings.meta` 导致原生规则被忽略的问题，并逐项核对原生 netclass 实值。SES 量化误差只在声明分辨率及 0.0001 mm 安全上限内恢复原始精确位置；超限、身份、角度、层或网络改变均拒绝。恢复后仍重新运行真实 DRC。

KiCad 8/10 只有能力探测与兼容代码，未在本机验收；不要写成三个版本均已验证。当前 SWIG 适配层后续需要向 IPC 迁移。

## 复现

桌面目录已创建 `.venv` 并安装依赖，已生成绝对路径 `.mcp.json`。尚未修改全局客户端设置，也没有把新 MCP 自动注入当前对话。

```powershell
./Run-Demo.ps1 -Route
.venv/Scripts/python.exe scripts/accept_mcp.py --route
.venv/Scripts/python.exe -m pytest tests -q
```

第二条命令默认使用新的项目名，证据写入 `docs/validation/`。需连接到其他 MCP 客户端时注册本目录 `.mcp.json` 中的服务器配置；移动目录后重新生成配置。

## 尚未完成的工业验证

未打样、未测量实板功能、未做高速/射频/电源完整性/热/EMC/安规分析、未对接厂商订单或 MES、未验证大型工程性能、未做外部安全审计或跨工具版本认证。支持范围仍限于 README 描述的小板，不是通用工业 EDA 的替代品。

所读上游仓库的准确文件与范围见 `RESEARCH.zh-CN.md`。未声称读完全部大型仓库，也不把本项目的独立实现等同于全球首次发明。

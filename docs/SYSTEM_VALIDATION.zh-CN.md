# PCB Weaver 0.2 系统版验证

状态：平台软件及独立小板制造链路通过；复杂板整板验收未通过。最佳 7 未连接，过孔修正对照为 8 未连接。保留真实失败证据，不宣称工业级自动布线或制造放行通过。

## 验收对象

KiCad 官方 ColdFire/CPLD 多模块工程的有来源衍生基准：160 器件、825 物理焊盘、807 电气焊盘身份、278 网络、4 铜层、35 封装种类、3 页原理图。包含 14 个背面电容、槽孔、QFP、SOIC、SOT、晶振、电源及多类连接器。原有走线、过孔和铜区已在基准准备阶段明确移除，不把人工布线计为本系统生成。

来源、许可和变更见 `examples/system-controller/PROVENANCE.json`、`LICENSE.KiCad.README` 及 `docs/SYSTEM_BENCHMARK.md`。输入由 `benchmarks/FROZEN.json` 固定；执行 profile 与冻结基准分开管理。

## 已完成的验证

| 层级 | 实际结果 | 证据 |
| --- | --- | --- |
| 冻结基准原生检查 | ERC 0 错误、16 警告；DRC 499 未连接错误、47 库差异警告；原理图一致性 0 项 | `benchmarks/results/system-controller-frozen/baseline.json` |
| 大板分块候选 | 3 个可行候选，约 1.5 秒/候选；选中 HPWL 8702.712 mm，比原有无效布局更长，不宣称线长优化 | `data/jobs/job-26add59a69f24a81/plan.json` |
| 最小位移合法化 | HPWL 7929.679 mm；27 件移动，总位移 2.682 mm、最大约 0.267 mm；真实 plan/apply 及独立几何检查通过 | `data/jobs/job-3387a33b624d4940/plan.json`、`apply.json` |
| 原生几何 | 825 个焊盘的正反面坐标、翻面和位移回读测试通过；槽孔方向和穿孔占位有回归 | `tests/test_system_placement.py` 等 |
| 2.0.1 原生矩阵 | 真实子集 13 通过、1 失败；2/4/6/8 层小样板的 7 组线宽/DRC/导出组合通过，2 层受控缩颈组空 SES 失败。不得算成全通过 | `docs/validation/native-v2/summary.json` |
| 1.9 小板 | 四层样板往返、0.6/0.25 mm 混合线宽及原生 DRC 通过；其他层数不能继承 2.0.1 的测试结论 | `docs/validation/native-v2/legacy-small-20260907-b/` |
| 工作台 | 160 器件、2644 段实际走线，1600×1000 / 390×844 无横向溢出；图层、缩放、实际 DRC 对象定位及工程/版本深链接恢复通过，桌面对象选择通过 | `docs/validation/platform-system-linked/browser-results.json` 与截图 |
| 页面状态绑定 | 延迟旧请求不能覆盖新工程；确认弹窗始终提交其打开时的版本。写请求拦截用于 UI 测试，不冒充真实作业执行 | 同上 `navigation_race`、`modal_binding` |
| 静态报告 | 已布线版本的 825 焊盘真实形状分组，桌面/手机无溢出，槽孔尺寸单元验证通过 | `docs/validation/report-system-routed/report-visual-check.json`、`tests/test_report.py` |
| 真实 MCP 负向验收 | 冻结复杂板的固定件冲突导致候选不可行；实际 ERC/DRC 后拒绝发布，历史中无 release_created | `docs/validation/negative-system-mcp.json` |
| 协议与服务 | HTTP 与真实 MCP stdio 共用服务和队列；18 工具、异步提交及结果查询已测 | `tests/test_platform.py`、`test_mcp.py`、`test_jobs.py` |
| 软件回归快照 | 760 项通过、24 项显式跳过，127.44 秒；有 2 条第三方弃用提示 | `docs/validation/pytest-system.xml` |
| 长任务协议隔离 | 真实子进程读取 stdin 得到 EOF，心跳存续期间 12 轮 get/list 共 24 次 MCP 请求全部响应；此项不是原生 EDA 测试 | `tests/test_mcp_subprocess.py`、`docs/validation/mcp-subprocess/evidence.json` |
| 原生扇出 | 四层同输入 A/B 均 0 错误、0 未连接、8 警告；单独扇出阶段真实生成 2 过孔并读回完整设置 | `docs/validation/native-v2/fanout-v1/README.md` |
| 2.4.1 原生小板 | 隔离 Java 25 环境下，启用/关闭扇出的两次小板往返均为 0 错误、0 未连接、8 警告；不是字节相同输入 A/B，也不是大板通过证明 | `docs/validation/native-v2/unified-v241/README.md` |
| Windows 并发路径 | 复现 Python 3.12 在父目录并发创建时的路径解析差异；严格解析已存在祖先并复验，外部 junction 仍被拒绝 | `tests/test_storage_paths.py` |
| 纳米布局落盘 | 只将要写入的位置规范到 KiCad 1 nm 网格，再对实际落盘板复验约束；没有放宽路由身份检查 | `tests/test_board.py`、`tests/test_service.py` |
| 真实小板完整 MCP | 新的独立制造回归样板完成布局、2.4.1 布线、ERC/DRC、制造输出、ZIP 校验、审阅报告和 ECO；22 段铜线，0 过孔。不是复杂板通过证明 | `docs/validation/mcp-manufacturing-final.json` |
| 搬迁逻辑 | 旧目录失效后，临时副本的历史、HTTP 下载、报告和真实 MCP 新路径校验通过；篡改/丢失拒绝。该项采用假 EDA 产物，不冒充桌面原生验证 | `tests/test_release_relocation.py` |
| Skill / 插件 | 官方验证脚本通过；桌面隔离环境安装版本 0.2.0 | 项目 `.codex-plugin/plugin.json`、`skills/pcb-engineering/` |

HPWL 是网络焊盘包围框半周长估计，不是完成铜线长度。求解器返回 success 也不能替代独立可行性复验。

## 布线问题与保留证据

1. 第一轮：分块布局、Freerouting 2.0.1、禁止缩颈、900 秒上限，发生超时并被阻断，未生成发布。原始命令、DSN、日志和失败记录保留在 `data/jobs/job-26add59a69f24a81/route.json` 及对应 routing 目录。
2. 单独诊断实验：启用受控缩颈后，一轮可回导 1451 段走线，POWER 最窄 0.3004 mm，仍高于声明的 0.2 mm 制造下限；该实验仍有 163 未连接，不能作为整板通过证据。
3. 第二轮：正式最小位移候选、受控缩颈、3 轮，进程退出码 0 但 SES 长度为 0。文件完整性门禁正确拒绝回导和发布。记录为 `data/jobs/job-3387a33b624d4940/route.json`。
4. 同 DSN 的 Freerouting 1.9.0 对照：10 轮、自然退出、约 604 秒生成 199414 字节 SES；2648 段线宽检查及精确位置保持通过。最终 DRC 仍有 11 未连接，47 条库差异警告，无其他错误；未达到发布条件。
5. 正式 MCP 30 轮验收：任务 `job-654c350190354a31`，工程 `system-mcp-acceptance`，版本 `r-9b5f5075e65e4931`。自然结束后回导 2644 段走线、164 个过孔；实际 DRC 7 未连接、0 其他错误、47 警告；ERC 0 错误、16 警告；原理图一致性 0 项。几何和 2644 段持久化线宽下限检查通过，但整板门禁仍阻断，不生成制造包。证据在该任务 `verify.json` 和对应原生报告中。
6. 上述长任务的原 MCP 会话出现查询停滞，已保留失败客户端记录 `docs/validation/system-mcp-acceptance.json`，并在工程任务结束后关闭该会话。使用新增的 `--resume-job` 通过真实 MCP 重新读取到相同 blocked 结果，见 `docs/validation/system-mcp-acceptance-resume-7c3257c7.json`。恢复查询成功不等于工程验收成功。
7. 原生扇出 30 轮对照：`job-23dcc4cbdff544fe` / `system-fanout-acceptance` / `r-ba139b99023a44ca`，最终 2805 段走线、188 过孔，DRC 仍为 7 未连接、0 其他错误、47 警告，ERC 0 错误。MCP 持续轮询到最终 blocked，没有先前的查询停滞，但布线未改善到放行水平。
8. 排队小板回归 `job-c8f46efd8d9e4d6c` 被上一 MCP 会话退出中断，证据保留在 `docs/validation/mcp-release-regression-v2.json`。已据此改为独立常驻 Worker 模型：MCP 不再启动执行线程。新增真实协议会话退出回归，确认执行中的任务可在外部 Worker 继续完成。该修复需后续实际发布链路复验。
9. 2.4.1 对照任务 `job-8144c6d4c8f749b5` 使用独立 Java 25、启用扇出及固定运行配置，达到 1800 秒超时且未产生必需的完整结果 JSON，门禁拒绝回导和发布。日志里的中间未连接数不是 KiCad DRC 结果。原始证据在 `docs/validation/system-unified-acceptance.json`。
10. 其后排队的小板 `job-56cac33f2a024992` 在前一个 MCP 客户端退出后继续执行，未被会话生命周期终止；但最终被回导几何/网络一致性门禁阻断，不能记录为发布通过。证据在 `docs/validation/mcp-release-persistent.json`。

## 过孔编译修正

对照中发现规则编译器无条件使用 `钻孔 + 0.4 mm` 的直径下限，导致原工程合法的 Default 0.6/0.4 mm 过孔被扩大为 0.8/0.4 mm。修复后使用原偏好、声明最小直径和 `有效钻孔 + 2 × 声明最小孔环` 的最大值；POWER 的 0.8 mm 原偏好仍保留。声明缺失时保留保守 fallback，非法数值拒绝编译。

冻结输入及制造下限均未修改。`system-via-corrected` 从冻结工程重新导入，使用 `toolchain.legacy-baseline.json` 的 1.9.0、无扇出、30 轮配置。任务 `job-fb23206d223649e9` 自然完成布线及后优化，版本 `r-559ebda35a154642` 为 2667 段线、173 过孔，DRC 8 未连接、0 其他错误、47 警告；ERC 0 错误、16 警告，几何和持久化线宽检查通过。没有优于先前 7 未连接基线，不发布制造包。证据在 `docs/validation/system-via-corrected.json`。

真实 0.6/0.4 与 0.8/0.4 mm 混合过孔回导的独立证明保存在 `docs/validation/native-v2/via-floor/`，它验证编译修正的正确性，不证明大板布通。

## 制造样板输入修复

旧 `routing-demo` 没有启用锡膏层，SMD 焊盘也没有锡膏定义。坐标修复后，1.9.0 对照仍有 1 处 GND 未连接；2.4.1 对照通过 ERC/DRC，但因缺少所需锡膏 Gerber 被制造门禁拒绝。两次失败分别保留在 `mcp-native-grid-release.json`、`mcp-unified-grid-release.json`，没有改成通过记录。

另建 `examples/manufacturing-demo`，明确增加前/后锡膏层及前面 SMD 锡膏开口，同时更新对应本地封装库。AST 差异测试确认除此之外没有修改电气、位置或规则。该新输入的完整 MCP 制造链路已通过，不能说旧输入无需修复就通过。

## 复杂板下一步

多轮引擎、扇出和过孔对照未改善到零未连接，本轮停止无依据重复重跑。原生诊断见 `docs/validation/remaining-connectivity-diagnosis.md`：最佳版本中三个信号网完全未布，另有孤立地引脚及电源岛。后续需要实现并验证 QFP 逃线和局部重布、引入明确电源平面策略或经批准调整布局；当前没有这些能力的完整证明，不通过放宽规则掩盖缺口。

## 桌面正式部署验证

交付目录为本机历史目录（公开版本已脱敏），旧 `pcb-weaver` 未覆盖。停止开发工作台后完整复制数据，复制前后 SQLite 文件 SHA-256 相同；新工作台启动后核对了 15 个工程历史和 36 个已封存版本，均通过完整性校验。历史日志中的原绝对路径不改写，工作台按当前目录和账本哈希定位文件。

桌面隔离环境的真实 MCP 常驻队列任务 `job-7592b5f2975d4a7e` 已完成，工程 `desktop-manufacturing-acceptance`、版本 `r-cc554050a60446bf`：DRC 0 错误/0 未连接/0 警告，ERC 0 错误/0 警告，制造发布及归档校验通过。证据为 `docs/validation/desktop-manufacturing-acceptance.json`；制造包在桌面工程的 `data/projects/desktop-manufacturing-acceptance/releases/`。该桌面新作业不在开发目录的历史数据副本中。

搬迁后的 160 器件真实板图在桌面/手机尺寸均通过像素、图层、缩放、DRC 定位、深链接及状态竞态检查，见 `docs/validation/desktop-platform-final/`。Skill 与插件官方结构验证通过。当前工作台为 `http://127.0.0.1:8765`，只监听本机。

布线器的新旧执行路径存在公开的质量回归讨论，因此正在增加版本隔离的备用后端并用相同输入作对照，而不是修改设计规则以换取通过。[维护者讨论](https://github.com/freerouting/freerouting/discussions/508)

## 门禁及范围

不可行固定件关系、未布线发布、显式关键网络、规则降级、验证证据跨版本重放、输入排队期间变化、报告及制造包篡改、跨站请求、并发领取和中断恢复都有针对性测试。被跳过的原生测试必须另列实机记录，不能算作已通过。

直接导入已布线板时，也重新检查项目保留的逐网络下限及旧格式下限；证据带 PCB、工程文件、revision 摘要，缺失、非有限值、非纳米网格值或低于下限均阻断。原生 DRC 的 netclass 首选线宽不是最小线宽的替代品。

服务不向板厂下单，不自动删除既有铜线，不代替硬件工程师批准机械条件。0.2 是本地单用户工程平台，未提供企业身份、权限隔离、多租户、高可用与审计签名。

请保持工作台或独立 Worker 运行。MCP 会话退出不再停止这些独立执行进程。强制终止 Worker 本身不是阶段边界取消；跨 Windows/WSL 的原生子进程可能继续运行到工具链超时。新工作进程会标记遗留作业为 interrupted，而不会把未封存的暂存文件当成成功版本。当前没有实现跨主机故障恢复或孤儿进程即时回收保证。

即使软件门禁通过，也不表示功能、SI/PI、回流、阻抗、时序、热、EMC、安规、供货、装配旋转或实板良率通过。高速关键网络签核、任意板框、自动局部 ECO 拆线重布和物理样机验证仍不在已验证范围内。

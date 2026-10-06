# PCB Weaver

面向 KiCad 的约束驱动布局、自动布线编排、ECO 改版分析和制造证据管理项目。包含可运行的 Python 工程服务、官方 SDK 实现的 MCP、Codex Skill、命令行、测试、示例工程和 HTML 审阅报告。

这是 v0.4 局部铜线修复版及后续自动修复增量：在工程台账、持久化长任务和制造证据门禁上，增加按网络/矩形区域限定的补线、显式拆线重布及原生前后对照。已有整板与局部重布编排使用 Freerouting；新增自动断线修复使用受限几何网格候选、原始 AST 合并和 KiCad 全板复验，不声称顶尖工业布线算法。尚未经过实板、量产或工业认证。历史范围见 [系统方案](docs/SYSTEM_PLAN.zh-CN.md)，接口见 [集成说明](docs/INTEGRATION.zh-CN.md)。

主项目代码采用 [Apache-2.0](LICENSE)；KiCad 衍生示例、第三方封装和图标沿用各自许可证，见[第三方来源](THIRD_PARTY.md)。公开源码不包含历史原生验收存档，以下验收数字仅是本机阶段记录，克隆仓库后必须独立复验，不能直接用于制造放行。

**此前增量验收：160 器件四层板 `completion-system-review/r-08ee661571804502` 已通过无参考局部多网络重布验收。真实 MCP 从原始缺 3 处连接的版本开始，连续修复至 0；独立原生 DRC/未连接/ERC 错误均为 0，原有 53/16 项警告保留。未读取历史通过板铜线作为答案，规则和固定器件未改变。这不代表任意复杂板从零全自动布局布线或制造放行。** 详见[无参考重布验收](docs/AUTONOMOUS-REPAIR-VALIDATION.md)，历史记录见[复杂板修复](docs/CONNECTION-FINISH-VALIDATION.md)。

新增[自动修复验收](docs/AUTO-REPAIR-VALIDATION.md)：只需工程和版本即可从网页或 `submit_pcb_auto_repair` 提交任务，自主诊断断线、生成受限候选、原生复验并保留最后合格版本。全量回归 1451 项通过、26 项跳过；五项原生场景按预期通过，包括三项修复成功、一项无需修改、一项正确停止待审。MCP 断开重连和三种屏宽交互另行验收。缩颈及局部扇出默认关闭，必须显式启用；本轮原生矩阵验证默认补线路径，不覆盖所有可选策略。

新增[未布线工程自动完成链路](docs/COMPLETION-VALIDATION.md)：`submit_pcb_completion` 将输入复验、不同布局候选、整板布线、原生检查和受限修复串为一个持久化任务。42 器件 CPLD 板和 64 器件编程器板已通过真实桌面 MCP 验收，原生未连接、DRC 错误和 ERC 错误均为 0；警告分别为 8 和 2。四层板早期授权修复从 5 项降到 3 项的阻断记录保留在[授权修复验收](docs/AUTHORIZED-REPAIR-VALIDATION.md)。**在该早期验收时点，没有已通过参考版时，四层复杂板从零布线的全自动完成尚未全部验收通过。** 当前 [v4 固定布局验收](docs/FIXED-WHOLEBOARD-VALIDATION.md)已完成三种电路各两次、原生 6/6 passed；此结论限定于该阶段的固定布局范围。

上一轮[参考版引导的局部多网络重布](docs/MULTINET-REPAIR-VALIDATION.md)：通过真实 MCP 持久化任务，对上述四层板完成三个局部铜线补丁，未连接 3 → 2 → 1 → 0。版本 `completion-system-review/r-2356718636b34fc3` 独立原生 DRC/未连接/ERC 错误均为 0，53/16 项原有警告保留。这是依赖兼容历史通过版的局部 ECO 修复验收，保留作对照，不是本轮无参考验收的输入。

本轮软件回归 1555 项通过、26 项跳过；三种屏宽网页交互、真实原生复验和 MCP 断线续查已通过。网页自动修复窗口可显式启用“无参考多网络拆线重布”并设置面积和求解时限；Skill 使用 `submit_pcb_auto_repair` 的 `options.allow_multinet: true`。本例使用 24 次尝试、1800 秒调度预算、2500 mm2 面积上限、180 秒单次时限，并另行授权短缩颈与局部调整。桌面部署哈希清单见[同步核验](docs/validation/autonomous-deployment.json)。

历史 v0.3 软件回归 922 项通过、24 项跳过；桌面部署的真实 MCP、HTTP、原生小板制造及桌面/手机 UI 另行通过。[完整验收记录](docs/V3_VALIDATION.zh-CN.md)明确列出仍未验证的工业范围。

历史 v0.4 初始软件回归 1277 项通过、26 项跳过；新增真实局部拆线重布小板正例、MCP 断线续查及桌面/手机修复入口。当时复杂板两次有时限的路由均被阻断。[局部修复验收](docs/REPAIR_VALIDATION.zh-CN.md)保留当时能力、保护门禁和失败证据，不代表当前最佳版本。

v0.1 的四器件样板保留作回归；含本机路径的历史审阅报告不随公开源码发布，也不代表系统级能力。本版引入具有来源记录的真实 MCU/CPLD 控制板：160 器件、825 个物理焊盘、278 网络、四层铜，包含电源、CAN、RS232、连接器和背面器件。

固定布局整板完成的 **v4 原生验收已 6/6 passed，当前阶段最终门禁已通过**。[真实证据汇总](docs/validation/fixed-v4-matrix/summary.md)及 [matrix.json](docs/validation/fixed-v4-matrix/matrix.json)校核通过：同一冻结核心/profile、独立 D 盘工作区、`benchmarks/fixed-completion-options-v4.json` 下，system（源码和桌面各一次）、CPLD、编程器三种电路各 2/2 passed；两份 system 会话 `49479`/`12534` 均 exit 0。六次均为 0 DRC/ERC 错误、0 未连接、0 新增 findings、0 器件移动，保留已有告警。具体版本、走线/过孔、长度、耗时及告警见[最终原生指标表](docs/FIXED-WHOLEBOARD-VALIDATION.md#native-results)。汇总脚本校核既有原生证据，并未再次调用原生工具。v3 的五次通过、桌面 ENOSPC 中断和六项 `via_dangling` 阻断均保留为历史，不计入 v4。

v4 **三份发布及真实 MCP QA 3/3 passed**，会话 `29487` exit 0，[证据](docs/validation/fixed-v4-three-published-mcp.json)包含普通项目 `fixed-cpld-v4-review/r-8c9904f6ca1b40a2`、`fixed-programmer-v4-review/r-409bce309a1d4934` 和 `fixed-system-v4-review/r-33f712f4dade4c53`。system 发布会话 `43003` 亦 exit 0。单项 helper 的 `wholeboard_phase: not_evaluated` 表示该检查不负责整阶段判定，不是失败，也不覆盖独立 native 6/6 汇总。

v4 **真实 UI 9/9 视口通过**：[system](docs/validation/fixed-system-v4-ui-final/results.json)、[CPLD](docs/validation/fixed-cpld-v4-ui-final/results.json)、[编程器](docs/validation/fixed-programmer-v4-ui-final/results.json)均完成 desktop/mobile/narrow，canvas 分别为 4273/1593/769、1823/1036/656、1565/933/611；每个视口均无页面错误、拦截 7 次提交。system UI 对应原生版本 `r-6b982b62c2924bbb`，不同于普通项目的发布副本。主验收方已检查 system desktop actual-job 和 320px narrow-passed、CPLD narrow、编程器 mobile 真实截图。CPLD 原 30 秒超时及断言未改，早先失败证据继续保留，详见[事件记录](docs/validation/fixed-v4-ui-load-incident.md)。

v4 [真实过孔清理诊断](docs/validation/fixed-v4-via-cleanup-diagnostic.json)从旧 v3 失败检查点得到 `improved` 版本 `r-9eec623326fa4055`：清理阶段删除 6 个过孔，DRC 警告 53 → 47，8 项未连接按网络不增，其他 DRC 错误/ERC 错误为 0，非布线 AST 和保留连接分区完整保留。这不是从零布线的整板通过。通用 publisher 的 ERC 结构等价校验及三份真实发布结果的 MCP 检查已通过，提供 schema 3 证明、不使用项目告警白名单；发布通过也不等于整板阶段通过。

v4 [全量回归](docs/validation/fixed-wholeboard-v4-regression.xml)已完成：2007 项中 **1981 passed、26 skipped、0 failure、0 error**，测试摘要耗时 352.45 秒（XML suite 计时 352.414 秒），不能表述为全部 2007 项执行。[部署预检](docs/validation/fixed-v4-deployment-preflight.json)及[最终部署报告](docs/validation/fixed-v4-deployment-final.json)均为 225 文件 passed，最终部署实际 exit 0。正常 8765 发布 system 页的只读 Chrome 截图检查亦 exit 0、无 JS 错误，[截图证据](docs/validation/fixed-system-v4-published-desktop.png)已保留。**当前 v4 固定布局阶段的原生、UI、发布、MCP 和部署门禁均通过**，不外推至其他设计或阶段，也不授权制造。最终部署报告记录所校验文件的哈希；本次终稿与桌面的一致性以该报告为准。

历史 v3 全套软件回归使用 D 盘临时目录[重跑完成](docs/validation/fixed-wholeboard-v3-regression-d-final.xml)：1909 项中 **1883 passed、26 skipped、0 failure、0 error**，耗时 357.808 秒。26 项跳过包括 22 项 opt-in/原生命令缺配置和 4 项 symlink 权限限制，不能表述为 1909 项全部执行。[此前报告](docs/validation/fixed-wholeboard-v3-regression-final.xml)的 1879 passed、4 failure 原样保留，四项失败均为 ENOSPC；[11 项 placement 测试](docs/validation/fixed-v3-placement-d-temp-3.xml)未改代码即在 D 盘通过。这些记录不替代 v4 回归或原生验收。

## 工程链路

1. 导入 `.kicad_pcb` 和同目录项目文件、根原理图、子页、本地库。
2. 将连接器固定、区域、邻近关系、最小间距、线宽、孔径等要求写入结构化约束。
3. 固化工程快照及 SHA-256，把制造下限编译到 KiCad 工程规则。
4. 小板使用 SLSQP，大板使用分块坐标优化，生成多套布局，按真实焊盘位置计算 HPWL、间距、重叠面积；可行性优先，不能把估计线长当成完成走线。
5. 应用可行布局到子版本；通过 KiCad 原生接口导出 DSN，运行本地 Freerouting，再导入 SES。
6. 对准确版本执行 KiCad ERC/DRC、原理图与 PCB 一致性和几何约束复验。
7. 检查通过后导出 Gerber、钻孔、贴装位置、原理图 BOM 和证据清单，打包并校验。
8. 后续修改作为 ECO 子版本导入，比较器件、焊盘连接、网络和项目文件变化。

## 这版的改进点

- 台账逐项列出位号、标称值、封装、层、坐标、角度、焊盘、网络和铜线几何；支持搜索、分页、排序、画布定位和完整引脚详情。
- 制造商、料号、数据手册只显示源文件已有字段；基于位号推断的类别明确标识，不能当作经过验证的元件选型。
- 版本绑定的 JSON、四类 CSV 和带 SHA-256 清单的工程交换 ZIP；未知弧长和部分线长保留状态，不冒充完整长度。
- 可选本机 REST 接口提供 Bearer、项目白名单、默认只读、幂等任务提交及 OpenAPI 3.1.1。没有已认证企业连接器，不等于任意 PLM/ERP/MES 即插即用。
- 把设计意图、数值优化、原生 EDA 验证连接成一条可复现的业务链路。
- 布局以明确约束和数值指标比较；模型不能用文字结论伪造布线完成。
- 三种布局模式分别处理小板优化、大板分块优化和最小位移合法化；选择依据与候选排序一致。
- 保留不同布线器版本的独立配置与真实证据；首选线宽和强制最小线宽分开处理，局部缩颈仍受逐段检查约束。
- 计划、原理图、PCB、规则、检查和制造包绑定同一个工程版本。
- 原生 DRC 问题清单支持点击定位实际引脚或走线坐标；原报告及板文件哈希不符时不显示可信定位结果。
- 局部修复先诊断真实未连接网络，再限定区域和允许拆除的 UUID；原板保持不变，失败候选保留，只有未连接数严格下降且没有其他检查退步才标为改进。
- 自动修复从原生未连接项推导目标与搜索范围，限制尝试次数、候选进程时限和区域面积；原生检查不合格的候选不会替代最后合格版本。
- 完全未布线的工程可提交一次自动完成任务，尝试去重后的可行布局并保留最佳已检查子版本；只有原生门禁真正通过才返回完成。布局间距变体保持固定器件和角度，整板路由独立禁用隐式缩颈，不修改已保存的工程下限。
- ECO 分析定位变更网络；旧检查不能用于新板的放行。
- 通过真实 MCP 协议调用服务，同时保留 CLI 便于复现和 CI。

多层布局、队列、工作台与验收分层见[系统方案](docs/SYSTEM_PLAN.zh-CN.md)；[原业务架构](docs/ARCHITECTURE.zh-CN.md)记录版本、验证和发布设计的起点。

这些是本项目的工程设计，不声称全球首创、新布线算法或专利新颖性。[源码调研](docs/RESEARCH.zh-CN.md)列出实际阅读范围及上游已有能力。

## 当前支持范围

- 约束模型支持 2/4/6/8 铜层；矩形板框，保持已有器件角度和正反面。复杂 courtyard 使用保守包络，不支持任意板框及三维机械干涉签核。
- 背面焊盘变换、通孔占位、椭圆槽孔与多种封装几何；真实四层控制板与更高层数适配测试分别记录，不能由 Schema 枚举推导出量产能力。
- 已有原理图、封装、明确网络和约束的设计。
- 无现有走线的板可调用整板自动布线。已有铜和声明为 critical 的网络会触发保护性阻断。
- 基于焊盘网络的 ECO 影响分析；支持显式范围内的局部拆线重布，以及受限的自动断线修复。可选局部扇出只搜索少量邻近端点，不支持任意阻挡物重布、任意板框、铜区/弧或高速关键网络修复。
- 布局 HPWL 是连线长度估计，不是已完成铜线长度或信号质量指标。
- 按具体网络派生 netclass，保留原有有效规则并核对 KiCad 原生加载值；不做阻抗合成和差分对调优。
- 本地单用户工作台，非公网多租户系统；任务可在阶段边界取消，进程中断不会自动重放修改。

未覆盖：高速 DDR/PCIe、射频、阻抗/时序、电源完整性、热、EMC、绝缘/安规、自动选型采购、元器件库存、产线旋转校正、实板功能测试。`max_voltage` 只是声明，不代表电气安全校验。

## 本机使用

项目目录的 `.venv` 是隔离 Python 环境。首次部署执行：

```powershell
./Setup.ps1
./Start-Platform.ps1
./Run-Demo.ps1
./Run-Demo.ps1 -Route
```

`Start-Platform.ps1` 启动工作台，默认地址 `http://127.0.0.1:8765`；端口已占用时传 `-Port 8766`。可浏览工程、导入 KiCad 文件、生成候选、提交流水线、检查任务证据、比较 ECO、下载已验证制造包。长任务在后台执行，不伪造进度百分比。

桌面交付和上述启动脚本默认使用已通过小板制造链路的 `toolchain.unified.json`；传 `-Config toolchain.legacy-baseline.json` 可选旧版对照。该选择不代表复杂板已布通。完整正向回归使用 `examples/manufacturing-demo`；旧 `routing-demo` 缺锡膏定义，保留作失败回归。

`Run-Demo` 执行四器件快速回归；加 `-Route` 才尝试完整工具链。缺依赖或检查失败会返回 `blocked`，不会生成模拟制造结果。系统控制板使用 `profiles/system-controller.json` 执行配置；它与冻结基准的机械假设差异见 `profiles/README.md`，不能用修改基准隐藏失败。

本机测试配置为 `toolchain.wsl.json`，通过 Windows Python 调用已有 Ubuntu 中的 KiCad 和 Java。原生 Windows KiCad 可使用 `toolchain.local.json` 并配置 `kicad_cli`、包含 `pcbnew` 的 `kicad_python` 和 Java 路径。兼容性基于运行时探测；未经本机实测的版本不保证支持。

Freerouting JAR 独立下载于官方 release，见 `tools/README.md`。项目服务不需要额外 LLM API Key：模型推理来自连接它的 MCP 客户端；服务自身不调用模型 API。

当前 WSL 配置选用经过对照试验的 Freerouting 1.9.0，需完整 Java 21 JRE、Xvfb 和 xauth，在虚拟显示中运行；保留 2.0.1 JAR 及失败记录。版本切换不是对所有板布通的承诺。新机器需另外安装 KiCad 原生 Python/CLI 和上述工具，`Setup.ps1` 只负责项目 Python 依赖。

另提供 `toolchain.legacy-baseline.json` 的无扇出对照，以及 `toolchain.unified.json` 的隔离 Java 25 / Freerouting 2.4.1 路线。后者通过独立小板制造回归，因此选为新安装默认；其大板对照仍超时，不能推断系统级布通能力。每轮实际输入、引擎和失败原因见系统验证记录。

## MCP 与 Skill

Altium 本机开发者内测服务已独立提供持久化 worker 与 stdio MCP；启动方式、任务状态和明确的未验收边界见[中文使用说明](docs/ALTIUM-SERVICE-BETA.zh-CN.md)。它不替代以下面向 KiCad 的主 MCP。
准备公开源码仓库前，请先阅读[GitHub 开发者内测版发布准备](docs/GITHUB-RELEASE.zh-CN.md)。本机配置、运行数据和大体积验收存档默认不进入源码仓库；下文历史验收链接在公开仓库中可能没有对应文件。

`Setup.ps1` 生成本安装目录的绝对路径 `.mcp.json`。包含 24 个工程工具、约束 Schema / 集成 OpenAPI 资源和业务流程 Prompt；`.codex-plugin/plugin.json` 与 `skills/pcb-engineering/` 提供插件和 Skill 包装。

`inspect_pcb_inventory` 按 `summary/components/nets/tracks/vias/layers` 读取版本台账，每页最多 500 条；应迭代至 `total`，不能把第一页当作全部设计。网络存在或具有铜线不代表已连通，放行仍依据原生检查。

`diagnose_pcb_repair` 读取已验证报告，返回网络、真实问题对象与待确认的建议范围。`submit_pcb_repair` 提交持久化任务，参数为 `project/revision/nets/region/remove_ids/passes`；region 为 mm 单位的 `[xmin,ymin,xmax,ymax]`，默认不拆线，最多 10 轮、单次路由最多 300 秒。`improved` 仍可能未布通，`repaired` 也不代表制造授权。企业机器桥接接口本版不开放修复，只允许受信任本机工作台或 MCP 提交。

`submit_pcb_completion(project, revision)` 只接受支持范围内的未布线工程，默认最多 3 个布局候选、20 个路由轮次、1800 秒阶段调度预算。`placement_spread_mm` 默认 0.5、范围 0-2，设为 0 可关闭布局间距变体。活动原生阶段另受工具链超时限制，不承诺总耗时必定小于调度预算。查询 `get_engineering_job` 的 `result.steps.complete` 查看候选、检查、线长、过孔、位移及最终版本；部分合格检查点仍为待审，不自动制造放行。输入准备和机械审查不计入运行期人工布线介入次数。

默认 `routing_policy: strict` 禁用布线器隐式缩颈。显式选择 `normalize_widths` 可尝试回导前按声明下限加宽，并对新生成铜线执行至多 6 次、每次最多 100 mm² 的局部间距修复；必须通过全板原生复验才能采用。这不启用补线器的可选短缩颈或扇出权限，也不修改规则、固定器件或既有铜线输入。

固定布局完成显式选择 `placement_mode: preserve`，默认仍为 `optimize`。`repair_cycles` 为 1–3 的严格整数，默认 1；大于 1 仅允许 preserve，默认请求省略此字段，保留旧哈希。v4 沿用 v3 的 3 轮及 7200 秒外层完成预算；每轮仍最多 24 次尝试、1800 秒，三轮最多 72 次，均受同一外层截止时间限制。启用 `repair.allow_multinet` 时，每轮先执行最多 4 次/300 秒的非多网络阶段，计入该轮限额，剩余额度用于后续修复；整轮未保留原生检查接受且未连接数严格减少的版本，就停止续轮。独立 AutoRepair 默认值和上限不变。首轮保留 `attempt.additive_repair`/`repair`，额外轮次写入 `attempt.additional_repair_cycles`；网页分轮显示并只计数一次。完整非布线 AST、配套文件和约束保留，仅排除顶层 `segment`、`via` 及 `generator`/`generator_version` 标签。

v4 仅在原始输入零走线/零过孔、`preserve` 且 `normalize_widths` 时，执行一次严格冗余过孔检查。只清理经证明可删的新悬空过孔；不支持的过孔仍拒绝，不忽略告警。`attempt.via_cleanup` 的 `improved` 只表示清理阶段采用，后续整板 assessment 仍可拒绝；删除数量依据已接受结果的 `proof.removed_ids`，不能把计划列表或清理阶段版本视为最终整板结果。

复杂板建议调用 `submit_engineering_job`，再通过 `get_engineering_job` / `list_engineering_jobs` 读取阶段证据；`cancel_engineering_job` 请求在阶段边界停止。MCP 与网页共享版本门禁和持久化队列，不是两个独立的演示实现。

长任务由独立常驻工作进程执行，不依附于 MCP 会话。先运行 `Start-Platform.ps1`，或者只运行无界面的 `Start-Worker.ps1`。MCP 断开不会关闭该工作进程；没有启动工作进程时，提交的任务保持排队。短程 MCP 会话不会在退出时终止它之后的其他任务。

v4 使用新的独立 D 盘工作区，通过 `--workspace` 显式指定；不传 `--external-worker` 时由 harness 管理独占工作进程，传入时须与已运行的外部 worker 使用同一隔离工作区及配置。源码与桌面系统各 `--repeats 1`，源码小板各 `--repeats 2`，不得复用 v3 运行或覆盖历史证据。同一核心/profile 冻结后执行，命令模板及待验收矩阵见[固定布局验收](docs/FIXED-WHOLEBOARD-VALIDATION.md)。

每个新任务保存提交配置、解析后的执行路径及可访问 JAR 的哈希。工作进程使用这份配置，不能因为由另一个客户端领取而切换引擎。任务列表返回轻量摘要；完整原生日志和步骤保留在任务详情中。缺失旧版运行配置快照的排队任务需要明确重新提交。

可把 `.mcp.json` 的服务器条目注册到支持 stdio 的客户端。Codex CLI 的标准 MCP 注册方式是 `codex mcp add`，也可配置 `config.toml`；详见[官方文档](https://developers.openai.com/codex/mcp/)。本项目不会在启动时改写全局客户端配置。安装文件夹移动后重新运行 `scripts/configure_client.py`，再更新客户端注册。

建议的第一条请求：

> 使用 pcb-engineering，导入我的 KiCad 工程，保持 J1、J2 位置不动，给出三套满足间距要求的布局，执行可用的自动布线和工程检查，并生成审阅报告。只在检查通过后生成制造包。

## CLI

```powershell
.venv/Scripts/pcb-weaver.exe --workspace data --config toolchain.wsl.json doctor
.venv/Scripts/pcb-weaver.exe --workspace data import myboard C:/boards/myboard/myboard.kicad_pcb --constraints constraints.json
.venv/Scripts/pcb-weaver.exe --workspace data plan myboard REVISION_ID
.venv/Scripts/pcb-weaver.exe --workspace data apply myboard REVISION_ID PLAN_ID CANDIDATE_ID
.venv/Scripts/pcb-weaver.exe --workspace data --config toolchain.wsl.json route myboard NEW_REVISION_ID
.venv/Scripts/pcb-weaver.exe --workspace data --config toolchain.wsl.json verify myboard ROUTED_REVISION_ID
.venv/Scripts/pcb-weaver.exe --workspace data --config toolchain.wsl.json release myboard ROUTED_REVISION_ID
.venv/Scripts/pcb-weaver.exe --workspace data report myboard ROUTED_REVISION_ID
```

每个修改工具会返回新 revision ID，后续使用新 ID。命令输出 JSON，失败/阻断使用非零退出码。静态报表可直接打开；交互工作台需启动本地服务。

## 目录

| 路径 | 作用 |
|---|---|
| `src/pcb_weaver/` | 解析、优化、工具适配、版本、验证、制造、MCP、CLI |
| `skills/pcb-engineering/` | 面向模型的工程决策说明与边界 |
| `examples/` | 原创回归小板，以及有 CC-BY-SA 来源记录的复杂 KiCad 衍生基准 |
| `profiles/` | 与冻结基准分离的明确执行约束 |
| `benchmarks/` | 上游选择、输入冻结、原生基线与规模统计 |
| `tests/` | 几何、协议、门禁、完整性及工具链测试 |
| `docs/` | 源码研究、架构约定、实际验收记录 |
| `data/projects/` | 本机工程版本、检查证据、审阅报告、制造包 |
| `data/jobs/` | 持久化任务各阶段结果；真实运行日志保存在对应版本 |

## 验证与可追溯性

```powershell
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe scripts/accept_system_mcp.py --project my-system-acceptance --passes 30
```

例板、工程服务测试和真实工具链测试分开记录。模拟执行器只用于回归测试，不能作为 KiCad 实机验收证据。SQLite hash chain 便于检测意外修改，但不是外部锚定或签名的合规审计日志。

系统验收脚本会实际导入基准、布局、布线、验证并尝试生成制造包，可能运行较久；可通过 `--board` 和 `--constraints` 指定自己的工程。它只在整条流水线及归档校验通过时记录 `passed`，失败记录也会保存。

客户端单次 MCP 调用设置 60 秒超时，工程任务的运行时限由工具链另行控制。客户端断线后，可使用 `--project 原工程ID --resume-job 原任务ID` 重新查询，不重复提交；恢复记录另存，不覆盖失败证据。工程子进程使用空标准输入，不能读取 MCP 协议通道。

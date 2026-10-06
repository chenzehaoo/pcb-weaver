# PCB Weaver：源码调研、需求边界与独立审查依据

研究日期：2026-09-06 至 2026-09-07，Asia/Shanghai。

## 1. 结论与证据边界

**仅把 KiCad 查询、移动器件、DRC、Gerber 导出或 Freerouting 包成 MCP，再附一份操作 Skill，已经不足以构成本项目的技术差异。** Konnect 已经组合这些能力，并提供布局算法、设计审查、制造工作流 Skill 和带版本校验的路由往返。NiRuLabs 也不只是查询接口，源码已有 Push & Shove 调用。Freerouting 官方仓库直接提供 MCP 桥接、作业状态机、文件传输和 DRC 接口。[K1][K2][K5][K8][N2][N3][F1][F2][F7]

建议本项目聚焦：**ECO 变更影响证据 + 显式约束的候选布局与复验 + 绑定完整设计版本的制造发布门禁**。差异必须由失败用例、真实工具调用及可核对产物证明，不能靠工具数量、自然语言描述或“用了优化器”证明。

这里的“创新”是本项目的独立设计与工程差异，**不是世界首创、专利新颖性或全行业无人实现的声明**。本次只比较指定项目的选定源码，不覆盖商业 EDA、PLM、DFM 平台或全部开源项目。

研究方法和限制：

- Konnect：本地 `work/research-konnect`，提交 `1401e9ab78fd915dc5f06b7a8865f909d5ce536b`，只读检查时工作树干净。
- NiRuLabs：本地 `work/research-nirulabs`，提交 `2fd14d32fb2532071649cf6779b5e97b524e0a1b`，只读检查时工作树干净。
- Freerouting：通过官方 GitHub 原始源码读取；用 `git ls-remote` 固定 `master` 为 `a11c0a42d1b3827e5126429c5c9820c4ab5bec7c`，核心实现随后按该 SHA 读取；没有在本任务中克隆它。
- 早期浏览过默认分支 README、工具目录和原始文件，发现网页缓存与当前源码不同，因此下文关键判断优先使用固定提交源码。Freerouting MCP 文档和 package.json 的早期阅读来自当时 `master`，不把它们当作独立运行验证。
- 这是定向静态阅读，没有完整读完任何大型仓库；未运行上游测试、KiCad、PCB 修改、路由、制造导出或实物打样。读取测试正文只说明测试存在，不说明本次测试通过。
- 本节研究结论形成于实现初期。后续实现与独立审查的实际结果另记于 `VALIDATION.zh-CN.md`；下文需求和验收条件不等于功能已完成。
- 上游仓库用于只读研究；本项目在独立目录实现，没有复制这些仓库的业务代码。

## 2. 三个项目的真实流程

### 2.1 Konnect：已有完整工具工作流，不能当作薄封装

**总体路径。** 官方说明将原理图文件编辑、PCB IPC 操作、`kicad-cli` 检查/导出与 MCP 分开。其制造 Skill 明确要求订单约束来源、直接 DRC、实际产物清单、Gerber/钻孔检查和 BOM/CPL 检查。因此“Skill + MCP + 制造导出”本身已存在。[K1][K8]

**原理图到 PCB 同步。** `handle_update_pcb_from_schematic` 读取保存的层次原理图，调用 KiCad 导出扁平网表，取得目标 PCB 的 live snapshot，生成 `SyncPlan`。计划包含新增/更新、焊盘网络调整、保留板上独有器件、冲突和 `plan_revision`。默认 dry-run；apply 必须提供 `expected_plan_revision`，现场重新生成的版本不匹配则拒绝。修改经 IPC commit 提交，并读取实际板内容核对。[K3]

已有的变更保护很具体：封装标识改变产生 `footprint_id_changed`；焊盘旧网络或新网络已有铜对象时产生 `routed_pad_net_change`，整个计划转为冲突并清空待写变更。更新保留位置、角度、层和锁定信息。**不能宣称 Konnect 没有变更识别、影响保护或过期计划校验。** 但该同步计划主要回答“当前保存的原理图怎样同步到当前板”，本次所读函数没有建立跨历史版本的器件、规则、验证证据、采购和制造文件依赖图。[K3]

**布局。** `placement.rs` 已有评分、去耦电容放置、BGA fanout、初始自动布局和力导向细化。初始布局按共享网络做 union-find 聚类；细化建立器件弹簧关系，再用排斥、边界和网格碰撞修正。它读取 KiCad 的锁定状态，也接受调用方指定锁定器件，返回前后评分和 dry-run 计划。[K2]

这里值得区分“约束”与“启发式”：`net_weight` 按网络名字后缀 `_P`/`_N` 给权重 5，常见电源名字给权重 3；这不是从差分协议、阻抗、时序或实际电源回路模型推导的约束。评分使用 courtyard/板框包围盒等几何量。本项目不能把“加权连线长度 + 防重叠 + 锁定”重新命名为独创约束布局；可做的差异是可追溯、带单位和适用范围的项目约束，以及对最终文件独立复验。[K2]

**Freerouting 往返。** 官方工具流程是 `export_specctra_dsn -> route_specctra_dsn -> plan_specctra_ses_import / apply_specctra_ses`。`freerouting_mcp.rs` 确实启动本地 Java MCP 子进程，检查必需工具及 schema，创建 session/job，上传 DSN，设置参数，启动、轮询并下载 SES；对超时、输出为空及非 `session` 格式做处理。[K1][K5]

`specctra_ses.rs` 校验 reverse manifest、目标板路径、源 SHA-256、支持的板型、网络/层映射、放置不变性及路由图元。`pcb_routing.rs` 在写入前再次比较 live board，创建路由项并核对返回 ID/回读数量，生成候选板、运行 DRC；操作错误时用补偿事务删除本次新增项并核对原快照。[K4][K6]

一个重要细节：工具说明写着在 commit 前检查，但所读实际实现解释了 KiCad 10 的可见性限制，采用**先结束写入事务，再回读/生成候选/DRC，失败时补偿**。所读导入分支收集 DRC 数量，并未因“DRC 数量大于零”直接判该导入操作失败。因此“SES 导入成功”不等于“制造发布通过”。[K6]

**制造与门禁。** `manufacturing.rs` 分别实现检查与导出。检查调用真实 `cli::run_drc`，DRC 执行失败、必要类别缺失或错误会阻止 READY；导出串联 Gerber、钻孔、位置和可选 BOM，非空文件验证后的路径才进入清单，有 warning 返回不完整。`cli.rs` 的 Gerber 导出使用 staging 目录并核对文件数量和非空性。[K7][K10]

其范围也有明确边界：厂商检查中 `_min_drill`、`_max_layers` 虽被赋值，却未在该分支用于验收；不能把内置表当作完整厂商能力合同。导出 handler 没有要求先提供与当前输入绑定的验收凭证，返回结构也不是覆盖设计、约束、工具版本、报告及全部产物哈希的发布记录。这里是本项目可重点验证的组合差异，不是“上游没有制造检查”。[K7]

**不要把基础模块误认成已接通的发布系统。** `gates.rs` 已实现 PASS/WARN/FAIL/BLOCKED/EMPTY 及组合顺序，`design_hash.rs` 已实现设计文件集合的规范化哈希并包含单元测试。但在该提交的 `crates/**/*.rs` 符号搜索中，`design_state_hash`、`combined_status`、`GateStatus` 的命中局限于定义/测试，`lib.rs` 导出模块，未发现这些公共函数被生产工具调用。`konnect-vcs/src/lib.rs` 自称 scaffold，实际读取内容是项目根仓库校验及测试。故既应承认已有思路和代码，也不能据文件名声称端到端发布审计已完成。[K11][K12][K13][K14]

### 2.2 NiRuLabs：直接 IPC 路由，README 与实现有差异

实际调用链是 `server.py:create_server -> register_tools -> tools.py:_execute_tool -> KiCadIPCClient -> protobuf ApiRequest -> pynng Req0 -> KiCad`。`_send_command` 检查响应状态并更新 token；`begin_commit/end_commit` 是 KiCad 编辑事务接口，不能当作不可变制造版本历史。[N1][N2][N3]

`route_pads` 的实现调用 `route_between_pads`，按器件位号与焊盘编号找起终点，检查网络编号，再执行 `start_route -> set_route_end -> finish_route`，失败分支可 abort。README 的“No Push & Shove”已不足以描述源码现状。但本次未验证特定 KiCad 发行版是否实现它引用的全部 protobuf 命令，不能把客户端代码存在写成工业兼容性已验证。[N0][N2][N3]

源码中 `has_violations` 为真时记录 warning，随后仍调用 `finish_route`。这说明路由动作成功需要另一个明确的工程验收阶段。根目录 `route_all_nets.py` 按网络中的焊盘顺序相邻配对，并有固定 `skip_count = 7`，是实验性批处理脚本证据，不是可重放的 ECO 路由任务管理。[N3][N4]

本次阅读中没有发现与历史设计版本绑定的 ECO 传播报告、制造发布记录和约束证据链；这仅是选定入口和调用链的观察，不是全仓库缺失证明。

### 2.3 Freerouting：官方已有 MCP，路由作业不等于制造签核

官方同时提供 Node 本地 stdio 到公共 API 的桥和本地 Java MCP 模式。Java `OpenApiMcpToolRegistry` 从 OpenAPI 的 `/v1/` 路径生成 schema，并把方法/路径映射为 `create_session`、`enqueue_job`、`start_job` 等工具名；`McpControllerV1` 处理 `tools/list`、`tools/call`，普通工具转发到配置的 REST API，返回 HTTP 状态和结构化 body。[F1][F2][F3]

真实流程：`create_session -> enqueue_job -> upload input -> settings/rules -> start_job -> poll job -> download SES`。`JobProgressResource.startJob` 检查 session 和 QUEUED 状态，再置为 READY_TO_START 交给调度器。`JobOutputResource.downloadOutput` 对进行中的部分结果返回 202、暂无结果返回 204，拒绝其代码列出的错误终态。**下载请求成功或文件存在都不能替代完成状态检查。**[F1][F6][F7]

Node `index.js` 截获本地文件上传/下载工具：本机读 DSN、Base64 编码后发往 REST，下载后解码写到本机。默认目标是公共 API，因此“本地 stdio 桥”不代表“PCB 数据留在本地”。选用本地 Java 模式是部署选择；不是重新封装一个 MCP 的创新。[F3]

此外，官方已有 `get_job_drc_report` 映射；下游 `getDrcReport` 使用 `DesignRulesChecker`，必要时从作业输入加载 DSN/KiCad JSON。不能说 Freerouting 完全没有 DRC。它检查的作业板状态与本项目最终导入、保存、生成制造文件的 KiCad 项目仍是两个证据对象，应分别记录和复验。[F2][F7]

本次跟读到 MCP、REST 作业状态与输出/DRC入口，没有深入证明路由核心算法、所有调度线程和全部几何检查正确性。另发现旧接口注释称取消后部分结果可取，而读取的输出实现拒绝 CANCELLED；本项目应按固定版本实际响应测试，不能只照文档推导错误恢复流程。[F4][F7]

## 3. 总体需求与工业链路

总体目标是：工程师导入真实 KiCad 项目后，能解释一次变更、在明确约束下生成可比较的布局候选、用真实 EDA 工具完成路由和验证，并得到能追溯到精确输入版本的制造包。

当前实施基线来自 `docs/IMPLEMENTATION_CONTRACT.md`。该契约约定 Python 3.11+、Pydantic v2、sexpdata AST、SciPy、官方 MCP SDK，独立实现，不复制研究源码。下表是需求，不是完成情况。

| 编号 | 需求与交付物 | 验收边界 |
| --- | --- | --- |
| R1 | 导入 PCB、原理图层次、项目及规则 sidecar，建立不可变 revision | 只写副本；原输入不变；缺子页、重复身份或不支持格式显式阻止相关操作 |
| R2 | 需求结构化：电气、布局、制造、发布条件 | 有 schema、单位、来源、适用对象和未知项；字段被保存不等于被执行 |
| R3 | ECO 比较及影响报告 | 给出直接变化、传播理由、受影响规则/网络/工件和需重验项；结构 diff 与工程影响分开 |
| R4 | 约束驱动候选布局 | 锁定、板框、间距、区域、邻近等硬约束独立验算；软目标仅排序可行候选 |
| R5 | 真实路由与回导 | 原生 PCB 引擎导出 DSN/导入 SES，调用现有 Freerouting；保存作业、参数和 DSN/SES 哈希；成功回导才产生子版本 |
| R6 | 原理图/PCB 一致性及真实 DRC/ERC | 解析真实 JSON 报告，区分进程成功、工程通过和未运行；未连接、parity 和缺失报告类别不可被吞掉 |
| R7 | 可追溯制造包 | Gerber、钻孔、需要时的 BOM/CPL 来自同一冻结输入集；产物真实非空、类型齐全，记录逐文件哈希 |
| R8 | 服务层强制发布门禁 | 当前完整 revision 和约束匹配新鲜证据；必要检查未完成、失败、未知或产物不完整时拒绝发布 |
| R9 | Skill + MCP + CLI 同一业务规则 | Skill 负责需求澄清、解释与流程；MCP/CLI 调同一确定性服务，绕过 Skill 也不能绕过门禁 |
| R10 | 可审计任务与失败恢复 | 每项目锁、事件与父子版本记录、超时日志；并发/崩溃不伪造完成状态，不覆盖原板 |

建议的工程路径：

```text
需求与资料 -> 工程约束 -> 导入/冻结 revision A
                            |
                 ECO(A, B) -> 影响证据与重验范围
                            |
               候选布局 -> 独立约束审计 -> revision C
                            |
              原生 DSN -> Freerouting -> SES -> 原生回导 D
                            |
               DRC/ERC/parity + 约束复验 + 人工适用项检查
                            |
            冻结制造导出 -> 工件核验 -> manifest -> 发布记录
```

所有检查绑定具体输入。ECO 的局部影响分析可以安排优先检查，但 MVP 不应据此跳过最终全板 DRC/ERC。实际采购下单、供应商接受、装配和上电测试在发布记录之外继续形成实物证据；本期不自动提交厂商订单。

契约以双层、低压配置为起点，但 `layers: 2`、`max_voltage: 24` 只是配置值，只有实际执行板型/输入范围检查才构成支持边界。电压字段不能证明耐压、绝缘安全或认证符合性。

## 4. 本项目可验证的设计差异

### 4.1 ECO 变更影响分析：从差分走到证据失效

首选优先级 P0。输入为 revision A/B、对应约束及显式 ECO 意图；输出至少分为直接变化、推导影响、未知影响与必须重验项。

- 身份匹配优先使用稳定 UUID/层次实例路径；位号可重标注、网络名可更改、数值 net code 可重排。回退匹配必须披露依据和歧义，不能静默按数组位置或位号猜测同一器件。
- 直接变化包括器件增删、位号/值/MPN/DNP、封装/焊盘、网络成员、位置/层、板框、规则。已读取的实施契约 `compare_boards` 尚不足以保证原理图、BOM 属性及规则差分都可用，需逐项实现或列为缺口。
- 推导边示例：`U1 封装变化 -> pin/pad 映射待核对 -> 所连网络及局部铜几何需复验 -> CPL/贴装检查失效`；`净距规则变化 -> DRC 证据失效`；`DNP 变化 -> BOM/CPL 一致性需重验`。
- 全项目输入摘要变化可以保守地使发布证据全部过期；影响分析再解释原因和重验优先级。不要把“整包 hash 不同”本身叫作 ECO 语义分析。
- 阻值变化对增益、负载或功耗的影响需要电路知识/模型。仅从同网连接可报告潜在影响，不能直接断言电路正确或错误；避免因为共享 GND 就把整板所有器件无解释地列为受影响。

可证伪验收：建立包含位号重标注、net code 重排、引脚换网、封装变化、DNP、仅布局变化和仅规则变化的已知答案样例。报告需对每个影响给出来源与路径；对该有限样例集统计漏报/误报，不能外推全行业准确率。

### 4.2 约束驱动布局：约束执行与结果复验分离

优先级 P1。Konnect 已有确定性启发式布局；本项目的可验证差异应是“项目约束可输入、可解释、可执行、可复验”。[K2]

- MVP 覆盖显式固定器件、边缘退让、器件间距、区域归属、邻近距离和关键网络权重；每条规则需要稳定 ID，测量值、阈值、单位和目标对象。
- `feasible` 必须来自硬约束审计；优化器 `success`、低目标值和更短估计连线不能替代它。无法找到可行候选时如实返回不可行/未找到，不能放宽约束后仍称通过。
- 用成熟优化库求解，在声明的同版本环境/种子下可复现；不宣称全局最优。多个随机种子或三个相似候选本身不构成创新。
- 保存候选为子 revision，再重新读取最终 PCB 验算旋转、焊盘变换、锁定和几何约束。未支持的凹板、孔洞、keepout、自定义焊盘、背面等必须报告范围，不能以外包矩形替代真实几何仍给通过。
- 已布线板移动 footprint 会影响铜连通；当前契约拒绝这类放置变更是合理 MVP 边界。所谓 ECO 最小扰动重布局、局部拆线/重布线属于后续能力，不能提前列为完成。
- `net_rules.min_width_mm` 必须进入实际路由规则或最终几何检查；只存在 YAML/JSON 中、或者只影响评分，不等于约束已经落实。

可证伪验收：固定连接器不得移动；邻近与区域规则冲突必须暴露；制造阈值变更会改变验收；原目标值改善但出现 courtyard 重叠时必须失败。比较基线应记录同一输入、参数、约束可行率、估计线长、运行时和回导后的真实 DRC/未连通数；不能只比较布局评分。

### 4.3 制造可追溯版本/门禁：同一输入、同一证据、同一工件

优先级 P0，可与 ECO 分阶段交付。Konnect 已有设计哈希、门禁基础代码及非空产物检查，因此本项目差异要由跨操作的实际强制绑定证明。[K7][K10][K11][K12]

建议发布记录绑定：父/子 revision、完整输入文件清单及摘要、约束/厂商 profile 版本、EDA/路由器/规则引擎版本与参数、DRC/ERC/parity 原始报告哈希、DSN/SES 哈希、制造工件类型/路径/大小/哈希、验收结论及明确的人工确认范围。manifest 自身不参与自身哈希；如需防篡改，另加可信签名或外部锚点，SQLite 和 SHA-256 本身不构成防恶意篡改证明。

输入身份不能只覆盖 `.kicad_pcb`。层次原理图、`.kicad_pro`、`.kicad_dru`、约束、订单 profile，以及影响解析/输出的库表或已解析库内容都要纳入闭包或明确固定版本。设计规范化摘要和交付物原始字节哈希是不同用途，应分字段保存。

发布服务应从不可变输入快照执行检查与导出，核对检查前后的输入身份；工件先在暂存目录生成，验证成功后才提交 release 记录。警告、豁免、NOT_APPLICABLE 的处理必须确定，不能由 LLM 临时决定；若某检查确实不适用，应保存判定理由，而不是把“没运行”折算为 PASS。

可证伪验收：DRC 后改一个子原理图、规则文件或约束，旧 PASS 必须失效；导出一半失败不得发布；已存在旧 Gerber 不得冒充新产物；BOM/CPL 的设计位号、DNP、面别/单位不一致不得称装配包合格；制造包任一字节被改后复核失败；并发发布和进程中断不得留下“成功但缺文件”的记录。

## 5. Skill 与 MCP 的职责及交付阶段

建议的工具职责包括 `import_project`、`analyze_eco`、`plan_placement`、`audit_constraints`、`apply_candidate`、`route_revision`、`verify_revision`、`prepare_release`、`verify_release`。这些是语义建议，不是当前已注册工具名。

Skill 应引导工程师明确变更意图、读取数据手册来源、解释影响/候选权衡、处理未知项、读取真实工具状态并形成审查记录；不能替代解析器、几何检查、版本核验和发布权限。MCP 应接收结构化输入并返回可机器判定的状态、证据路径和版本身份；不用让 LLM 搬运巨量 DSN/SES 文本。

| 阶段 | 可交付结果 | 不能提前宣称 |
| --- | --- | --- |
| A | 独立导入/版本记录、基础几何与结构 ECO、真实 DRC/ERC、冻结制造包与门禁 | 完整电路语义 ECO、硬件功能验证 |
| B | 显式约束候选布局、原生 DSN/SES 往返、回导后复验 | 高速签核、任意板型、全局最优 |
| C | pin/net/规则/工件依赖图、影响解释、制造订单 profile、人工验收记录 | 未接入供应商/实验室的数据已受控 |
| D | 实际制造、装配、上电与测试记录闭环 | 仅凭软件报告量产放行 |

最有说服力的演示是一个低压双层真实项目的 A/B ECO：先让旧验证失效，生成/选用受约束候选，实际路由与回导，再用当前版本检查和制造 manifest 完成闭环，同时展示至少一个被可靠阻止的错误案例。

## 6. 未覆盖的工程范围

以下是需求覆盖边界，不是现行法规/标准的适用性判断；本次没有调研具体产品的认证法规或标准版本。

| 范围 | 当前不能由本项目证明的内容 | 后续所需证据 |
| --- | --- | --- |
| SI 信号完整性 | 受控阻抗、差分/总线时序、串扰、反射、回流路径、眼图、连接器/过孔模型 | 确定叠层/介质模型、IBIS/SPICE/S 参数、场求解或链路仿真及测量；布局距离与网络名字不能替代 |
| PI 电源完整性 | PDN 阻抗、直流压降、动态负载、电源时序、去耦反谐振、电流密度与温升 | 电源模型、铜结构/电流/温度条件、仿真和负载测试；去耦“放得近”不是 PI 签核 |
| 电路功能 | 元件参数裕量、启动、保护、容差、异常工况、固件与硬件配合 | 数据手册约束、电路分析/仿真、样板测量；ERC/DRC 不证明功能 |
| EMI/EMC、ESD | 发射、抗扰、浪涌、静电、线缆和外壳效应 | 明确产品环境、专业预一致性和实验室测试；不能自动给认证通过 |
| 安全/认证 | 市场及产品类别对应的安全、无线、环境、绝缘等适用性和符合性 | 产品范围确认、适用标准版本、材料/BOM 文档及合格评定；24V 配置不是认证结论 |
| 机械与工艺 | 外壳配合、高度/公差、槽孔/拼板、阻焊桥、钢网、特殊钻孔、装配旋转约定 | 机械模型、真实厂商工艺合同、Gerber/CAM 与装配预览人工确认 |
| 供应链与量产 | MPN 替代资格、批次、到料、PCN/EOL、良率、测试覆盖、返修追踪 | 供应商/PLM/MES/测试系统数据及实际生产记录；manifest 只追溯设计到发布文件 |

不支持的板型、解析几何或检查能力应作为结构化 unknown/blocked 返回。不可因为功能尚未覆盖，就在总报告中省略该维度或将其隐含为通过。

## 7. 后续独立审查准备

本轮未对其他 agent 的实现作出通过结论。实现冻结后应记录准确 commit/工作树快照及环境，再独立核对服务层、CLI、MCP、Skill、真实工具和报告的一致性。测试证据应分为静态阅读、纯单元测试、模拟工具测试、真实 EDA 集成测试和实物验证，不能互相替代。

| 审查场景 | 预期结果 | 对应需求 |
| --- | --- | --- |
| 保持网拓扑，仅重新编号 net code 或位号 | 不误报为全网改接；身份歧义显式输出 | R1/R3 |
| 引脚换网、器件封装/DNP/规则变化 | 有直接变化、影响路径和证据失效说明；缺少分析能力明确标注 | R3 |
| 非 ASCII 路径、层次子页、未知 AST 字段、旋转焊盘 | 真实读取/写副本后仍可由 KiCad 解析；不丢字段、不丢层次 | R1/R4 |
| 不可行区域/邻近约束、锁定冲突、缺板框 | 无假可行候选；原因可定位至约束 | R2/R4 |
| 规则被保存但路由参数不变 | 最终实际几何审计仍能抓住违规；不得宣称规则已生效 | R2/R5 |
| 已布线板移动器件 | 按支持边界拒绝，不能保留悬空旧铜后算成功 | R4/R5 |
| MCP schema 变化、路由错误终态、202 部分结果、204、空 SES、错板 SES | 显式失败/阻塞，不生成伪成功子版本 | R5 |
| 假 CLI 返回 0 但无报告/空报告/缺字段 | 必要检查不通过，发布失败 | R6/R8 |
| DRC violations 为空，但 unconnected/parity 有问题 | 不得当作干净板；保留相关 item 信息 | R6 |
| DRC 后改变 PCB、子原理图、规则、约束或工具/profile 版本 | 旧证据失效；对变更范围给出说明 | R3/R8 |
| 错配原理图 BOM 与 PCB CPL；缺钻孔/层；旧文件或零字节文件 | 制造工件集合核验失败 | R7 |
| 检查与导出之间改文件、并发操作、导出中断 | 不产生有效 release；源板和已有 revision 不被污染 | R8/R10 |
| 绕过 Skill，直接调用 MCP 或 CLI 发布 | 服务层同样执行全部门禁 | R9 |
| manifest 工件字节变动、路径越界、重用其他 revision 的报告 | 完整性/身份核验失败 | R7/R8 |
| KiCad/Java/原生 helper 未安装 | 返回 blocked 和具体缺失项；不伪造 DRC/路由结果 | R5/R6 |

独立审查的最低真实集成证据：至少一个 KiCad 可打开的非空样板，通过真实 DSN 导出、Freerouting 路由、SES 回导、最终 DRC/ERC 和制造文件生成；另有一个故意断网/违规样板被拒绝。记录工具版本、命令参数、退出码、原始 JSON、输入/产物哈希。若只跑通模拟测试，应写“业务编排测试通过，真实 EDA 尚未验证”。

审查优先核对实施契约中可能出现的语义缺口：结构 compare 是否被包装成完整 ECO、`max_voltage` 等配置是否仅记录、net rule 是否进入实际检查、整体摘要是否包含子页/规则/约束、release 是否绑定当前报告、布局估计是否被写成 SI/PI 结论。

## 8. 实际阅读清单与引用

“全文”只用于确实读过全部内容的小文件；“选段”表示函数/区间和相关符号搜索，未声称全文件审查。目录枚举和搜索命中不计为读过所有目录内容。以下链接固定关键源码提交，便于后续审查重放。

### Konnect（固定提交 1401e9a）

| 引用 | 实际文件 | 阅读范围/用途 |
| --- | --- | --- |
| [K1] | `README.md`、`tool-directory.md` | 官方网页主要流程及工具表；初始浏览默认分支，未将工具数量作为结论 |
| [K2] | `crates/konnect-core/src/tools/placement.rs` | 工具/评分选段；本地 769-840、1042-1145、1236-1338；聚类、权重、锁定、碰撞修正和候选评分；其余函数/测试名搜索 |
| [K3] | `crates/konnect-core/src/tools/pcb_sync.rs` | 1-345、550-680 的相关区段及函数/冲突/哈希检索；同步计划、过期校验与回读 |
| [K4] | `crates/konnect-core/src/specctra_ses.rs` | 定向搜索和展开显示的 manifest/path/hash/profile/placement 校验命中；非全文 |
| [K5] | `crates/konnect-core/src/freerouting_mcp.rs` | 1-120、138-431；子进程模式、能力探测、输入/输出校验、作业状态机 |
| [K6] | `crates/konnect-core/src/tools/pcb_routing.rs` | 工具声明及 805-931；live revision 再核对、commit、回读、候选 DRC、补偿事务 |
| [K7] | `crates/konnect-core/src/tools/manufacturing.rs` | 在线读取 handler 正文，本地复核 128-180、287-354、417-494；导出/检查；未全文阅读测试部分 |
| [K8] | `crates/konnect/assets/skills/kicad-manufacture/SKILL.md` | 全文，作为研究对象；订单合同、工件验收与人工检查；没有将其指令应用于本任务 |
| [K9] | `crates/konnect/assets/skills/kicad-review/SKILL.md` | 工具清单、证据优先级和检查流程选段；输出部分截断，不列为全文 |
| [K10] | `crates/konnect-core/src/tools/cli.rs` | 1-138、418-486、784-840；DRC 报告分类与解析、Gerber staging/非空验证 |
| [K11] | `crates/konnect-core/src/gates.rs` | 全文含单元测试，未执行 |
| [K12] | `crates/konnect-core/src/design_hash.rs` | 全文含单元测试，未执行 |
| [K13] | `crates/konnect-vcs/src/lib.rs` | 85 行全文含测试，仓库根校验 scaffold |
| [K14] | `crates/konnect-core/src/lib.rs` | 模块导出相关命中；全 `crates/**/*.rs` 的 gate/hash 调用符号搜索 |
| 辅助 | `crates/konnect-core/src/tools/design_review.rs` | 1061-1270，汇总检查、层次覆盖与直接 DRC；其余函数名检索 |
| 辅助 | `crates/konnect-core/src/tools/integration.rs`、`router/registry.rs` | 仅定向符号检索，不作为 handler 正文已读证据 |

制造 Skill 中“files 可能只是目录列表”的描述与本地当前 handler 的 verified_paths 实现存在时间差；报告以源码为准，不能把 Skill 的旧说明当作当前缺陷。`design_review.rs` 中相关入口可见 [设计审查实现][K15]。

### NiRuLabs（固定提交 2fd14d3）

| 引用 | 实际文件 | 阅读范围/用途 |
| --- | --- | --- |
| [N0] | `README.md` | 官方页面流程、工具表和限制说明 |
| [N1] | `src/kicad_mcp/server.py` | 45 行全文，本地复核 |
| [N2] | `src/kicad_mcp/tools.py` | 285-331、759-820，及 route/transaction 分发符号；不是全文 |
| [N3] | `src/kicad_mcp/ipc_client.py` | 类/函数检索、NNG 连接相关命中、526-570、2805-2888；路由事务与 DRC warning 行为 |
| [N4] | `route_all_nets.py` | 125 行脚本正文全文，在线读取；实验性循环和固定 skip 计数 |

### Freerouting（核心代码固定提交 a11c0a4）

| 引用 | 实际文件 | 阅读范围/用途 |
| --- | --- | --- |
| [F1] | `docs/API/MCP.md` | 当时 master 文档全文；部署方式、工具状态机；未执行其中命令 |
| [F2] | `src/main/java/app/freerouting/api/mcp/OpenApiMcpToolRegistry.java` | 固定 SHA 的 55-200、339-470；OpenAPI 枚举及工具命名；其余结构符号检索 |
| [F3] | `integrations/mcp-server/src/index.js` | 固定 SHA 的 1-50、105-243、260-292 及函数检索；桥目标、本地读写和 REST 传输 |
| [F4] | `src/main/java/app/freerouting/api/v1/JobControllerV1.java` | 固定 SHA 的 1-40 及作业/输出/DRC入口与注释搜索；确认 facade 委托，不当作下游实现 |
| [F5] | `src/main/java/app/freerouting/api/mcp/McpControllerV1.java` | 固定 SHA 的 294-383，tools/list/call 与 HTTP 转发；其余函数名检索 |
| [F6] | `src/main/java/app/freerouting/api/v1/JobProgressResource.java` | 固定 SHA 的 192-286；startJob 正文及 cancel 分支选段 |
| [F7] | `src/main/java/app/freerouting/api/v1/JobOutputResource.java` | 固定 SHA 的 116-210、592-662；输出终态、202/204 和 DRC 正文 |
| 辅助 | `integrations/mcp-server/package.json` | 当时 master 全文，识别 Node 桥入口；不据此推断 Java 功能版本 |

本项目材料：`outputs/pcb-weaver/docs/IMPLEMENTATION_CONTRACT.md` 全文，用于对齐需求和独立审查条件；没有把正在实现的任何模块标为已验证。

[K1]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/README.md
[K2]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/tools/placement.rs#L1042
[K3]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/tools/pcb_sync.rs#L170
[K4]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/specctra_ses.rs#L143
[K5]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/freerouting_mcp.rs#L289
[K6]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/tools/pcb_routing.rs#L835
[K7]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/tools/manufacturing.rs#L128
[K8]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect/assets/skills/kicad-manufacture/SKILL.md
[K9]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect/assets/skills/kicad-review/SKILL.md
[K10]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/tools/cli.rs#L418
[K11]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/gates.rs
[K12]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/design_hash.rs
[K13]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-vcs/src/lib.rs
[K14]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/lib.rs
[K15]: https://github.com/mixelpixx/Konnect/blob/1401e9ab78fd915dc5f06b7a8865f909d5ce536b/crates/konnect-core/src/tools/design_review.rs#L1061
[N0]: https://github.com/NiRuLabs/kicad-mcp-server/blob/2fd14d32fb2532071649cf6779b5e97b524e0a1b/README.md
[N1]: https://github.com/NiRuLabs/kicad-mcp-server/blob/2fd14d32fb2532071649cf6779b5e97b524e0a1b/src/kicad_mcp/server.py
[N2]: https://github.com/NiRuLabs/kicad-mcp-server/blob/2fd14d32fb2532071649cf6779b5e97b524e0a1b/src/kicad_mcp/tools.py#L299
[N3]: https://github.com/NiRuLabs/kicad-mcp-server/blob/2fd14d32fb2532071649cf6779b5e97b524e0a1b/src/kicad_mcp/ipc_client.py#L2805
[N4]: https://github.com/NiRuLabs/kicad-mcp-server/blob/2fd14d32fb2532071649cf6779b5e97b524e0a1b/route_all_nets.py
[F1]: https://github.com/freerouting/freerouting/blob/master/docs/API/MCP.md
[F2]: https://github.com/freerouting/freerouting/blob/a11c0a42d1b3827e5126429c5c9820c4ab5bec7c/src/main/java/app/freerouting/api/mcp/OpenApiMcpToolRegistry.java#L55
[F3]: https://github.com/freerouting/freerouting/blob/a11c0a42d1b3827e5126429c5c9820c4ab5bec7c/integrations/mcp-server/src/index.js#L105
[F4]: https://github.com/freerouting/freerouting/blob/a11c0a42d1b3827e5126429c5c9820c4ab5bec7c/src/main/java/app/freerouting/api/v1/JobControllerV1.java
[F5]: https://github.com/freerouting/freerouting/blob/a11c0a42d1b3827e5126429c5c9820c4ab5bec7c/src/main/java/app/freerouting/api/mcp/McpControllerV1.java#L294
[F6]: https://github.com/freerouting/freerouting/blob/a11c0a42d1b3827e5126429c5c9820c4ab5bec7c/src/main/java/app/freerouting/api/v1/JobProgressResource.java#L192
[F7]: https://github.com/freerouting/freerouting/blob/a11c0a42d1b3827e5126429c5c9820c4ab5bec7c/src/main/java/app/freerouting/api/v1/JobOutputResource.java#L116

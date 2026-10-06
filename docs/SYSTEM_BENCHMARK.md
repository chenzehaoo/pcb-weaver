# 系统级控制板基准

## 结论与入口

选择 KiCad 官方 `kit-dev-coldfire-xilinx_5213` demo 的真实 MCU/CPLD 控制板，
而不是用重复无源器件合成规模。基准用于软件分析、布局与全新布线验证，
**不是 PCB Weaver 原创参考电路，不代表硬件功能、制造或量产批准**。

- 工程目录：`examples/system-controller/`
- 匹配根文件：`kit-dev-coldfire-xilinx_5213.kicad_pcb`、同名 `.kicad_sch`、同名 `.kicad_pro`
- 子页：`in_out_conn.kicad_sch`、`xilinx.kicad_sch`
- 工程约束：`examples/system-controller/constraints.json`
- 来源、每文件哈希与修改日志：`examples/system-controller/PROVENANCE.json`
- 原生最终证据：`benchmarks/results/system-controller-frozen/`
- 适配器审计与图统计：`benchmarks/SYSTEM_BASELINE.json`
- 冻结清单：`benchmarks/FROZEN.json`，包含工程哈希、约束哈希和原生证据哈希。

本轮没有运行完整优化及 freerouting 新路由；该阶段交给主线程的真实 pipeline 作业。
当前原生基线有未连接错误，不能发布制造包。测试通过只表示基准证据与约定一致。

## 来源与许可

- 上游版本：KiCad `9.0.0`。
- 固定提交：`286b0611feca00727bf70bfa184ec2c28a745dc3`。
- [官方源码镜像中的控制板目录](https://github.com/KiCad/kicad-source-mirror/tree/286b0611feca00727bf70bfa184ec2c28a745dc3/demos/kit-dev-coldfire-xilinx_5213)。
- [该提交的 LICENSE.README](https://github.com/KiCad/kicad-source-mirror/blob/286b0611feca00727bf70bfa184ec2c28a745dc3/LICENSE.README)
  将 `demos/*` 列为 CC BY-SA 4.0；副本随工程携带为 `LICENSE.KiCad.README`。
- 衍生工程沿用 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)，保留原 demo 贡献者归属、
  原理图标题 `Dev kit coldfire 5213`、日期 `Sun 22 Mar 2015`、修订 `0` 及源工程内容。
  原理图没有列出个人作者，不臆造作者姓名；完整源文件及固定提交提供贡献历史入口。
- `PROVENANCE.json` 明确标记为 PCB Weaver 软件基准衍生物，不能据此宣称原设计经本项目硬件验证。
- service 已将 `PROVENANCE.json` 和 `LICENSE.KiCad.README` 纳入版本哈希及制造包，不能在分发时省略归属。

只实际解析了 `benchmarks/CANDIDATES.json` 列出的候选 PCB，并详细读取选中工程的板、三张原理图、
工程规则、库表、本地库和许可说明；没有声称完整读完 KiCad 大型源码仓库。
`interf_u` 规模不足，`video` 是更大的四层视频板，`vme-wren` 是 12 层超大板；选择记录可复查。

## 实际规模

| 指标 | 实测 |
| --- | ---: |
| PCB 封装实例 / 原理图物理器件 | 160 / 160 |
| 物理焊盘 | 825 |
| 唯一非空 `(reference, pad number)` 电气焊盘标识 | 807 |
| 非空网络总数 | 278 |
| 多端点网络 / 单端点网络 | 209 / 69 |
| 铜层 | F.Cu、In1.Cu、In2.Cu、B.Cu |
| 不同封装 | 35 |
| 功能子系统 / 跨子系统网络 | 3 / 56 |
| 最大器件连接图 / 孤立器件 | 160 / 0 |
| 原始板框 | 157.48 × 91.44 mm |
| 软件基准板框 | 170 × 122 mm，坐标 `(64,31)` 至 `(234,153)` |

单端点网络含设计中真实的未使用引脚，不将其冒充有效跨模块互连；即使只计多端点网络也远超 40。
图连通性不等于电路功能证明，电源网络也会连接多个子系统。

原根页有 66 个器件，`/inout_user/` 有 73 个，`/xilinx/` 有 21 个。
核心器件包括 U102 `MCF5213-LQFP100`、U301 `XCR3256-TQ144`、U202/U203/U204 `MAX202`、
U205 `PCA82C251`、U201 `74HC125`，以及稳压、电源保护、复位、晶振、去耦、CAN/RS232/扩展接口。
封装含 LQFP-100、TQFP-144、SOIC-8/14/16、SOT353、SOT-23、TO-92、TO-263、0805/1206 R/C、
二极管、LED、排针、接线端子、DB9。**没有 TSSOP，不虚报此类覆盖**。

原生 XML 中使用的库符号定义包含 input、output、bidirectional、tri_state、power_in、power_out、
open_collector、passive 八类引脚；类型分布保存在基线。该统计针对使用的库定义，不冒充实例焊盘总数。
引脚电气类型和全部原理图接线与上游逐项比较保持不变，不存在“全 passive 假 ERC”。

## 衍生变更

1. 原版完整保留在 `benchmarks/results/upstream-9.0.0/design/`，执行检查不修改原版。
2. 去除 2935 段走线、253 个过孔、3 个铜区、5 个板级铜层文字。
   基准内没有继承的手工布线或灌铜，后续必须从零完成全部路由。
3. 原版部分 DB9、开关等机械本体伸出板边，和当前“完整庭院须在板内”的约束不兼容。
   因此用容纳现有所有保守器件包络并至少留 5 mm 空间的矩形取代原四条板边。
   此项是明确的软件测试机械变更，不宣称原产品板框保持不变。
4. 保留全部器件、值、位置、旋转、面别、焊盘几何、net code/name 与四层 stackup。
   特别保留 14 个背面去耦电容，不把它们偷偷变成正面器件。
5. 上游 PCB 已用本地 `kit-dev-coldfire:` 库，而部分原理图实例仍写官方旧库前缀。
   同步 152 个符号实例的 Footprint 属性，对应原生报告中的 147 个封装 ID 不匹配项；
   多单元器件存在多个符号实例，因此数量不同。
6. 电容、LED 等符号保留了过时封装过滤器。只向对应本地符号库及原理图缓存追加实际选用的具体封装名称，
   不移除原有允许项，不加入 `*` 放行所有封装。修改前后字符串均在 `PROVENANCE.json`。
7. 所有原项目 `ignore` 检查改为 `warning`；DRC 的 extra/missing footprint、footprint-symbol mismatch、
   net conflict 以及 ERC pin-to-pin 提升为 `error`。其余规则数值、ERC pin map 和检查配置保持不变。
   无新增排除项、无降低严重性、无更改引脚类型、无更改器件值、无改接连线。

导出的原版与衍生版 XML netlist 按网络名和 `(reference,pin)` 完整比较相同。
本地 `fp-lib-table`、`sym-lib-table` 均仅引用 `${KIPRJMOD}/...`；44 个工程依赖文件闭包通过校验，
加上来源和许可文件，目前版本封存集为 46 个文件。
这不意味着全局库、3D 模型及工具用户配置全部被固定，相关范围声明必须保留。

## 真实原生基线

运行环境为 WSL Ubuntu 中 KiCad CLI/Python **9.0.9**。每次在独立工程副本运行：
ERC 和 DRC 使用 `--severity-all --exit-code-violations`；DRC 另用 `--all-track-errors --schematic-parity`。
所有命令、stdout/stderr、退出码、输入/产物 SHA-256、耗时保存在各目录的 `baseline.json`。

| 工程 | ERC | DRC | 原生 parity | 未连接 |
| --- | --- | --- | ---: | ---: |
| 未修改上游 | 原配置下报告 0 项 | 194 warnings | 147 | 0 |
| 初次元数据迁移、恢复检查 | 75 warnings | 499 errors、115 warnings | 59 | 499 |
| 具体封装过滤器迁移后 | 16 warnings | 499 errors、56 warnings | 0 | 499 |
| 最终扩框基准 | **0 errors、16 warnings** | **499 errors、47 warnings** | **0** | **499** |

上游“报告 0 项”受到原有 ignore 设置影响，不能表述为绝对没有 ERC 问题。
最终 ERC 警告为 3 个 four-way-junction、13 个 multiple-net-names；全部保留供工程复核。
最终 DRC 的 499 个错误均为待连接，47 个警告均为本地库与板上 C_0805 封装副本差异。
保留原板实际几何，不通过刷新封装覆盖真实焊盘来消掉这些警告。
扩框后原来的 9 个丝印边缘警告自然消失，没有关闭丝印规则。
输入原工程与引擎工作副本在检查前后哈希均未变化。

## 适配器与后续验收

冻结时的核心适配器边界在 `SYSTEM_BASELINE.json`，附当时核心源码哈希，不代表后续核心修复状态：

- 原生 parity 为 0，但服务 netlist 比较有 10 项 KiCad 转义差异：板中 `{slash}` 与 XML 的 `/`。
  涉及 CLKIN/EXTAL、TCLK/PSTCLK/CLKOUT、TXD2/CANL、CTS2/CANH；器件值、封装和引用一致。
  必须在核心层正确规范化 KiCad 名称并测试冲突，不靠修改真实电路或略过比较。
- 14 个背面电容需要镜像局部坐标、铜层与绝对焊盘旋转的正确支持。
- J201.3 的真实长圆孔需要按方向比较钻孔和铜焊盘包络，不能只比较最大钻孔尺寸与最小焊盘尺寸。
- 现有保守 AABB 布局检查仍可报告冲突；不能将 native DRC 无几何错误直接转换为 `audit.passed=true`。
  板框已经扩展，固定连接器不再仅因原机械悬伸导致板边约束无解。

`test_adapter_parity_has_no_undocumented_gap` 会识别上述已记录转义差异，也允许核心修复后的完整 pass；
它不把差异伪装成服务门禁通过。完整 pipeline 验收必须在修复后重新要求 `connectivity.status=passed`。

主线程新路由验收应保存：导入版本摘要、布局候选及约束复核、原生 DSN/SES、层分配、route 前后电气签名、
最终 ERC/DRC/parity 原文、剩余未连接、走线长度/过孔数、制造导出和版本门禁证据。
布局 HPWL 只作估计，实际布线长度及全新路由结果由原生输出判定。
不得降低间距/线宽规则、关闭检查或把 unknown 当作通过。
本项目不提供 SI/PI、时序、EMC、热、固件、BOM 供应、贴装旋转、认证或实物测试结论。

## 复现与测试

在安装项目依赖的 Python 环境中，从项目根运行：

```powershell
python -m pytest tests/test_system_benchmark.py -q
python benchmarks/analyze_system_benchmark.py
```

准备干净固定源：

```powershell
git clone --depth 1 --filter=blob:none --sparse --branch 9.0.0 https://github.com/KiCad/kicad-source-mirror.git benchmarks/upstream/kicad-source
git -C benchmarks/upstream/kicad-source sparse-checkout set demos/kit-dev-coldfire-xilinx_5213
python benchmarks/prepare_system_benchmark.py --output benchmarks/generated/reproduced-system-controller
```

生成器验证固定提交及源目录未修改，拒绝覆盖已存在的输出。它需要项目当前的 board 读取模块来计算保守外框；
冻结工程与证据才是精确基准，不承诺核心算法改变后重新生成的包络仍逐字相同。

在 WSL KiCad Python 中运行原生验证，`--output` 必须是新的证据目录：

```bash
python3 /path/to/pcb-weaver/benchmarks/run_native_baseline.py \
  --project /path/to/pcb-weaver/examples/system-controller \
  --output /path/to/pcb-weaver/benchmarks/results/new-native-run
```

`upstream/`、`generated/` 为本地缓存；固定原版证据、最终工程、原生原始报告、修改日志与测试无需这些缓存即可审查。

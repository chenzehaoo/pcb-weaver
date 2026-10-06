# v0.3 验收记录

日期：2026-09-07。交付：桌面 `pcb-weaver-system`，工作台 `http://127.0.0.1:8765`，版本 0.3.0。升级未覆盖原工程数据、运行环境或 MCP 本机配置。

**结论：本轮台账、导出、机器桥接、MCP 和小板制造链路已通过下述验收；不代表顶尖工业 EDA、任意企业平台即插即用或复杂板自动布线全部通过。**

## 已通过

| 范围 | 实测结果 | 证据 |
|---|---|---|
| Python 完整软件回归 | 922 passed、24 skipped、0 failed；170.60 秒；2 条依赖弃用警告 | [JUnit](validation/pytest-v3.xml) |
| 桌面部署真实 MCP 台账分页 | 全量读取 160 器件、825 焊盘、278 网络、2644 线段、164 过孔、4 铜层、35 封装 | [MCP 记录](validation/inventory-mcp-v3.json) |
| 桌面部署真实 HTTP | 临时随机凭据、项目隔离、来源检查、同键去重、异请求 409、真实数值布局任务完成、交换包逐文件哈希 | [HTTP 记录](validation/integration-83ed6051.json) |
| 桌面部署原生小板制造 | 真实 stdio MCP 导入、布局、应用、Freerouting 布线、KiCad 验证、制造发布、归档校验、报告、ECO、历史全部成功 | [制造记录](validation/v3-desktop-manufacturing.json) |
| 桌面与手机新 UI | 全量分页、搜索、分类/封装/层过滤、数值排序、器件/网络定位、144 引脚详情、六种导出；无页面溢出和浏览器错误 | [UI 记录](validation/inventory-ui-v3-desktop/inventory-results.json) |
| 原有工作台回归 | 工程版本深链接、切换竞争、画布像素、缩放、层、问题定位；桌面与手机通过 | `validation/inventory-ui-v3-desktop/existing-platform/` |
| Skill 和插件 | 官方本地 `quick_validate.py`、`validate_plugin.py` 验证通过 | 项目 `skills/pcb-engineering`、`.codex-plugin/plugin.json` |

制造正例是单独标识的 `examples/manufacturing-demo` 小板，不是 160 器件复杂板。实际工具为 KiCad 9.0.9 和隔离 Java 25 / Freerouting 2.4.1，最终版本 `r-ab0413e5a612437d`，归档位于 `data/projects/v3-desktop-manufacturing/releases/release-78b14222f823.zip`。该通过只表示软件/EDA 及文件交付链路，未实际生产 PCB。

## 本轮修正

- 固化版本台账在原生长任务持有写锁时仍可读取，并发读取不互相阻塞；输入字节哈希、约束哈希、前后版本和证据一致性校验全部保留。
- API 采用有类型的响应模型，测试拒绝错误计数、状态及字段类型；请求限制准确工程/版本标识和机器接口可用操作。JSON Schema 合约回归不等于外部企业认证。
- CSV 保留弧几何、未知线长、部分长度状态；交换包有完整哈希清单，不具有制造授权。
- 机器接口默认禁用、默认只读；重复提交原子去重。UI 不会将未知资料、推断分类或网络存在误示为供应链真实性或电气完成。

## 数据覆盖与限制

复杂板读取版本固定为 `system-mcp-acceptance / r-9b5f5075e65e4931`，摘要 `615cb6cb6bcc6c105a5387e1ec8f846c87d830556cb813340eb3c8ee9deef26c`。台账没有截断，列出全部实际存在的器件与铜线。

但该来源的 160 个器件均未提供独立的制造商、制造料号和数据手册字段，因此如实显示未知。标称值中的型号字符串不是经过验证的 MPN；类别为有依据标注的推断。9 个焊盘没有网络赋值。直线总长排除过孔桶壁和铜区，不能用它评估完整信号路径或阻抗。

## 未通过或未验证

- **复杂板仍有 7 处未连接，制造放行保持阻断。** 本轮没有重新求解大板布线，也没有改变历史失败证据。既有诊断见 [未连接分析](validation/remaining-connectivity-diagnosis.md)。
- 24 条跳过测试包括需要额外权限的符号链接测试、需显式开启的真实 WSL/原生几何、引擎对照、局部铜及多层布线矩阵。它们不计作通过；独立真实 MCP 小板验收不能替代全部跳过矩阵。
- 没有指定企业平台的真实 API/账号/沙箱，因此企业认证、字段映射、审批和端到端 UAT 均未验证。接口是本机桥接，不提供企业多租户、安全部署和高可用承诺，见 [集成说明](INTEGRATION.zh-CN.md)。
- 未实现或未验证高速 DDR/PCIe、阻抗和等长、SI/PI、EMC、热/机械、全球器件库与供应链真实性、实板功能、工厂贴装校正和量产良率。

因此不能把此次交付描述为“所有工业要求全部验证通过”。下一步关键工程仍是复杂封装逃逸及局部重布的原生算法能力，并针对一个明确企业平台完成实际适配验收。

## 复现入口

```powershell
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe scripts/accept_inventory_mcp.py
.venv/Scripts/python.exe scripts/accept_integration.py
.venv/Scripts/python.exe scripts/accept_mcp.py --project new-manufacturing-acceptance --route --example-dir examples/manufacturing-demo --config toolchain.unified.json --passes 10
```

真实验收会创建独立工程，重跑制造脚本请使用新的工程 ID；不得修改冻结基准掩盖失败。浏览器验收需安装 Playwright 与 Chromium，运行 `scripts/check_inventory_ui.cjs`，传入工作台 URL 和截图输出目录。

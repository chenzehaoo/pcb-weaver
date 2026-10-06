# Altium MCP 开发者内测版

## 验收结论

本地开发者内测版已达到可试用状态。它接收配置目录内的 Altium 工程，
校验工程直接引用的文档，在独立副本上调用 Altium 原生编译，按需执行当前启用的
批量设计规则检查，并通过 MCP 返回结果和分页违规明细。

验收覆盖两种不同工程：

| 工程 | 直接引用文档 | 原生结果 |
| --- | ---: | --- |
| Wi-Fi 示例工程 | 6 个；包含两块 PCB，检查时明确选择 `WiFi.PcbDoc` | 编译通过；当前启用的 6 类规则检查返回 0 项违规 |
| 水平仪示例工程 | 9 个；包含子目录内的库文件 | 编译通过 |

两套工程通过真实标准输入输出 MCP 会话调用，来源与副本摘要一致。
中文工程文件名的独立副本也通过原生编译。验收记录分别位于
`validation/altium-beta-mcp-acceptance.json`、
`validation/altium-beta-chinese-acceptance.json` 和
`validation/altium-beta-tests.xml`。本轮相关测试共 101 项通过，
覆盖原有 MCP 入口的回归检查。

Wi-Fi 工程的“0 项违规”只对应实际执行的 6 类规则；完整规则覆盖尚未证实。
内测版不会据此给出整板 DRC、信号完整性、自动布线或制造放行结论。

## 开始使用

当前机器的项目配置已经增加 `altium-developer-beta`，配置文件为
[项目 MCP 配置](../.mcp.json)。开发客户端读取该配置后可调用下列四个工具：

| 工具 | 用途 |
| --- | --- |
| `altium_beta_status` | 查看 Altium 可用性、允许目录和原生任务状态 |
| `altium_beta_inspect` | 读取工程与直接引用文档及文件摘要 |
| `altium_beta_check` | 复制工程并执行原生编译；可选执行当前启用规则的 DRC |
| `altium_beta_report` | 按运行编号分页读取经校验的检查报告 |

先调用 `altium_beta_status`。检查 Wi-Fi 示例工程时，参数示例：

```json
{
  "project_path": "D:\\Altium\\AD26-Examples\\Examples\\Mini PC\\Mini PC - WiFi\\WiFi_miniPCIe.PrjPcb",
  "board_path": "WiFi.PcbDoc",
  "run_drc": true
}
```

工程只有一块 PCB 时可省略 `board_path`；有多块 PCB 时必须明确指定。
`altium_beta_check` 返回 `run_id`，再将该编号传给 `altium_beta_report`。
若报告中的 `selected_rules_pass` 为真，仅表示当前启用规则通过。
查看 `drc_counts.rule_rows` 才知道报告实际包含多少类规则。

在其他开发机上，先安装项目的 Python 依赖及 Altium，并登录可用许可证。
然后生成这台机器自己的 MCP 配置，不要复制本机的绝对路径：

```powershell
python scripts/configure_altium_beta.py --project-root "D:\Work\PCB" --altium-exe "D:\Altium\AD26\X2.EXE"
```

生成的 `altium-beta.mcp.json` 可作为支持 `mcpServers` 格式的开发客户端配置条目。
工程所在目录必须列入 `ALTIUM_BETA_PROJECT_ROOTS`；多个目录用分号分隔。
运行证据默认写在项目的 `docs/validation/altium-beta`，可通过
`ALTIUM_BETA_RUN_ROOT` 改为另一个独立、可写的位置。
目前仅在 Windows 与 Altium Designer 26.7.1 上完成原生验收。

## 工程范围与恢复

内测版读取工程中连续编号的 `DocumentPath` 直接依赖，
支持其下级目录，限制为 64 个文档、单文件 64 MiB、总计 256 MiB。
外部库、托管文档以及未列入工程的生成文件不在这项复制证明范围内。
路径必须位于允许目录内，不接受符号链接、路径回退或网络绝对引用。

原生任务使用独占锁，避免多个请求同时操作 Altium。
超时或响应不完整时锁会保留；开发人员应先检查 Altium 窗口和运行目录的
`result.json`、`response.ini`，确认任务已经结束，再归档
`docs/validation/altium-native/pending.json` 后重试。
内测版不会自动清除结果不确定的任务。

目前提供的是开发者本机、只读工程检查能力。
尚未完成跨版本兼容、完整规则覆盖、图形界面无人值守恢复、任意复杂工程依赖解析，
也没有自动布局布线或制造放行能力。

## 技术依据

原生编译使用 Altium 工程接口和脚本接口，参考
[工程接口说明](https://www.altium.com/documentation/altium-dxp-developer/iproject-interface) 与
[脚本执行说明](https://www.altium.com/documentation/altium-designer/scripting/running-scripts)。
是否通过本轮内测以本机原生运行和 MCP 客户端验收记录为准。

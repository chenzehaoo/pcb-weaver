# 第三方组件与来源

PCB Weaver 主项目代码采用仓库根目录的 Apache-2.0 许可证。该许可证不自动覆盖本仓库中来自第三方的示例电路、封装、图标，也不替代外部工具各自的许可。研究过的 Konnect 和 NiRuLabs 仓库没有复制进主项目实现。

| 组件 | 用途 | 来源或许可 |
|---|---|---|
| KiCad 9.0.9 | PCB 原生操作、ERC/DRC 与导出 | [KiCad 官方网站](https://www.kicad.org/)；使用者需自行安装 |
| Freerouting 1.9.0、2.0.1、2.4.1 | 独立本机布线程序 | [Freerouting 项目](https://github.com/freerouting/freerouting)；GPL，二进制不随公开源码提交 |
| MCP Python SDK | MCP 传输与接口 | [官方项目](https://github.com/modelcontextprotocol/python-sdk) |
| SciPy、sexpdata、Pydantic | 数值优化、结构化解析、约束校验 | 各依赖随安装包保留其原始许可 |
| Starlette、Uvicorn | 本机 HTTP 工作台 | 各依赖随安装包保留其原始许可 |
| Lucide 1.8.0 | 本地界面图标 | ISC；随源码保留 `src/pcb_weaver/web/vendor/LUCIDE-LICENSE` |

Freerouting JAR 不进入公开源码仓库。项目未翻译或复制 Freerouting 路由器源码；版本、下载来源与 SHA-256 记录见 `tools/README.md`。KiCad 与 Java 运行时同样需要使用者按各自许可自行安装或取得。

`examples/system-controller` 是从 KiCad 演示工程衍生的修改版，采用 CC-BY-SA 4.0，不是 PCB Weaver 原创电路。该目录的 `LICENSE.KiCad.README` 和 `PROVENANCE.json` 记录原作者、上游版本和修改内容；其他基于该演示工程的示例目录也保留相应通知。`tests/fixtures/kicad/` 中的第三方封装另见其 `LICENSE.md`。这些文件的许可边界与主项目 Apache-2.0 不同。

公开源码不包含本机上游 KiCad 检出目录、验收数据库、原生运行证据和打包的 Java 运行时。历史验证文档中提及的本机证据路径仅是当时的记录，不能当作公开仓库中可复现的实机验收结果。

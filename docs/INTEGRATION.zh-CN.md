# v0.3 本机工程集成

## 实际定位

这是供同一受信任工作站上的连接器使用的本机桥接接口，不是公网多租户服务。没有已认证的企业平台适配器，也没有实现 SSO、企业 RBAC、TLS 终止或高可用。工作台和 stdio MCP 仍是受信任本机用户接口，机器令牌不保护这两种入口。

接口契约使用 [OpenAPI 3.1.1](https://spec.openapis.org/oas/v3.1.1.html)。浏览 `http://127.0.0.1:8765/api/integration/openapi.json` 获取当前版本，`/api/integration/capabilities` 获取实际启用情况。所有长度单位为 mm。

## 启用与权限

默认关闭。由部署人员在服务进程的环境中配置，重启服务后生效：

| 环境变量 | 语义 |
|---|---|
| `PCB_WEAVER_INTEGRATION_TOKEN` | 密码学随机生成的 32-256 位 URL-safe ASCII 字符，保存在受保护的密钥存储中 |
| `PCB_WEAVER_INTEGRATION_PROJECTS` | 逗号分隔的准确工程 ID；空列表不授权任何工程 |
| `PCB_WEAVER_INTEGRATION_WRITE` | 默认 `0`；只有 `1` 允许提交任务 |

调用方提供 `Authorization: Bearer <token>`。不要把真实令牌写入工程、日志、截图或提交到版本库。应用不向外部平台传输设计，也不会自动配置企业账号。不要通过移除本机来源检查将该工作台直接暴露到公网。

## 可用业务链路

前缀为 `/api/integration/v1`：

| 方法与路径 | 输出或作用 |
|---|---|
| `GET /projects` | 授权工程摘要 |
| `GET /projects/{project}/revisions` | 固化版本摘要 |
| `GET /projects/{project}/revisions/{revision}/inventory` | 完整器件、焊盘、网络、线段、过孔、层及来源覆盖率 |
| `GET /projects/{project}/revisions/{revision}/exchange` | JSON、四类 CSV、哈希清单组成的 ZIP |
| `POST /jobs` | 对已有版本提交布局、布线、验证或发布等任务 |
| `GET /jobs/{job}` | 当前状态与步骤摘要，不返回本机路径和原生日志 |
| `GET /projects/{project}/releases` | 真实制造归档记录 |
| `GET /projects/{project}/releases/{release}` | 已记录且通过完整性检查的制造 ZIP |

导入原文件仍由本机工作台或 MCP 执行，机器接口不接受任意输入路径或替换约束。请求字段以 OpenAPI 为准；无授权返回 401，范围不符返回 403，关闭返回 503，不存在或未授权任务返回 404。

提交必须提供 `Idempotency-Key`，允许 1-128 个 ASCII 字母、数字、点、下划线、冒号或短横线。同一凭据、同一键和请求/运行配置返回原任务，不重复执行；冲突返回 409。冲突后应检查已有任务，不要换键盲目重试。幂等命名空间按凭据区分，轮换令牌后客户端应继续保存旧任务 ID，不能依赖新令牌去重旧提交。

## 数据语义

- 台账绑定版本摘要并复验源文件 SHA-256；版本被修改时拒绝给出可信快照。
- 无来源的制造商、料号、数据手册为 null；类别推断不等于已验证。台账不是全世界元件数据库。
- 每个焊盘列出网络与几何，线段列出端点、层和宽度，过孔列出孔径和所跨层。支持读取的弧保留原始几何，但弧长可能未知；必须同时读取 `length_status`、`length_scope` 和 `geometry_supported`。
- 网络台账的 `connectivity_status=not_evaluated` 不表示接通。制造仍依赖对应版本的 KiCad 原生 ERC/DRC、几何及一致性门禁，见 [KiCad CLI](https://docs.kicad.org/9.0/en/cli/cli.html)。
- CSV 使用 UTF-8 BOM、标准引号转义；可能作为公式执行的字符串加前导单引号。需要原始字符串时使用 JSON，不能直接删除所有转义后打开不可信 CSV。
- 工程交换 ZIP 的 `manufacturing_authorized=false`，只有工程数据，没有制造授权；它不能替代受门禁保护的 Gerber/钻孔/贴装制造归档。

## 企业落地尚需

针对一个明确目标平台取得 API 文档与测试环境，在本机连接器中实现该企业的身份认证、BOM/料号/版本字段映射、审批与同步规则，再执行双方沙箱 UAT。公网部署还需独立安全架构及运维验收。当前没有企业账号联调、供应链数据库覆盖、实板功能或量产验收证据，不能标为任意企业可直接投产。

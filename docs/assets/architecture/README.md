# 架构图源与维护

[技术架构](../../ARCHITECTURE.zh-CN.md) · [项目首页](../../../README.md)

五张原创 SVG 矢量技术图使用白底、分区编号、统一配色与图例，适合 GitHub 阅读和技术报告。文字保留为可编辑文本；每张图含 `title`、`desc`，不依赖网络字体、外部图片或脚本。

| 图件 | 内容 |
| --- | --- |
| [system-overview.svg](system-overview.svg) | 系统总览与 KiCad / Altium 工程域隔离 |
| [engineering-pipeline.svg](engineering-pipeline.svg) | 输入、布局、DSN/SES 布线、复验与发布 |
| [revision-evidence.svg](revision-evidence.svg) | revision、verification、release 的绑定 |
| [runtime-integration.svg](runtime-integration.svg) | MCP / HTTP、持久队列、单活 Worker |
| [altium-service.svg](altium-service.svg) | Altium 静态检查、指纹、租约与实验边界 |

## 重新生成

```powershell
python scripts/render_architecture.py
```

生成器只依赖 Python 标准库。修改布局、文字或连线请编辑 [`scripts/render_architecture.py`](../../../scripts/render_architecture.py)，再重新生成 SVG，保持图源与产物一致。

蓝色表示 KiCad 接入与编排，绿色表示工程计算、工具与证据，棕色表示独立服务或发布边界，红色表示阻断或实验能力边界。连线含义见各图注释。图件不代表新增实验结果。

改图后检查文字溢出、连线交叉、源码对应关系及状态语义。字体优先 Microsoft YaHei / Noto Sans CJK SC / PingFang SC；换环境时检查字体替换后的布局。

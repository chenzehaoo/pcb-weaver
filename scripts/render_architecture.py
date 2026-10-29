"""Generate the documentation's editable, dependency-free SVG figures.

Run from any directory: python scripts/render_architecture.py
SVGs contain text, vector paths and accessible descriptions; no external assets.
"""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'assets' / 'architecture'
OUT.mkdir(parents=True, exist_ok=True)
C = dict(ink='#24364B', muted='#63758A', blue='#3C6D9C', teal='#287F7A',
         amber='#AD7733', red='#AD5552', line='#BCCAD6', light='#F4F7FA',
         bfill='#EDF3FA', tfill='#EDF6F3', afill='#FBF5E9', rfill='#FAEFEE')


class Figure:
    def __init__(self, name, h, title, subtitle, desc):
        self.name, self.h, self.parts = name, h, []
        self.add(f'<svg xmlns="http://www.w3.org/2000/svg" width="1760" height="{h}" viewBox="0 0 1760 {h}" role="img" aria-labelledby="title desc">')
        self.add(f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>')
        self.add('<defs>'+''.join(f'<marker id="arrow-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 1 1 L 9 5 L 1 9" fill="none" stroke="{C[k]}" stroke-width="1.6"/></marker>' for k in ['blue','teal','amber','red','muted'])+'</defs>')
        self.add('<style>text{font-family:"Microsoft YaHei","Noto Sans CJK SC","PingFang SC",Arial,sans-serif} .mono{font-family:Consolas,"Liberation Mono",monospace}</style>')
        self.rect(0, 0, 1760, h, '#FFFFFF', 'none', 0)
        self.text(title, 48, 57, 34, 'ink', 700)
        self.text(subtitle, 48, 99, 20, 'muted')
        self.add(f'<path d="M48 124 H1712" stroke="{C["line"]}"/>')

    def add(self, s): self.parts.append(s)

    def text(self, s, x, y, size=22, color='ink', weight=400, anchor='start', mono=False):
        self.add(f'<text x="{x}" y="{y}" font-size="{size}" fill="{C.get(color,color)}" font-weight="{weight}" text-anchor="{anchor}"'+(' class="mono"' if mono else '')+f'>{escape(s)}</text>')

    def rect(self, x, y, w, h, fill='white', stroke='line', r=8, dash=False):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{C.get(fill,fill)}" stroke="{C.get(stroke,stroke)}" stroke-width="1.5"'+(' stroke-dasharray="8 6"' if dash else '')+'/>')

    def panel(self, x, y, w, h, title, color='blue', fill='light', dash=False):
        self.rect(x,y,w,h,fill,color,12,dash)
        self.text(title,x+22,y+36,24,color,700)

    def node(self, x,y,w,h,title,lines=(),color='blue',fill='white',num=None):
        self.rect(x,y,w,h,fill,color)
        self.add(f'<path d="M{x+2} {y+12} V{y+h-12}" stroke="{C[color]}" stroke-width="4"/>')
        dx=22
        if num:
            self.add(f'<circle cx="{x+30}" cy="{y+32}" r="16" fill="{C[color]}"/>')
            self.text(str(num),x+30,y+38,17,'#fff',700,'middle');dx=57
        self.text(title,x+dx,y+38,24,color,700)
        for i,line in enumerate(lines): self.text(line,x+22,y+72+i*29,20,'ink')

    def edge(self, pts, color='blue', dash=False, label=None, lx=None, ly=None):
        d='M'+' L'.join(f'{x} {y}' for x,y in pts)
        self.add(f'<path d="{d}" fill="none" stroke="{C[color]}" stroke-width="2" stroke-linejoin="round" marker-end="url(#arrow-{color})"'+(' stroke-dasharray="7 6"' if dash else '')+'/>')
        if label:
            x,y=lx,ly
            width=sum(11 if ord(c)<128 else 20 for c in label)+14
            self.rect(x-7,y-20,width,27,'white','none',3)
            self.text(label,x,y,19,color)

    def db(self,x,y,w,h,title,lines=(),color='teal'):
        self.add(f'<path d="M{x} {y+15} C{x} {y-5},{x+w} {y-5},{x+w} {y+15} V{y+h-15} C{x+w} {y+h+5},{x} {y+h+5},{x} {y+h-15} Z" fill="white" stroke="{C[color]}" stroke-width="1.7"/>')
        self.add(f'<path d="M{x} {y+15} C{x} {y+35},{x+w} {y+35},{x+w} {y+15}" fill="none" stroke="{C[color]}" stroke-width="1.4"/>')
        self.text(title,x+w/2,y+57,23,color,700,'middle')
        for i,line in enumerate(lines):self.text(line,x+w/2,y+88+i*27,19,'ink',400,'middle')

    def footer(self, sentence):
        y=self.h-65
        self.add(f'<path d="M48 {y-22} H1712" stroke="{C["line"]}"/>')
        self.text(sentence,48,y+9,19,'muted')
        self.text('PCB WEAVER  /  TECHNICAL ARCHITECTURE',1712,self.h-20,14,'muted',400,'end')

    def save(self):
        self.add('</svg>')
        (OUT/(self.name+'.svg')).write_text('\n'.join(self.parts)+'\n',encoding='utf-8')


def system():
    f=Figure('system-overview',1390,'01  系统总览  /  System architecture',
             '本机工程编排 · 独立执行生命周期 · 版本绑定的验证证据',
             'MCP、工作台和可选机器接口连接 KiCad 工程服务。持久任务由工作台或独立 Worker 执行；布局、布线、验证和发布共享版本门禁。Altium 使用独立 MCP、SQLite 和 Worker，仅提供静态检查内测。')
    f.node(48,155,510,105,'AI / MCP 客户端',['工程读取 · 直接工具 · 提交 / 查询任务'])
    f.node(606,155,510,105,'本机浏览器',['工作台 HTTP · 工程与证据可视化'])
    f.node(1164,155,548,105,'本机连接器（可选）',['机器接口 · 默认关闭'],color='muted')
    f.panel(48,320,1080,750,'A  KiCad 工程域',fill='bfill')
    f.panel(1164,320,548,750,'B  Altium 静态检查内测',color='amber',fill='afill')
    f.node(76,390,310,108,'stdio MCP',['server.py'],fill='white')
    f.node(424,390,310,108,'HTTP 工作台',['platform.py'],fill='white')
    f.node(772,390,328,108,'机器接口',['integration.py · 有范围授权'],color='muted')
    f.edge([(300,260),(300,294),(365,294),(365,390)],label='stdio',lx=310,ly=287)
    f.edge([(861,260),(861,280),(579,280),(579,390)],label='loopback HTTP',lx=602,ly=274)
    f.edge([(1438,260),(1438,285),(936,285),(936,390)],'muted',True)
    f.node(76,568,460,133,'持久任务与执行器',['JobQueue · 运行配置 / 输入快照','工作台线程 或 独立 worker.py'],num='1')
    f.node(594,568,506,133,'共享工程服务',['EngineeringService · 项目锁','导入 / 修改 / 检查 / 发布门禁'],num='2')
    f.edge([(231,498),(231,568)])
    f.edge([(579,498),(579,530),(306,530),(306,568)])
    f.edge([(936,498),(936,540),(478,540),(478,568)],'muted',True)
    f.edge([(536,634),(594,634)],label='调用',lx=541,ly=619)
    f.edge([(386,444),(405,444),(405,518),(847,518),(847,568)],label='直接工具',lx=700,ly=512)
    f.node(76,778,310,133,'约束与布局',['models / compiler','planning / placement_large'],color='teal')
    f.node(424,778,310,133,'布线与受限修复',['completion / repair 系列','候选 · 预算 · 局部策略'],color='teal')
    f.node(772,778,328,133,'复验与工程产物',['原生检查 · ECO · 台账','报告 · 受控制造包'],color='teal')
    f.edge([(847,701),(847,740),(231,740),(231,778)],'teal')
    f.edge([(579,740),(579,778)],'teal');f.edge([(847,740),(936,740),(936,778)],'teal')
    f.node(76,966,1024,76,'原生工具适配：KiCad CLI / pcbnew  ⇄  Freerouting (Java)',[],color='teal',fill='tfill')
    f.edge([(579,911),(579,966)],'teal');f.edge([(936,911),(936,966)],'teal')
    f.node(1192,390,492,108,'独立 stdio MCP',['scripts/altium_service_mcp.py'],color='amber')
    f.edge([(558,205),(582,205),(582,304),(1697,304),(1697,368),(1438,368),(1438,390)],'amber',label='独立服务条目',lx=1480,ly=296)
    f.db(1220,550,436,115,'独立 SQLite 任务库',['jobs.sqlite3 · 输入指纹'],color='amber')
    f.edge([(1438,498),(1438,550)],'amber')
    f.node(1192,723,492,133,'Altium Worker',['领取 / 续租 · 执行前复核指纹','过期 running → blocked'],color='amber')
    f.edge([(1438,665),(1438,723)],'amber')
    f.node(1192,914,492,128,'允许目录内的静态检查',['结果 / 日志 · 不回写原工程','公开启动脚本禁用原生启动'],color='amber')
    f.edge([(1438,856),(1438,914)],'amber')
    f.panel(48,1130,1080,150,'C  KiCad 数据与证据',color='teal',fill='tfill')
    f.text('ledger.sqlite3  ·  revisions / jobs / events',76,1210,22,'teal',600)
    f.text('design + constraints + SHA-256  →  verification  →  release manifest',76,1250,21)
    f.edge([(588,1070),(588,1130)],'teal',True,label='记录版本、事件与产物',lx=610,ly=1108)
    f.panel(1164,1130,548,150,'边界：两套任务与数据域',color='amber',fill='afill',dash=True)
    f.text('不共享任务库；不存在跨 CAD 统一路由。',1188,1210,21)
    f.text('静态检查结果不代表完整 DRC 或可制造。',1188,1250,21)
    f.footer('实线：调用 / 执行路径；虚线：可选入口或持久化关系。各框为职责分组，不表示都运行在独立进程中。')
    f.save()


def pipeline():
    f=Figure('engineering-pipeline',1120,'02  工程流水线  /  Constraint-to-evidence pipeline',
             '输入是已有工程与结构化约束；输出是新版本、原生检查证据与明确的阻断原因',
             '工程导入为 r0，布局候选应用为 r1，经 DSN、Freerouting、SES 得到待验证 r2。原生复验通过后可显式请求发布并重新检查；失败进入有限修复或人工审查。')
    f.panel(48,160,1664,270,'A  输入与布局',fill='bfill')
    f.node(76,224,348,155,'工程输入',['PCB / 原理图 / 项目规则','本地库 + 结构化约束','已有网络与封装'],num='1')
    f.node(492,224,348,155,'导入与快照',['复制设计 · 编译规则','SHA-256 + parent revision','生成 r₀'],num='2')
    f.node(908,224,348,155,'布局候选',['SLSQP / 分块 / 合法化','度量 + 约束复验','选择可行候选'],num='3')
    f.node(1324,224,360,155,'应用布局',['保存计划与候选 ID','修改副本 · 重新封存','生成 r₁'],num='4')
    for x in [424,840,1256]:f.edge([(x,302),(x+68,302)])
    f.panel(48,492,1664,220,'B  原生布线',color='teal',fill='tfill')
    f.node(76,553,348,115,'KiCad / pcbnew',['导出 board.dsn'],color='teal')
    f.node(492,553,348,115,'Freerouting / Java',['运行路由 · 生成 board.ses'],color='teal')
    f.node(908,553,348,115,'KiCad / pcbnew',['回导 SES · 核对电气身份'],color='teal')
    f.node(1324,553,360,115,'新版本 r₂',['routed_unverified'],color='teal')
    f.edge([(1504,379),(1504,460),(250,460),(250,553)],'teal',label='支持的未布线输入',lx=620,ly=451)
    for x in [424,840,1256]:f.edge([(x,611),(x+68,611)],'teal')
    f.node(76,790,560,172,'原生复验：针对精确 revision',['DRC / ERC（按 require_erc 配置）','网表一致性 · 几何 / 线宽 / 规则覆盖','输出 verification_id + revision_digest'],color='teal',num='5')
    f.edge([(1504,668),(1504,752),(356,752),(356,790)],'teal')
    f.node(744,790,428,172,'显式请求发布',['fresh verification 再检查','制造文件 / BOM-CPL 对照','manifest + 哈希 ZIP'],color='teal',num='6')
    f.edge([(636,851),(744,851)],'teal',label='通过',lx=658,ly=833)
    f.node(1280,790,404,172,'阻断 / 有限恢复',['保留失败阶段与原生日志','受限修复 → 新版本 → 复验','预算耗尽 → 人工审查'],color='red')
    f.edge([(636,923),(690,923),(690,1000),(1482,1000),(1482,962)],'red',label='检查未通过 / 工具不可用',lx=750,ly=994)
    f.edge([(1280,820),(1220,820),(1220,774),(356,774),(356,790)],'amber',True,label='受限修复后重新检查',lx=760,ly=768)
    f.footer('completion 是有候选数、时间和修复次数上限的编排；它不授权制造。布局可行、布线回导、任务完成、制造门禁是不同结论。')
    f.save()


def evidence():
    f=Figure('revision-evidence',1020,'03  版本与证据  /  Revision-bound verification',
             '工程数据身份、检查结果和制造归档三者必须指向同一版本',
             '每个版本绑定设计文件、约束和摘要。修改创建子版本；检查在临时副本上执行，记录版本摘要与报告哈希。制造发布强制重新检查，并记录清单和完整性信息。')
    f.panel(48,164,510,655,'A  版本谱系',fill='bfill')
    f.node(80,232,446,118,'r₀  导入快照',['design/ + constraints.json','revision.json + digest'])
    f.node(80,420,446,118,'r₁  布局 / 约束变更',['parent = r₀','新的文件哈希与约束哈希'])
    f.node(80,608,446,148,'r₂  布线 / 修复结果',['parent = r₁','新的 revision_digest','旧版报告不能替新版放行'])
    f.edge([(303,350),(303,420)],label='创建子版本',lx=322,ly=392)
    f.edge([(303,538),(303,608)],label='创建子版本',lx=322,ly=580)
    f.panel(626,164,510,655,'B  对应版本的原生检查',color='teal',fill='tfill')
    f.node(658,232,446,118,'完整性检查',['重算设计文件与约束哈希','外部修改 → 拒绝继续'],color='teal')
    f.node(658,420,446,118,'临时验证副本',['KiCad 检查 + 网表 / 几何审计','受管理版本不由检查工具改写'],color='teal')
    f.node(658,608,446,148,'verification / v-*',['verification_id + revision_digest','原生报告 + evidence_hashes','passed 或 blocked + reasons'],color='teal')
    f.edge([(526,682),(590,682),(590,292),(658,292)],'teal',label='绑定精确版本',lx=428,ly=852)
    f.edge([(881,350),(881,420)],'teal');f.edge([(881,538),(881,608)],'teal')
    f.panel(1204,164,508,655,'C  显式发布与可验证归档',color='amber',fill='afill')
    f.node(1236,232,444,118,'build_release(r₂)',['强制发起新一轮 _verify','不能使用缓存 pass 标记'],color='amber')
    f.node(1236,420,444,118,'实际制造导出',['Gerber / 钻孔 / 贴装位置','有原理图时核对 BOM 与 DNP'],color='amber')
    f.node(1236,608,444,148,'release-*.zip',['design + verification + manifest','文件 SHA-256 · revision_digest','登记归档；读取时检查完整性'],color='amber')
    f.edge([(1104,682),(1170,682),(1170,292),(1236,292)],'amber',label='请求发布后重新复验',lx=1164,ly=852)
    f.edge([(1458,350),(1458,420)],'amber',label='新鲜检查通过',lx=1475,ly=392)
    f.edge([(1458,538),(1458,608)],'amber')
    f.rect(48,882,1664,42,'light','none',5)
    f.text('digest = SHA256(canonical({ files: 文件哈希映射, constraints: 约束文件哈希 }))',76,911,22,'ink',400,mono=True)
    f.footer('摘要与事件链用于完整性校验和追溯，不是数字签名或第三方认证；检查覆盖不包含 SI/PI、EMC、热、安规及实板验证。')
    f.save()


def runtime():
    f=Figure('runtime-integration',1130,'04  执行与接入  /  Runtime and integration boundaries',
             '会话生命周期 ≠ 作业生命周期；任务与运行配置持久化到同一个 KiCad 工作区',
             'MCP 直接工具同步调用工程服务，长任务写入 SQLite。工作台线程或独立 Worker 单活领取任务，执行后保存结果。机器接口默认关闭，授权不保护其他本机入口。')
    f.panel(48,164,1664,710,'同一受信任工作站  /  trusted local workstation',fill='light',dash=True)
    f.node(80,235,450,128,'AI 客户端 → stdio MCP',['.mcp.json · 独立 Python 进程','server.py 不启动队列 Worker'])
    f.node(80,426,450,128,'浏览器 → HTTP 工作台',['127.0.0.1:8765','platform.py 默认启动队列线程'])
    f.node(80,617,450,182,'本机连接器 → 机器接口',['默认关闭 · Bearer + 工程白名单','写开关 + Idempotency-Key','机器接口不接收任意导入路径','机器令牌不保护 MCP / 工作台'],color='amber')
    f.db(653,414,460,157,'ledger.sqlite3',['jobs / job_events / revisions','请求、运行配置与输入指纹'])
    f.node(653,235,460,128,'EngineeringService',['直接工具同步调用','共享版本、约束和发布门禁'],color='teal')
    f.edge([(530,291),(653,291)],'teal',label='直接工具',lx=544,ly=278)
    f.edge([(305,363),(305,392),(780,392),(780,414)],label='提交持久任务',lx=552,ly=385)
    f.edge([(530,490),(653,490)])
    f.edge([(530,709),(603,709),(603,541),(653,541)],'amber')
    f.node(1220,235,460,182,'单活执行器',['工作台线程 或 worker.py','相同 workspace + config','OS 锁 + 数据库事务领取','阶段边界取消 · 保存进度'],color='teal')
    f.edge([(1113,493),(1166,493),(1166,327),(1220,327)],'teal',label='领取',lx=1169,ly=477)
    f.edge([(1220,277),(1113,277)],'teal')
    f.node(1220,520,460,128,'原生工具子进程',['KiCad / pcbnew / Java','工具链配置 · 超时 · 原生日志'],color='teal')
    f.edge([(1450,417),(1450,520)],'teal',label='执行',lx=1466,ly=473)
    f.node(653,679,460,120,'查询与恢复',['按 job_id 查询；客户端可重连','无 Worker → queued 保持不动'])
    f.edge([(883,571),(883,679)])
    f.node(1220,706,460,93,'产物与阶段证据',['写入 workspace；客户端查询'],color='teal')
    f.edge([(1450,648),(1450,706)],'teal')
    f.edge([(1220,754),(1113,754)],'teal',True)
    f.rect(48,923,1664,103,'bfill','line')
    f.text('KiCad 作业状态',76,962,23,'blue',700)
    f.text('queued → running → completed / blocked / failed / cancelled / interrupted',330,962,21,'ink',400,mono=True)
    f.text('旧 Worker 丢失后的 running → interrupted；不自动重放可能已经产生版本的工程修改。',76,1002,22)
    f.footer('本图是本机执行模型，非分布式集群。SQLite 与单活 Worker 不等于多租户隔离或高可用；MCP 的 stdio 不是工作台 HTTP 地址。')
    f.save()


def altium():
    f=Figure('altium-service',1030,'05  Altium 服务  /  Isolated inspection service',
             '持久任务 · 允许目录 · 文件指纹 · 租约恢复；静态检查与实验原生分支严格区分',
             'MCP 提交时检查路径与文件指纹，任务写入独立 SQLite。Worker 领取、续租、复核指纹后执行静态检查，结果可重连查询。受限原生实验默认被门禁阻断，不提供路由回写和制造放行。')
    f.panel(48,164,1664,394,'A  默认公开内测路径',color='amber',fill='afill')
    f.node(80,236,350,191,'Altium stdio MCP',['允许工程根目录检查','记录显式文档指纹','提交 inspect → 返回 job_id','客户端可断开后重连'],color='amber',num='1')
    f.db(500,251,350,146,'独立 SQLite',['data/altium-service/','jobs.sqlite3'],color='amber')
    f.node(920,236,350,191,'常驻 Worker',['领取 queued → running','租约 / 心跳续期','复核源文件指纹','执行 inspect'],color='amber',num='2')
    f.node(1340,236,340,191,'检查结果',['completed / blocked / failed','文件清单与静态证据','native_executed = false','不修改原始工程'],color='amber',num='3')
    f.edge([(430,325),(500,325)],'amber');f.edge([(850,325),(920,325)],'amber');f.edge([(1270,325),(1340,325)],'amber')
    f.edge([(1510,427),(1510,491),(675,491),(675,397)],'amber',True,label='结果持久化；altium_get_job 重新查询',lx=840,ly=484)
    f.panel(48,625,796,275,'B  恢复规则',color='teal',fill='tfill')
    f.text('没有 Worker',80,704,24,'teal',700);f.text('queued 保持排队',375,704,24)
    f.text('提交后输入改变',80,761,24,'teal',700);f.text('blocked，要求重新提交',375,761,24)
    f.text('running 租约过期',80,818,24,'teal',700);f.text('blocked，不自动重试原生操作',375,818,23)
    f.text('取消范围',80,875,24,'teal',700);f.text('仅 queued；不强杀原生进程',375,875,23)
    f.panel(900,625,812,275,'C  受限原生实验分支',color='red',fill='rfill',dash=True)
    f.text('altium_submit_native_check',932,704,25,'red',600,mono=True)
    f.text('公开 Start-Altium-Service.ps1 要求原生启动开关为 0。',932,754,22)
    f.text('底层实验还检查 Windows、X2.EXE 与 pending 锁。',932,804,22)
    f.text('不提供自动布线、工程回写、全量 DRC 或制造放行。',932,854,22)
    f.footer('此服务不使用 KiCad ledger.sqlite3，不借用 KiCad 的复验或发布结论。历史原生实验文档不代表默认内测入口具备相应能力。')
    f.save()


if __name__ == '__main__':
    for draw in (system, pipeline, evidence, runtime, altium): draw()
    print(f'Generated 5 SVG figures in {OUT}')

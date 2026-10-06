"""Render the persisted Altium beta demo evidence as a Chinese HTML report."""
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
from string import Template

from altium_project_snapshot import ROOT, _read, _sha
from altium_silk_status import snapshot as silk_snapshot

FOLDER = ROOT / 'docs/validation/altium-beta-demo-v1'


def e(value):
    return escape(str(value), quote=True)


def link(href, label, note='打开'):
    return f'<a href="{e(href)}"><span>{e(label)}</span><span>{e(note)} ↗</span></a>'


def metric(value, label, color=''):
    return f'<div class="metric {color}"><span class="value">{e(value)}</span><span class="label">{e(label)}</span></div>'


def fmt_time(value):
    moment = datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8)))
    return moment.strftime('%Y-%m-%d %H:%M:%S CST')


def build():
    summary = json.loads(_read(FOLDER / 'summary.json'))
    manifest = json.loads(_read(FOLDER / 'example-manifest.json'))
    configuration = json.loads(_read(FOLDER / 'configuration.json'))
    steps = [json.loads(_read(FOLDER / row['file'])) for row in summary['steps']]
    history = silk_snapshot()
    if (summary['status'] != 'passed' or len(steps) != 5 or
            [s['tool'] for s in steps] != ['altium_beta_status', 'altium_beta_inspect',
                                          'altium_beta_check', 'altium_beta_report', 'altium_beta_status']):
        raise ValueError('Demo sequence incomplete')
    check, report = steps[2]['response'], steps[3]['response']
    run_id = summary['beta_run_id']
    native = ROOT / 'docs/validation/altium-beta' / run_id
    if (check['run_id'] != run_id or report['run_id'] != run_id or not check['native_compile']
            or not check['selected_rules_pass'] or check['full_drc_pass']
            or check['drc_counts']['violations'] != 0 or check['drc_counts']['rule_rows'] != 6
            or history['after_violations'] != 154 or _sha(_read(Path(manifest['board']))) != _sha(_read(Path(history['board'])))):
        raise ValueError('Demo result or historical board identity mismatch')
    for row in manifest['files']:
        if _sha(_read(Path(row['source']))) != row['source_sha256'] or _sha(_read(Path(row['copy']))) != row['copy_sha256']:
            raise ValueError('Example file hash mismatch')
    if _sha(_read(native / 'manifest.json')) != check['manifest_sha256'] or _sha(_read(native / 'native-drc.html')) != check['report_sha256']:
        raise ValueError('Native evidence changed')
    if not (FOLDER / 'board-preview-final.png').is_file():
        raise ValueError('Native preview image missing')

    rel_project = '../altium-redesign/developer-demo-v1/'
    rel_native = f'../altium-beta/{run_id}/'
    rel_history = '../altium-redesign/ba00d483efd34f16a50205b6a3e08c08/'
    project_facts = '<dl class="facts">' + ''.join(
        f'<dt>{e(k)}</dt><dd>{v}</dd>' for k, v in [
            ('工程文件', f'<a href="{e(rel_project + "Controller_Beta_Demo.PrjPcb")}">Controller_Beta_Demo.PrjPcb</a>'),
            ('原理图', 'Connector_WiFi.SchDoc'), ('PCB', 'WiFi.PcbDoc'),
            ('项目规模', '28 个器件 / 200 个焊盘 / 579 条线段（先前原生清单）'),
            ('板图摘要', f'<code>{e(summary["historical_comparison_board_sha256"])}</code>'),
            ('检查副本', f'<code>{e(run_id)}</code>'),
            ('用途', '<span class="pill warn">内测工程</span>')
        ]) + '</dl>'

    source_rows = ''.join(f'<tr><td>{e(Path(row["copy"]).name)}</td><td>{row["bytes"]:,} B</td>'
                          f'<td class="hash">{e(row["copy_sha256"])}</td><td><span class="pill">一致</span></td></tr>'
                          for row in manifest['files'])
    env = configuration['env']
    config_items = [
        ('服务器名称', 'altium-developer-beta'), ('启动方式', 'stdio / Python'),
        ('Python', configuration['command']), ('入口脚本', configuration['args'][0]),
        ('Altium 程序', env['ALTIUM_BETA_EXE']),
        ('允许的工程目录', env['ALTIUM_BETA_PROJECT_ROOTS']),
        ('运行证据目录', env['ALTIUM_BETA_RUN_ROOT']),
        ('本次工程路径', manifest['project']),
        ('本次检查选项', 'run_drc = true；单 PCB 工程，无需 board_path')]
    config_rows = ''.join(
        f'<tr><th>{e(k)}</th><td class="mono">'
        f'{"<br>".join(e(path) for path in v.split(";")) if k == "允许的工程目录" else e(v)}'
        '</td></tr>' for k, v in config_items)
    descriptions = [
        '确认本机 Altium 程序存在、工程根目录可访问、没有残留任务锁。',
        '解析工程与两个直接引用的文档，读取文件大小和摘要。',
        '复制工程，在 Altium 中原生编译并执行当前启用的批量 DRC。',
        '以运行编号复读并校验报告，分页取得违规明细。',
        '检查结束后再次确认任务锁已经释放。']
    brief = [
        '<span class="pill">可用 / 空闲</span>',
        '<span class="pill">3 个文件摘要一致</span>',
        '<span class="pill">编译通过</span> <span class="pill warn">仅 6 类规则</span>',
        '<span class="pill">0 条当前范围违规</span>',
        '<span class="pill">空闲 / 可继续请求</span>']
    step_html = []
    for i, row in enumerate(steps):
        n = i + 1
        raw = json.dumps({'arguments': row['arguments'], 'response': row['response']}, ensure_ascii=False, indent=2)
        step_html.append(f'<article class="step"><div class="step-index">{n:02d}</div><div>'
                         f'<div class="step-head"><h3>{e(row["tool"])}</h3><span>{row["duration_ms"]:,} ms</span>{brief[i]}</div>'
                         f'<p>{e(descriptions[i])}</p>'
                         f'<details><summary>查看请求与完整响应</summary><pre>{e(raw)}</pre>'
                         f'<a href="{e(summary["steps"][i]["file"])}">打开第 {n} 步 JSON 证据</a></details>'
                         '</div></article>')
    rule_rows = ''.join(f'<tr><td>{e(row["description"])}</td><td>{row["count"]}</td></tr>'
                        for row in check['drc_rule_rows'])
    historical = [row for row in history['remaining_rules'] if row['count']]
    if sum(row['count'] for row in historical) != 154:
        raise ValueError('Historical DRC summary mismatch')
    history_rows = ''.join(f'<tr><td>{e(row["description"])}</td><td>{row["count"]}</td></tr>'
                           for row in historical)
    delivery = [
        (rel_project + 'Controller_Beta_Demo.PrjPcb', '示例 Altium 工程', '打开工程'),
        (rel_project + 'Connector_WiFi.SchDoc', '原理图文件', '打开文件'),
        (rel_project + 'WiFi.PcbDoc', 'PCB 文件', '打开文件'),
        (rel_project + 'demo-manifest.json', '示例复制摘要', '查看证据'),
        ('configuration.json', '本次 MCP 配置', '查看配置'),
        ('00-session.json', 'MCP 握手与工具清单', '查看证据'),
        *[(row['file'], f'第 {row["step"]} 步：{row["tool"]}', '查看 JSON') for row in summary['steps']],
        (rel_native + 'manifest.json', '原生运行副本清单', '查看证据'),
        (rel_native + 'response.ini', 'Altium 原生响应', '查看证据'),
        (rel_native + 'native-drc.html', '本轮原生 DRC 报告', '查看报告'),
        (rel_history + 'native-drc.html', '先前完整检查报告（154 项）', '查看报告'),
        ('board-preview-final.png', '真实 PCB 预览图', '查看图片'),
        ('summary.json', '最终验收摘要', '查看 JSON')]
    for href, _, _ in delivery:
        if not (FOLDER / href).resolve().is_file():
            raise ValueError('Missing delivered file: ' + href)
    html = Template(_read(ROOT / 'scripts/altium/report.template.html').decode('utf-8')).substitute(
        run_id=e(run_id), generated_at=e(fmt_time(summary['ended_utc'])),
        metrics=''.join([metric('4 / 4', 'MCP 工具完成', 'good'),
                         metric('通过', 'Altium 原生编译', 'good'),
                         metric('6 类 · 0 项', '本次所选规则结果', 'good'),
                         metric('154 项', '同板历史完整检查', 'warn')]),
        project_facts=project_facts, source_rows=source_rows, config_rows=config_rows,
        steps=''.join(step_html), rule_rows=rule_rows, history_rows=history_rows,
        board_hash=e(summary['historical_comparison_board_sha256']),
        deliverables=''.join(link(*item) for item in delivery))
    output = FOLDER / 'report.html'
    output.write_text(html, encoding='utf-8')
    return {'report': str(output), 'bytes': output.stat().st_size,
            'steps': len(steps), 'delivered_links': len(delivery)}


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False))

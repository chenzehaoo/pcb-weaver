"""Self-contained, escaped engineering review artifacts."""
from html import escape
import json


def board_svg(board):
    bounds = board["outline"].get("bounds")
    if not bounds:
        return '<p>Unsupported board outline. Inspect the source in KiCad.</p>'
    x, y, right, bottom = bounds
    width, height = right - x, bottom - y
    parts = [f'<svg role="img" aria-label="PCB geometry" viewBox="{x-4} {y-4} {width+8} {height+8}" xmlns="http://www.w3.org/2000/svg">',
             f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="#153c35" stroke="#6ca396" stroke-width=".25"/>']
    colors = {"F.Cu": "#eb947b", "B.Cu": "#7ac9e7", "In1.Cu": "#d1b45e", "In2.Cu": "#80c99b",
              "In3.Cu": "#c58ba9", "In4.Cu": "#77c8bd", "In5.Cu": "#bdbd7f", "In6.Cu": "#bd9990"}
    for track in board.get("track_items", []):
        a, b = track["start"], track["end"]
        color = colors.get(track.get("layer"), "#a8beb0")
        parts.append(f'<path d="M {a[0]} {a[1]} L {b[0]} {b[1]}" fill="none" stroke="{color}" stroke-width="{track["width_mm"]}"/>')
    for via in board.get("via_items", []):
        parts.append(f'<circle cx="{via["x"]}" cy="{via["y"]}" r="{via["diameter_mm"]/2}" fill="#dac992"/>')
        parts.append(f'<circle cx="{via["x"]}" cy="{via["y"]}" r="{via["drill_mm"]/2}" fill="#122c28"/>')
    for fp in board["footprints"]:
        a, b, c, d = fp["bounds"]
        parts.append(f'<g><title>{escape(fp["reference"] + ": " + fp["value"])}</title><rect x="{a}" y="{b}" width="{c-a}" height="{d-b}" fill="none" stroke="#9aada2" stroke-opacity=".5" stroke-width=".12"/>')
        for pad in fp["pads"]:
            sx, sy = pad["size"]
            shape = pad["shape"]
            parts.append(f'<g transform="translate({pad["x"]} {pad["y"]}) rotate({-pad.get("rotation", 0)})" fill="#dbbd75"><title>{escape(pad["number"] + ": " + pad["net"])}</title>')
            if shape == "circle":
                parts.append(f'<ellipse rx="{sx/2}" ry="{sy/2}"/>')
            else:
                radius = min(sx, sy) * (0.5 if shape == "oval" else pad.get("roundrect_rratio", 0.25) if shape == "roundrect" else 0)
                parts.append(f'<rect x="{-sx/2}" y="{-sy/2}" width="{sx}" height="{sy}" rx="{radius}"/>')
            if pad.get("drill_size"):
                dx, dy = pad["drill_size"]
                parts.append(f'<rect x="{-dx/2}" y="{-dy/2}" width="{dx}" height="{dy}" rx="{min(dx,dy)/2}" fill="#122c28"/>')
            elif pad.get("drill_mm"):
                parts.append(f'<circle r="{pad["drill_mm"]/2}" fill="#122c28"/>')
            parts.append('</g>')
        if len(board["footprints"]) <= 32 or (c-a)*(d-b) > 100:
            parts.append(f'<text x="{fp["x"]}" y="{b-0.9}" text-anchor="middle" fill="#e6eeea" font-size="1.3" font-family="monospace">{escape(fp["reference"])}</text>')
        parts.append('</g>')
    parts.append('</svg>')
    return "".join(parts)


def build_report(engine, project, revision):
    inspection = engine.inspect_revision(project, revision)
    board = inspection["board"]
    folder = engine.store.revision_dir(project, revision)
    from .catalog import verification as verified_evidence
    current = verified_evidence(engine, project, revision)
    verification = None if current["status"] == "not_verified" else current
    state = verification["status"] if verification else "not_verified"
    labels = {"passed": "工程检查通过", "blocked": "检查未通过", "not_verified": "尚未验证", "invalid_evidence": "验证证据已变化"}
    components = "".join('<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in [f["reference"], f["value"], f["footprint"], f'{f["x"]:.3f}, {f["y"]:.3f}', f["rotation"], "锁定" if f["locked"] or f["reference"] in inspection["constraints"]["fixed_references"] else "可调整"]) + '</tr>' for f in board["footprints"])
    reasons = (verification or {}).get("reasons", []) + board["unsupported"]
    empty_reason = '当前记录无阻断项。检查覆盖范围见下方。' if verification else '尚未执行工程检查，当前版本不能据此放行。'
    reasons_html = ''.join(f'<li>{escape(str(r))}</li>' for r in reasons) or f'<li>{empty_reason}</li>'
    revisions = engine.store.list_revisions(project)
    revision_rows = ''.join(f'<tr><td>{escape(r["id"])}</td><td>{escape(r["operation"])}</td><td>{escape(r.get("parent") or "起始版本")}</td><td>{escape(r["digest"][:16])}</td></tr>' for r in revisions)
    check_rows = []
    for name, key in [("PCB 设计规则", "drc"), ("原理图电气规则", "erc"), ("原理图与板连接一致性", "connectivity"), ("布局与制造约束", "constraints"), ("保留的逐网络线宽下限", "persisted_track_minima")]:
        check = (verification or {}).get(key, {})
        check_rows.append(f'<tr><td>{name}</td><td>{escape(str(check.get("status", "not_run")))}</td><td>{escape(str(check.get("errors", "-")))}</td><td>{escape(str(check.get("warnings", "-")))}</td></tr>')
    body = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PCB Weaver · {escape(project)}</title><style>
*{{box-sizing:border-box;letter-spacing:0}}body{{margin:0;color:#202b30;background:#f5f7f8;font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}}
header{{background:#182427;color:#f4f6f5;padding:22px 5%;display:flex;align-items:center;justify-content:space-between;gap:20px}}header strong{{font-size:20px}}header span{{color:#a7bab8;font:12px monospace}}
main{{max-width:1320px;margin:0 auto;padding:32px 28px}}h1{{font-size:28px;margin:0 0 5px}}h2{{font-size:17px;margin:0 0 16px}}p{{margin:8px 0;color:#5d6b72}}.intro{{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:26px}}.badge{{font-size:13px;font-weight:600;color:#94651b;background:#f7eacb;padding:7px 12px;border-radius:4px;white-space:nowrap}}.passed{{color:#14604d;background:#dbefe7}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);background:#fff;border-top:1px solid #d9e1e4;border-bottom:1px solid #d9e1e4;padding:18px 0;margin-bottom:30px}}.metric{{padding:0 24px;border-right:1px solid #e4e9eb}}.metric:last-child{{border:0}}.metric b{{display:block;font:28px/1.4 monospace}}.metric span{{color:#6d7c83;font-size:12px}}
.overview{{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:30px;padding-bottom:30px;border-bottom:1px solid #dce3e6}}.board{{background:#102c28;min-height:300px;display:flex;align-items:center;justify-content:center}}.board svg{{width:100%;max-height:420px;display:block}}section{{padding:28px 0;border-bottom:1px solid #dce3e6}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:11px 12px;border-bottom:1px solid #e0e6e9;text-align:left;overflow-wrap:anywhere}}th{{color:#718088;font-weight:500;background:#edf1f3}}.scroll{{overflow:auto}}.scroll table{{min-width:620px}}ul{{padding-left:20px;color:#685438}}li{{margin:8px 0}}code{{font-family:monospace;font-size:12px;overflow-wrap:anywhere}}details{{margin:14px 0}}summary{{cursor:pointer;font-weight:600}}pre{{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;background:#eaf0f2;padding:18px;max-height:500px;overflow:auto}}footer{{padding:24px 0;font-size:12px;color:#6c7b81}}
@media(max-width:720px){{header{{padding:18px 20px}}header span{{display:none}}main{{padding:22px 18px}}.intro{{align-items:flex-start;flex-direction:column;gap:10px}}.overview{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(2,1fr);gap:16px 0}}.metric:nth-child(2){{border:0}}h1{{font-size:24px}}.board{{min-height:260px}}}}@media print{{body{{background:white}}details{{display:block}}main{{padding:10px}}}}
</style></head><body><header><strong>PCB WEAVER</strong><span>ENGINEERING REVIEW / v0.2</span></header><main>
<div class="intro"><div><h1>{escape(project)}</h1><p>设计版本 <code>{escape(revision)}</code></p></div><span class="badge {"passed" if state == "passed" else ""}">{labels.get(state, escape(state))}</span></div>
<div class="metrics">{''.join(f'<div class="metric"><b>{value}</b><span>{label}</span></div>' for label,value in [("器件",len(board["footprints"])),("网络",len(board["nets"])),("走线段",board["tracks"]),("过孔",board["vias"])])}</div>
<div class="overview"><div><h2>板级几何</h2><div class="board">{board_svg(board)}</div><p>来源：当前版本的实际板文件。包络和焊盘为审阅简图，以 KiCad 为最终几何视图。</p></div><div><h2>工程检查</h2><table><thead><tr><th>检查项</th><th>状态</th><th>错误</th><th>警告</th></tr></thead><tbody>{''.join(check_rows)}</tbody></table><ul>{reasons_html}</ul></div></div>
<section><h2>器件清单</h2><div class="scroll"><table><thead><tr><th>位号</th><th>值</th><th>封装</th><th>位置 / mm</th><th>角度</th><th>布局状态</th></tr></thead><tbody>{components}</tbody></table></div></section>
<section><h2>版本追踪</h2><div class="scroll"><table><thead><tr><th>版本</th><th>操作</th><th>父版本</th><th>摘要</th></tr></thead><tbody>{revision_rows}</tbody></table></div></section>
<section><h2>验证依据</h2><p>设计摘要 <code>{inspection["revision"]["digest"]}</code></p><details><summary>设计约束</summary><pre>{escape(json.dumps(inspection["constraints"],ensure_ascii=False,indent=2))}</pre></details><details><summary>完整检查结果</summary><pre>{escape(json.dumps(verification or {"status":"not_verified"},ensure_ascii=False,indent=2))}</pre></details></section>
<footer>检查覆盖：声明的几何约束、线宽与孔径、KiCad ERC/DRC、原理图与 PCB 网络一致性。未覆盖 SI/PI、热、EMC、认证和实板测试。本报告不替代工程签核。</footer>
</main></body></html>'''
    path = folder / "review.html"
    path.write_text(body, encoding="utf-8")
    return {"status": "generated", "path": str(path), "verification_status": state}

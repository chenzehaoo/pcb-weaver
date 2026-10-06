"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icons = () => window.lucide?.createIcons();
const state = {
  project: null,
  revision: null,
  revisions: [],
  inspection: null,
  tab: [...document.querySelectorAll("[data-tab]")].some((el) => el.dataset.tab === new URLSearchParams(location.search).get("tab"))
    ? new URLSearchParams(location.search).get("tab") : "checks",
  objectMode: "components",
  search: "",
  selected: null,
  selectedNet: null,
  finding: null,
  jobs: [],
  presets: [],
  watched: new Set(),
  layers: new Set(),
  pan: { x: 0, y: 0 },
  scale: 1,
  fitScale: 1,
  pending: false,
};
window.pcbWorkbench = state;
const labels = {
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  blocked: "已阻断",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  passed: "通过",
  not_verified: "未验证",
  invalid_evidence: "证据变化",
  ok: "已执行",
  released: "已发布",
  repaired: "已修复",
  improved: "已有改善",
  rejected: "已拒绝",
  review_required: "待审查",
  repair_candidate: "修复候选",
};
const operations = {
  pipeline: "工程流程",
  plan: "布局规划",
  apply: "应用布局",
  route: "自动布线",
  repair: "局部修复",
  auto_repair: "自动修复",
  reference_repair: "参考版局部重布",
  complete: "自动完成",
  clearance: "间距修复",
  repair_candidate: "修复候选",
  verify: "工程检查",
  release: "制造发布",
  report: "审阅报告",
  constraints: "约束更新",
  import: "导入工程",
};
const badge = (s) =>
  `<span class="state ${esc(s)}">${esc(labels[s] || s)}</span>`;
let toastTimer,
  modalHandler,
  modalJob,
  pollBusy = false;
let projectLoad = 0,
  revisionLoad = 0,
  panelLoad = 0;
let modalEpoch = 0, repairTarget = null;
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 6000);
}
async function api(path, data) {
  const response = await fetch(
    "/api" + path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-PCB-Client": "workbench",
          },
          body: JSON.stringify(data),
        },
  );
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
const projectUrl = () => `/projects/${encodeURIComponent(state.project)}`;
const revisionUrl = () =>
  `${projectUrl()}/revisions/${encodeURIComponent(state.revision)}`;
function dialog(title, html, onSubmit, submit = "确认") {
  ++modalEpoch;
  repairTarget = null;
  modalJob = null;
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = html;
  $("#dialog-submit").textContent = submit;
  $("#dialog-submit").hidden = !onSubmit;
  $("#dialog-submit").disabled = false;
  $("#dialog-cancel").textContent = onSubmit ? "取消" : "关闭";
  modalHandler = onSubmit;
  icons();
  if (!$("#dialog").open) $("#dialog").showModal();
}
function closeDialog() {
  ++modalEpoch;
  repairTarget = null;
  modalHandler = null;
  $("#dialog").close();
  modalJob = null;
}
$("#dialog-close").onclick = closeDialog;
$("#dialog-cancel").onclick = closeDialog;
$("#dialog").oncancel = (event) => {
  event.preventDefault();
  closeDialog();
};
$("#dialog-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!modalHandler || $("#dialog-submit").disabled) return;
  const epoch = modalEpoch, handler = modalHandler;
  const button = $("#dialog-submit");
  button.disabled = true;
  try {
    await handler(new FormData(event.target));
    if (epoch === modalEpoch) closeDialog();
  } catch (error) {
    toast(error.message);
  } finally {
    if (epoch === modalEpoch) button.disabled = false;
  }
};
async function submit(request) {
  const job = await api("/jobs", request);
  state.watched.add(job.id);
  state.tab = "jobs";
  setTabs();
  toast(`${operations[request.operation] || "工程任务"}已入队`);
  await poll();
  return job;
}
function openImport(preset = null) {
  const intent = preset?.constraints || {
    schema_version: 1,
    board: {
      layers: 4,
      max_voltage: 24,
      edge_clearance_mm: 1,
      component_gap_mm: 0.3,
    },
    fixed_references: [],
    critical_nets: [],
    proximity: [],
    regions: [],
    net_rules: [],
    fabrication: {
      min_track_mm: 0.2,
      min_clearance_mm: 0.15,
      min_via_drill_mm: 0.3,
    },
    release: { require_erc: true },
  };
  dialog(
    "导入工程",
    `<label>工程标识<input name="project" value="${esc(preset?.id || "")}" required pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}" placeholder="system-controller"></label><label>KiCad 板文件<input name="board_path" value="${esc(preset?.board_path || "")}" required placeholder="C:\\Projects\\controller\\controller.kicad_pcb"></label><label>设计约束<textarea name="constraints" spellcheck="false">${esc(JSON.stringify(intent, null, 2))}</textarea></label><label class="check-option"><input name="route" type="checkbox" checked>应用布局后自动布线与验证</label><label class="check-option"><input name="release" type="checkbox">验证通过后生成制造包</label>`,
    async (form) => {
      await submit({
        operation: "pipeline",
        project: form.get("project"),
        board_path: form.get("board_path"),
        constraints: JSON.parse(form.get("constraints")),
        route: form.has("route"),
        release: form.has("release"),
        passes: 20,
      });
    },
    "导入并运行",
  );
}
$("#new-project").onclick = () => openImport();
$("#empty-import").onclick = () => openImport();
async function loadProjects(auto = false) {
  const list = await api("/projects");
  $("#projects").innerHTML = list.length
    ? list
        .map(
          (item) =>
            `<button class="project-item ${item.project === state.project ? "active" : ""}" data-project="${esc(item.project)}"><strong>${esc(item.project)}</strong><small>${item.revision_count} 个版本 · ${esc(item.updated.slice(5, 16).replace("T", " "))}</small></button>`,
        )
        .join("")
    : '<div class="panel-empty">暂无工程</div>';
  if (auto && list.length && !state.project) {
    const query = new URLSearchParams(location.search);
    const project = query.get("project");
    if (project && !list.some((item) => item.project === project)) {
      toast("未找到链接指定的工程");
      return;
    }
    await loadProject(
      project || list[0].project,
      project ? query.get("revision") : null,
    );
  }
}
async function loadProject(project, revision = null) {
  if (repairTarget) closeDialog();
  const loading = ++projectLoad;
  ++revisionLoad;
  clearInventory();
  state.project = project;
  state.revision = null;
  state.inspection = null;
  renderRevisionStatus();
  $("canvas").style.visibility = "hidden";
  $$(".project-actions button,#constraints").forEach(
    (b) => (b.disabled = true),
  );
  const revisions = await api(projectUrl() + "/revisions");
  if (loading !== projectLoad) return;
  state.revisions = revisions;
  if (revision && !revisions.some((item) => item.id === revision))
    throw new Error("未找到链接指定的工程版本");
  state.revision = revision || state.revisions.at(-1)?.id;
  $("#project-title").textContent = project;
  $("#revision-select").innerHTML = [...state.revisions]
    .reverse()
    .map(
      (r) =>
        `<option value="${esc(r.id)}" ${r.id === state.revision ? "selected" : ""}>${esc(operations[r.operation] || r.operation)} · ${esc(r.id.slice(-8))}</option>`,
    )
    .join("");
  await loadRevision();
  await loadProjects();
}
async function loadRevision() {
  if (repairTarget) closeDialog();
  if (!state.revision) return;
  const loading = ++revisionLoad;
  clearInventory();
  state.inspection = null;
  renderRevisionStatus();
  $("canvas").style.visibility = "hidden";
  $$(".project-actions button,#constraints").forEach(
    (b) => (b.disabled = true),
  );
  const inspection = await api(revisionUrl());
  if (loading !== revisionLoad) return;
  state.inspection = inspection;
  renderRevisionStatus();
  $("canvas").style.visibility = "visible";
  state.selected = null;
  state.selectedNet = null;
  state.finding = null;
  state.layers = new Set(
    state.inspection.board.copper_layers || ["F.Cu", "B.Cu"],
  );
  $("#empty-board").hidden = true;
  renderLayers();
  renderObjects();
  fit();
  renderMetrics();
  await renderPanel();
  if (loading !== revisionLoad) return;
  const address = new URL(location.href);
  address.searchParams.set("project", state.project);
  address.searchParams.set("revision", state.revision);
  address.searchParams.set("tab", state.tab);
  history.replaceState(null, "", address);
  $$(".project-actions button,#constraints").forEach(
    (b) => (b.disabled = false),
  );
  const hasCopper = state.inspection.board.tracks > 0 || state.inspection.board.vias > 0;
  $("#run-completion").disabled = hasCopper;
  $("#run-completion").title = hasCopper ? "已有走线的版本不执行整板布局布线" : "自动完成布局布线";
}
function renderRevisionStatus() {
  const panel = $("#revision-status");
  panel.hidden = !state.inspection;
  if (!state.inspection) {
    panel.replaceChildren();
    delete panel.dataset.revision;
    return;
  }
  const v = state.inspection.verification;
  const status = v?.status || "not_verified";
  const title = status === "passed" ? "工程检查通过" : `工程检查：${labels[status] || status}`;
  panel.dataset.revision = state.revision;
  panel.innerHTML = `<div class="revision-status-heading"><strong class="state ${esc(status)}">${esc(title)}</strong><code>${esc(state.revision)}</code><button class="icon" id="show-revision-checks" title="查看检查结果" aria-label="查看检查结果"><i data-lucide="clipboard-check"></i></button></div>
    <div class="revision-status-metrics"><span>未连接 <b>${esc(v?.drc?.unconnected ?? "未知")}</b></span><span>DRC 错误 <b>${esc(v?.drc?.errors ?? "未知")}</b></span><span>ERC 错误 <b>${esc(v?.erc?.errors ?? "未知")}</b></span><span>警告 DRC ${esc(v?.drc?.warnings ?? "未知")} / ERC ${esc(v?.erc?.warnings ?? "未知")}</span><span>制造签核未评估</span></div>`;
  const option = [...$("#revision-select").options].find((o) => o.value === state.revision);
  if (option) option.textContent = `${labels[status] || status} · ${state.revision.slice(-8)}`;
  $("#show-revision-checks").onclick = () => {
    state.tab = "checks";
    setTabs();
  };
  icons();
}
$("#projects").onclick = (event) => {
  const button = event.target.closest("[data-project]");
  if (button)
    loadProject(button.dataset.project).catch((e) => toast(e.message));
};
$("#revision-select").onchange = (event) => {
  state.revision = event.target.value;
  loadRevision().catch((e) => toast(e.message));
};
$("#refresh").onclick = () =>
  loadProjects(true)
    .then(() => state.revision && loadRevision())
    .catch((e) => toast(e.message));
$("#constraints").onclick = () => {
  if (!state.inspection) return;
  const target = { project: state.project, revision: state.revision };
  dialog(
    "设计约束",
    `<label>约束版本<textarea name="constraints" spellcheck="false">${esc(JSON.stringify(state.inspection.constraints, null, 2))}</textarea></label>`,
    async (form) =>
      submit({
        operation: "constraints",
        ...target,
        constraints: JSON.parse(form.get("constraints")),
      }),
    "创建约束版本",
  );
};
$("#run-plan").onclick = () => openRun("plan");
$("#run-route").onclick = () => openRun("route");
$("#run-repair").onclick = openRepair;
$("#run-auto-repair").onclick = () => {
  if (!state.inspection || !state.revision) return;
  const target = {project:state.project,revision:state.revision};
  dialog("自动修复", `<div class="dialog-data"><strong>${esc(target.project)}</strong><p><code>${esc(target.revision)}</code></p></div>
    <div class="form-grid"><label>最多尝试次数<input name="attempts" type="number" min="1" max="24" step="1" value="6" required></label>
    <label>调度预算 / 秒<input name="budget" type="number" min="30" max="1800" step="1" value="600" required></label></div>
    <label class="check-option"><input type="checkbox" name="neckdown">允许短缩颈，遵守现有最小线宽</label>
    <label class="check-option"><input type="checkbox" name="adjustment">允许局部扇出调整，保留线宽和孔径</label>
    <label class="check-option"><input type="checkbox" name="multinet">无参考多网络拆线重布</label>
    <div id="multinet-bounds" class="form-grid" style="display:none">
    <label>最大重布面积 / mm²<input name="region-area" type="number" min="0.01" max="2500" step="any" value="900" required disabled></label>
    <label>单次求解时限 / 秒<input name="proposal-timeout" type="number" min="1" max="180" step="1" value="60" required disabled></label></div>
    <button type="button" id="reference-repair-open"><i data-lucide="git-compare-arrows"></i>参考版局部重布</button>`, async (form) => {
      if (state.project !== target.project || state.revision !== target.revision) throw new Error("工程版本已变化，请重新选择修复目标");
      await submit({operation:"auto_repair",...target,auto_options:{max_attempts:Number(form.get("attempts")),
        time_budget_seconds:Number(form.get("budget")),allow_neckdown:form.has("neckdown"),allow_local_adjustment:form.has("adjustment"),
        ...(form.has("multinet") ? {allow_multinet:true,max_region_area_mm2:Number(form.get("region-area")),
          proposal_timeout_seconds:Number(form.get("proposal-timeout"))} : {})}});
    }, "开始自动修复");
  $("#reference-repair-open").onclick = () => openReferenceRepair(target).catch(e => toast(e.message));
  $('[name="multinet"]').onchange = event => {
    $("#multinet-bounds").style.display = event.target.checked ? "" : "none";
    $$("#multinet-bounds input").forEach(input => { input.disabled = !event.target.checked; });
  };
};
async function openReferenceRepair(target) {
  const openingEpoch = modalEpoch;
  const projects = await api("/projects");
  if (modalEpoch !== openingEpoch || !$("#dialog").open) return;
  if (state.project !== target.project || state.revision !== target.revision) throw new Error("工程版本已变化");
  let available = [], generation = 0;
  dialog("参考版局部重布", `<div class="dialog-data"><strong>${esc(target.project)}</strong><p><code>${esc(target.revision)}</code></p></div>
    <label>参考工程<select name="reference-project" required><option value="">未选择</option>${projects.map(p=>`<option value="${esc(p.project)}">${esc(p.project)}</option>`).join("")}</select></label>
    <label>参考版本<select name="reference-revision" required disabled><option value="">未选择</option></select></label>`, async form => {
      if (state.project !== target.project || state.revision !== target.revision) throw new Error("工程版本已变化，请重新选择目标");
      const reference_project = form.get("reference-project"), reference_revision = form.get("reference-revision");
      if (!reference_project || !available.includes(reference_revision)) throw new Error("请选择已加载的参考版本");
      await submit({operation:"reference_repair",...target,reference_project,reference_revision});
    }, "开始局部重布");
  const epoch = modalEpoch, projectSelect = $('[name="reference-project"]'), revisionSelect = $('[name="reference-revision"]');
  projectSelect.onchange = async () => {
    const selected = projectSelect.value, request = ++generation;
    available = [];
    revisionSelect.disabled = true;
    revisionSelect.innerHTML = '<option value="">未选择</option>';
    if (!selected) return;
    try {
      const revisions = await api(`/projects/${encodeURIComponent(selected)}/revisions`);
      if (request !== generation || modalEpoch !== epoch || projectSelect.value !== selected) return;
      available = revisions.map(r=>r.id);
      revisionSelect.innerHTML = '<option value="">未选择</option>'+[...revisions].reverse().map(r=>`<option value="${esc(r.id)}">${esc(operations[r.operation] || r.operation)} · ${esc(r.id)}</option>`).join("");
      revisionSelect.disabled = false;
    } catch (error) {
      if (request === generation && modalEpoch === epoch) toast(error.message);
    }
  };
}
$("#run-verify").onclick = () => openRun("verify");
$("#run-completion").onclick = () => {
  if (!state.inspection || !state.revision) return;
  const target = {project:state.project,revision:state.revision};
  dialog("自动完成布局布线", `<div class="dialog-data"><strong>${esc(target.project)}</strong><p><code>${esc(target.revision)}</code></p></div>
    <label>布局模式<select name="placement_mode"><option value="optimize">优化布局</option><option value="preserve">保留现有布局</option></select></label>
    <div class="form-grid"><label>布局候选数<input name="candidates" type="number" min="1" max="5" value="3" step="1" required></label>
    <label>布线轮数<input name="passes" type="number" min="1" max="100" value="20" step="1" required></label>
    <label>调度预算 / 秒<input name="budget" type="number" min="60" max="7200" value="1800" step="1" required></label>
    <label>布局变体步长 / mm<input name="spread" type="number" min="0" max="2" value="0.5" step="0.1" required></label>
    <label>布线策略<select name="routing_policy"><option value="strict">严格线宽</option><option value="normalize_widths">回导加宽与间距复验</option></select></label></div>
    <label id="completion-cycles" style="display:none">修复轮数<input name="repair_cycles" type="number" min="1" max="3" step="1" value="1" required disabled></label>
    <label class="check-option"><input name="neckdown" type="checkbox">允许短缩颈，遵守现有最小线宽</label>
    <label class="check-option"><input name="adjustment" type="checkbox">允许局部扇出调整，保留线宽和孔径</label>
    <label class="check-option"><input name="multinet" type="checkbox">无参考多网络拆线重布</label>
    <div id="multinet-bounds" class="form-grid" style="display:none">
    <label>每轮最多尝试<input name="attempts" type="number" min="1" max="24" step="1" value="6" required disabled></label>
    <label>最大重布面积 / mm²<input name="region-area" type="number" min="0.01" max="2500" step="any" value="900" required disabled></label>
    <label>单次求解时限 / 秒<input name="proposal-timeout" type="number" min="1" max="180" step="1" value="60" required disabled></label></div>`, async form => {
      if (state.project !== target.project || state.revision !== target.revision) throw new Error("工程版本已变化，请重新选择目标");
      const preserve = form.get("placement_mode") === "preserve";
      const repairCycles = preserve ? Number(form.get("repair_cycles")) : 1;
      if (!Number.isInteger(repairCycles) || repairCycles < 1 || repairCycles > 3)
        throw new Error("修复轮数必须为 1 至 3 的整数");
      await submit({operation:"complete",...target,completion_options:{
        ...(preserve ? {placement_mode:"preserve",...(repairCycles > 1 ? {repair_cycles:repairCycles} : {})} :
          {candidate_count:Number(form.get("candidates")),placement_spread_mm:Number(form.get("spread"))}),
        route_passes:Number(form.get("passes")),time_budget_seconds:Number(form.get("budget")),routing_policy:form.get("routing_policy"),
        repair:{allow_neckdown:form.has("neckdown"),allow_local_adjustment:form.has("adjustment"),
          ...(form.has("multinet") ? {allow_multinet:true,max_attempts:Number(form.get("attempts")),
            max_region_area_mm2:Number(form.get("region-area")),proposal_timeout_seconds:Number(form.get("proposal-timeout"))} : {})}}});
    }, "开始自动完成");
  $('[name="placement_mode"]').onchange = event => {
    const preserve = event.target.value === "preserve";
    $$('#dialog-body [name="candidates"],#dialog-body [name="spread"]').forEach(input => {
      input.disabled = preserve;
    });
    $("#completion-cycles").style.display = preserve ? "" : "none";
    $('[name="repair_cycles"]').disabled = !preserve;
  };
  $('[name="multinet"]').onchange = event => {
    $("#multinet-bounds").style.display = event.target.checked ? "" : "none";
    $$("#multinet-bounds input").forEach(input => { input.disabled = !event.target.checked; });
  };
};
$("#run-pipeline").onclick = () => openRun("pipeline");
function openRun(operation) {
  if (!state.revision || !state.inspection) return;
  const target = { project: state.project, revision: state.revision };
  dialog(
    operations[operation],
    `<div class="dialog-data"><strong>${esc(state.project)}</strong><p><code>${esc(state.revision)}</code></p></div><div class="form-grid"><label>布局候选数<input name="count" type="number" value="3" min="1" max="5"></label><label>最大布线轮数<input name="passes" type="number" value="20" min="1" max="100"></label></div>${operation === "pipeline" ? '<label class="check-option"><input type="checkbox" name="release">验证通过后生成制造包</label>' : ""}<div class="steps">${(operation === "pipeline" ? ["布局", "应用", "布线", "验证"] : [operations[operation]]).map((x) => `<span>${x}</span>`).join("")}</div>`,
    async (form) =>
      submit({
        operation,
        ...target,
        candidate_count: Number(form.get("count")),
        passes: Number(form.get("passes")),
        release: form.has("release"),
      }),
    "开始执行",
  );
}
async function openRepair() {
  if (!state.revision || !state.inspection) return;
  const target = { project: state.project, revision: state.revision };
  let diagnosis = null, sending = false;
  const bound = `<div class="dialog-data"><strong>${esc(target.project)}</strong><p><code>${esc(target.revision)}</code></p></div>`;
  dialog("局部修复", bound + '<div class="panel-empty">正在读取诊断</div>', async () => {
    if (!active() || sending) throw new Error("修复范围已失效");
    const request = values();
    sending = true;
    try {
      await submit(request);
    } finally {
      sending = false;
    }
  }, "提交修复");
  repairTarget = target;
  const epoch = modalEpoch;
  const active = () => epoch === modalEpoch && $("#dialog").open &&
    state.project === target.project && state.revision === target.revision;
  $("#dialog-submit").disabled = true;

  function values() {
    if (diagnosis?.status !== "ok" || diagnosis.revision !== target.revision)
      throw new Error("当前诊断未放行");
    const proposal = diagnosis.proposals?.[Number($("#repair-proposal")?.value)];
    if (!proposal || !Array.isArray(proposal.region) || proposal.region.length !== 4)
      throw new Error("候选缺少有效范围");
    const nets = proposal.nets;
    if (!Array.isArray(nets) || !nets.length || nets.length > 8 ||
        nets.some((net) => typeof net !== "string" || !net.trim()) || new Set(nets).size !== nets.length)
      throw new Error("候选网络无效");
    const inputs = $$("#repair-roi input");
    const region = inputs.map((input) => input.valueAsNumber);
    if (region.length !== 4 || !region.every(Number.isFinite) || region[0] >= region[2] || region[1] >= region[3])
      throw new Error("范围须满足 X最小 < X最大、Y最小 < Y最大");
    const passes = $("#repair-passes").valueAsNumber;
    if (!Number.isInteger(passes) || passes < 1 || passes > 10)
      throw new Error("布线轮数须为 1–10 的整数");
    const ids = $("#repair-remove").value.trim().split(/[\s,;]+/).filter(Boolean);
    if (ids.length > 1000 || new Set(ids.map((id) => id.toLowerCase())).size !== ids.length ||
        ids.some((id) => !/^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/i.test(id)))
      throw new Error("删除 UUID 须有效且唯一，最多 1000 个");
    if (ids.length && !$("#repair-delete-confirm").checked)
      throw new Error("请确认删除列出的铜线对象");
    return { operation: "repair", ...target, repair_nets: [...nets],
      repair_region: region, repair_remove_ids: ids, passes };
  }
  function update() {
    if (!active()) return;
    const hasRemovals = Boolean($("#repair-remove")?.value.trim());
    $("#repair-mode").textContent = hasRemovals ? "含指定铜线删除" : "仅新增铜线";
    $("#repair-delete-confirm").required = hasRemovals;
    try {
      values();
      $("#repair-error").textContent = "";
      $("#dialog-submit").disabled = sending;
    } catch (error) {
      $("#repair-error").textContent = error.message;
      $("#dialog-submit").disabled = true;
    }
  }
  function selectProposal() {
    const proposal = diagnosis.proposals?.[Number($("#repair-proposal").value)];
    $("#repair-net").value = proposal?.nets?.join(", ") || "";
    const contact = proposal?.contact_geometry;
    const kinds = { unrouted_net: "网络尚未布线", same_layer_gap: "同层间隙",
      layer_transition: "跨层连接", overlapping_projection: "投影重叠，连通待核查" };
    $("#repair-contact").textContent = contact?.status === "ok"
      ? `${kinds[contact.classification] || "未分类"} · 投影间隙下界 ${Number(contact.projected_gap_lower_bound_mm).toFixed(3)} mm · 共同铜层 ${(contact.shared_layers || []).join(", ") || "无"} · 路径可行性未验证`
      : "几何诊断不可用";
    $$("#repair-roi input").forEach((input, i) => {
      input.value = Number.isFinite(proposal?.region?.[i]) ? proposal.region[i] : "";
    });
    $("#repair-remove").value = "";
    $("#repair-delete-confirm").checked = false;
    update();
  }
  try {
    const result = await api(`/projects/${encodeURIComponent(target.project)}/revisions/${encodeURIComponent(target.revision)}/repair-diagnosis`);
    if (!active()) return;
    diagnosis = result;
    const proposals = Array.isArray(result.proposals) ? result.proposals : [];
    $("#dialog-body").innerHTML = bound + `
      <div class="notice repair-notice">候选范围待确认 / <span id="repair-mode">仅新增铜线</span> / 制造门禁未放行</div>
      <label>候选<select id="repair-proposal" ${!proposals.length ? "disabled" : ""}>
        ${proposals.length ? proposals.map((p, i) => `<option value="${i}">${esc(`${i + 1}. ${(p.nets || []).join(", ")} · ${(p.items || []).map((item) => item.description || item.uuid).join(" ↔ ")}`)}</option>`).join("") : '<option>无修复候选</option>'}
      </select></label>
      <label>选定网络<input id="repair-net" readonly></label>
      <p id="repair-contact" class="notice" role="status"></p>
      <div id="repair-roi" class="form-grid repair-roi">${["X 最小", "Y 最小", "X 最大", "Y 最大"].map((label, i) => `<label>${label} (mm)<input id="repair-roi-${i}" type="number" step="any" required></label>`).join("")}</div>
      <label>最大布线轮数<input id="repair-passes" type="number" min="1" max="10" step="1" value="3" required></label>
      <details class="repair-deletions"><summary>删除指定铜线（可选）</summary>
        <label for="repair-remove">铜线 UUID</label><textarea id="repair-remove" rows="2" spellcheck="false"></textarea>
        <label class="check-option"><input id="repair-delete-confirm" type="checkbox">确认删除以上 UUID 对应铜线</label>
      </details><p id="repair-error" class="error-field" role="status"></p>`;
    $("#repair-proposal").onchange = selectProposal;
    $$("#repair-roi input,#repair-passes,#repair-delete-confirm").forEach((input) => input.oninput = update);
    $("#repair-remove").oninput = () => {
      $("#repair-delete-confirm").checked = false;
      update();
    };
    selectProposal();
  } catch (error) {
    if (!active()) return;
    $("#dialog-body").innerHTML = bound + `<div class="job-error">${esc(error.message)}</div>`;
    $("#dialog-submit").disabled = true;
  }
}
$("#environment").onclick = async () => {
  dialog("工具环境", '<div class="panel-empty">正在检测原生工具</div>', null);
  try {
    const result = await api("/environment");
    $("#dialog-body").innerHTML =
      `<div class="check-summary">${badge(result.status)}</div><pre class="evidence-json">${esc(JSON.stringify(result, null, 2))}</pre>`;
  } catch (error) {
    toast(error.message);
  }
};
function setTabs() {
  $$("[data-tab]").forEach((x) =>
    x.classList.toggle("active", x.dataset.tab === state.tab),
  );
  if (state.inspection) {
    const address = new URL(location.href);
    address.searchParams.set("project", state.project);
    address.searchParams.set("revision", state.revision);
    address.searchParams.set("tab", state.tab);
    history.replaceState(null, "", address);
  }
  renderPanel().catch((e) => toast(e.message));
}
$$("[data-tab]").forEach(
  (button) =>
    (button.onclick = () => {
      state.tab = button.dataset.tab;
      setTabs();
    }),
);
function table(headers, rows) {
  return `<table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}
async function renderPanel() {
  const panel = $("#panel-content");
  const loading = ++panelLoad;
  const project = state.project,
    revision = state.revision,
    tab = state.tab;
  const unchanged = () =>
    loading === panelLoad &&
    project === state.project &&
    revision === state.revision &&
    tab === state.tab;
  const inventoryTab = ["components", "nets", "routing"].includes(tab);
  document
    .querySelector("main")
    .classList.toggle("ledger-open", inventoryTab || tab === "integration");
  panel.classList.toggle("ledger-panel", inventoryTab || tab === "integration");
  if (tab === "integration") {
    await renderIntegration(panel, unchanged);
    return;
  } else if (inventoryTab) {
    if (!state.inspection) {
      panel.innerHTML = '<div class="panel-empty">未选择工程版本</div>';
      return;
    }
    panel.innerHTML =
      '<div class="panel-empty" role="status">正在读取工程台账</div>';
    try {
      const inventory = await getInventory(project, revision);
      if (!unchanged()) return;
      state.inventory = inventory;
      renderInventory();
    } catch (error) {
      if (unchanged())
        panel.innerHTML = `<div class="panel-empty" role="status">台账不可用：${esc(error.message)}<br><button class="icon" data-inventory-retry title="重新读取台账"><i data-lucide="refresh-cw"></i></button></div>`;
      icons();
    }
    return;
  } else if (state.tab === "jobs") {
    const jobs = state.project
      ? state.jobs.filter(
          (j) => j.project === state.project || state.watched.has(j.id),
        )
      : state.jobs;
    panel.innerHTML = jobs.length
      ? table(
          ["任务", "工程", "状态", "当前阶段", "更新时间", ""],
          jobs.map(
            (j) =>
              `<tr><td><code>${esc(j.id.slice(-8))}</code> · ${esc(operations[j.request.operation])}</td><td>${esc(j.project)}</td><td>${badge(j.status)}</td><td>${esc(operations[j.stage] || j.stage)}${j.cancel_requested ? " · 取消已请求" : ""}</td><td>${esc(j.updated.slice(11, 19))}</td><td class="row-actions"><button data-job="${esc(j.id)}" title="任务详情"><i data-lucide="list-tree"></i></button>${["running", "queued"].includes(j.status) ? `<button data-cancel="${esc(j.id)}" title="请求取消"><i data-lucide="square"></i></button>` : ""}</td></tr>`,
          ),
        )
      : '<div class="panel-empty">没有工程任务</div>';
  } else if (!state.inspection) {
    panel.innerHTML = '<div class="panel-empty">未选择工程</div>';
  } else if (state.tab === "checks") {
    const v = state.inspection.verification;
    panel.innerHTML =
      `<div class="check-summary">${badge(v.status)}<span>${esc(v.verification_id || "")}</span><span>${esc(v.created?.slice(0, 19).replace("T", " ") || "")}</span><button class="icon" data-report title="生成审阅报告"><i data-lucide="file-chart-column"></i></button></div>` +
      table(
        ["检查项", "状态", "错误", "警告", "未连接"],
        [
          ["PCB 设计规则", v.drc],
          ["原理图电气规则", v.erc],
          ["网络与器件一致性", v.connectivity],
          ["声明约束", v.constraints],
          ["保留线宽下限", v.persisted_track_minima],
        ].map(
          ([label, c]) =>
            `<tr><td>${label}</td><td>${badge(c?.status || "not_verified")}</td><td>${c?.errors ?? "—"}</td><td>${c?.warnings ?? "—"}</td><td>${c?.unconnected ?? "—"}</td></tr>`,
        ),
      ) +
      (v.reasons?.length
        ? `<ul class="findings">${v.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`
        : "") +
      findingsHtml(state.inspection.drc_findings) +
      `<details><summary class="subtle">原始验证依据</summary><pre class="evidence-json">${esc(JSON.stringify(v, null, 2))}</pre></details>`;
  } else if (state.tab === "plans") {
    let source = state.revision,
      plans = await api(`${projectUrl()}/revisions/${source}/plans`);
    if (!unchanged()) return;
    for (let i = 0; !plans.length && i < 5; i++) {
      const parent = state.revisions.find((r) => r.id === source)?.parent;
      if (!parent) break;
      source = parent;
      plans = await api(`${projectUrl()}/revisions/${source}/plans`);
      if (!unchanged()) return;
    }
    if (!unchanged()) return;
    panel.innerHTML = plans.length
      ? plans
          .slice(0, 2)
          .map(
            (plan) =>
              `<div class="inline-controls"><code>${esc(plan.plan_id)}</code><span class="subtle">基线 HPWL ${Number(plan.baseline?.metrics?.hpwl_mm ?? plan.baseline?.hpwl_mm ?? 0).toFixed(2)} mm · ${plan.baseline?.passed === false ? "基线约束未通过 · " : ""}${esc(plan.algorithm || plan.scope || "")}</span></div>` +
              table(
                [
                  "方案",
                  "可行性",
                  "HPWL / mm",
                  "改进 / mm",
                  "移动器件",
                  "总位移 / mm",
                  "间距 / mm",
                  "",
                ],
                plan.candidates.map(
                  (c) =>
                    `<tr><td>${esc(c.id)}</td><td>${badge(c.feasible ? "passed" : "blocked")}</td><td>${c.metrics.hpwl_mm.toFixed(2)}</td><td>${Number(c.metrics.hpwl_improvement_mm || 0).toFixed(2)}</td><td>${c.metrics.moved_components ?? "未记录"}</td><td>${c.metrics.total_displacement_mm == null ? "未记录" : c.metrics.total_displacement_mm.toFixed(3)}</td><td>${Number(c.metrics.minimum_axis_gap_mm || 0).toFixed(2)}</td><td><button data-apply="${esc(c.id)}" data-plan="${esc(plan.plan_id)}" data-source="${esc(source)}" ${!c.feasible ? "disabled" : ""}>应用</button></td></tr>`,
                ),
              ),
          )
          .join("")
      : '<div class="panel-empty">暂无布局候选</div>';
  } else if (state.tab === "releases") {
    const releases = await api(projectUrl() + "/releases");
    if (!unchanged()) return;
    panel.innerHTML =
      `<div class="inline-controls"><button data-release><i data-lucide="package-check"></i>生成制造包</button><button data-report><i data-lucide="file-chart-column"></i>生成报告</button><a target="_blank" href="/api${revisionUrl()}/report">打开当前报告</a></div>` +
      (releases.length
        ? table(
            ["发布记录", "工程版本", "日期", "SHA-256", ""],
            releases.map(
              (r) =>
                `<tr><td>${esc(r.release_id)}</td><td><code>${esc(r.revision)}</code></td><td>${esc(r.created.slice(0, 16).replace("T", " "))}</td><td><code>${esc(r.sha256.slice(0, 18))}</code></td><td><a href="/api${projectUrl()}/releases/${encodeURIComponent(r.release_id)}">下载 ZIP</a></td></tr>`,
            ),
          )
        : '<div class="panel-empty">暂无制造发布</div>');
  } else if (state.tab === "eco") {
    const choices = state.revisions.filter((r) => r.id !== state.revision);
    panel.innerHTML = `<div class="inline-controls"><select id="eco-before" aria-label="比较基线">${choices.map((r) => `<option value="${esc(r.id)}">${esc(r.operation)} · ${esc(r.id.slice(-8))}</option>`).join("")}</select><span class="subtle">→ ${esc(state.revision.slice(-8))}</span><button id="eco-compare" ${!choices.length ? "disabled" : ""}>比较</button></div><div id="eco-result"></div>`;
  }
  icons();
}
$("#panel-content").onclick = async (event) => {
  try {
    const b = event.target.closest("button");
    if (!b) return;
    if (b.dataset.job) await showJob(b.dataset.job);
    else if (b.dataset.finding) locateFinding(b.dataset.finding);
    else if (b.dataset.cancel) {
      await api(`/jobs/${b.dataset.cancel}/cancel`, {});
      toast("已请求在工程阶段边界取消");
      await poll();
    } else if (b.hasAttribute("data-apply"))
      await submit({
        operation: "apply",
        project: state.project,
        revision: b.dataset.source,
        plan_id: b.dataset.plan,
        candidate_id: b.dataset.apply,
      });
    else if (b.hasAttribute("data-release")) openRun("release");
    else if (b.hasAttribute("data-report"))
      await submit({
        operation: "report",
        project: state.project,
        revision: state.revision,
      });
    else if (b.id === "eco-compare") {
      const target = revisionUrl();
      const eco = await api(
        `${projectUrl()}/eco?before=${encodeURIComponent($("#eco-before").value)}&after=${encodeURIComponent(state.revision)}`,
      );
      if (target !== revisionUrl() || state.tab !== "eco") return;
      $("#eco-result").innerHTML =
        `<div class="check-summary"><strong>${eco.impact.direct_references.length} 个直接变更器件</strong><span>${eco.impact.affected_nets.length} 个影响网络</span><span>${eco.requires_new_verification ? "需要重新验证" : "设计摘要相同"}</span></div><pre class="evidence-json">${esc(JSON.stringify(eco.impact, null, 2))}</pre>`;
    }
  } catch (error) {
    toast(error.message);
  }
};
async function showJob(id) {
  const job = await api("/jobs/" + id);
  dialog("工程任务 · " + id.slice(-8), jobHtml(job), null);
  modalJob = id;
}
function jobFailureHtml(job) {
  const blocking = job.result?.blocking;
  if (!blocking && !job.result?.error) return "";
  const headline = job.result.error || blocking.reason || JSON.stringify(blocking.reasons || blocking);
  const details = Array.isArray(blocking?.details) ? blocking.details : [];
  const failed = details.find((item) => item && ["failed", "blocked"].includes(item.status));
  if (!failed || !String(blocking.reason || "").startsWith("Routing"))
    return `<div class="job-error">${esc(headline)}</div>`;
  const commands = Array.isArray(failed.commands) ? failed.commands : [];
  const command = [...commands].reverse().find((item) => item?.timed_out || item?.status === "failed");
  const timeout = failed.timed_out === true || command?.timed_out === true;
  const stage = blocking.failed_stage || ["export_dsn", "autoroute", "import_ses"][details.indexOf(failed)];
  const stageName = {export_dsn: "导出布线输入", autoroute: "自动布线", import_ses: "导入布线结果"}[stage] || stage;
  const seconds = Number(command?.timeout_seconds);
  const budget = timeout && Number.isFinite(seconds) && seconds > 0 ? `，时限 ${seconds} 秒` : "";
  const messages = [...new Set([failed.reason, failed.validation_error].filter((value) => typeof value === "string" && value))];
  return `<div class="job-error"><strong>${esc(stageName)}${timeout ? "超时" : "失败"}${esc(budget)}</strong><p>${esc(messages.join("；") || headline)}</p><p>未采用本次布线结果，原版本保留。此状态不代表已通过电气检查。</p></div>`;
}
function completionRepairsHtml(flow) {
  const phases = (flow.attempts || []).flatMap(attempt => [
    {cycle:1,additive_repair:attempt.additive_repair,repair:attempt.repair},
    ...(attempt.additional_repair_cycles || []),
  ].flatMap(round => [
    ["additive_repair", "补线修复"], ["repair", round.additive_repair ? "后续修复" : "自动修复"],
  ].filter(([key]) => round[key]).map(([key, label]) => ({candidate:attempt.candidate_id,
    label:`第 ${round.cycle} 轮 · ${label}`, result:round[key], attempts:round[key].attempts || []}))));
  if (!phases.length) return "";
  const count = phases.reduce((total, phase) => total + phase.attempts.length, 0);
  const accepted = phases.reduce((total, phase) => total + phase.attempts.filter(a => a.status === "accepted").length, 0);
  return `<div class="completion-repairs"><p>修复尝试 ${count} · 已采用 ${accepted}</p>
    <div class="inventory-scroll">${table(["布局候选","修复阶段","结果","尝试","未连接前 / 后"],phases.map(phase =>
      `<tr><td>${esc(phase.candidate)}</td><td>${esc(phase.label)}</td><td>${badge(phase.result.status || "running")}</td><td>${phase.attempts.length} / ${esc(phase.result.options?.max_attempts ?? "未知")}</td><td>${esc(phase.result.before_unconnected ?? "未知")} / ${esc(phase.result.after_unconnected ?? "未知")}</td></tr>`))}</div></div>`;
}
function completionViaCleanupHtml(flow) {
  return (flow.attempts || []).filter(attempt => attempt.via_cleanup).map(attempt => {
    const cleanup = attempt.via_cleanup, comparison = cleanup.comparison;
    const accepted = cleanup.status === "improved" && comparison?.accepted === true;
    const removed = accepted ? (Array.isArray(cleanup.proof?.removed_ids) ? cleanup.proof.removed_ids.length : "未知") : 0;
    const before = comparison?.before_by_net, after = comparison?.after_by_net;
    const nets = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])].sort();
    const reasons = [...new Set([cleanup.reason, ...(comparison?.reasons || [])].filter(Boolean))];
    return `<div class="completion-via-cleanup"><h4>冗余过孔检查 · ${esc(attempt.candidate_id)}</h4>
      <p>${badge(cleanup.status || "running")} · 本次清理采用删除 <b class="via-removed-count">${esc(removed)}</b> 个过孔</p>
      <p class="via-retained">${accepted ? "清理阶段保留版本" : "未采用清理结果，保留旧版"}：<code>${esc(cleanup.revision || cleanup.source_revision || flow.revision)}</code></p>
      ${reasons.length ? `<p class="job-error">${esc(reasons.join("；"))}</p>` : ""}
      ${comparison ? `<p>${accepted ? "未连接前 / 后" : "候选未连接前 / 后（未采用）"}</p><div class="inventory-scroll">${table(["网络","未连接前","未连接后"],nets.map(net => `<tr><td>${esc(net)}</td><td>${esc(before ? before[net] ?? 0 : "未知")}</td><td>${esc(after ? after[net] ?? 0 : "未知")}</td></tr>`))}</div>` : ""}</div>`;
  }).join("");
}
function jobHtml(job) {
  const flow = job.result?.steps?.complete;
  const flowStages = {preflight:"输入检查",planning:"布局规划",placement:"应用布局",routing:"整板布线",verification:"原生检查",clearance_cleanup:"间距复验修复",via_cleanup:"冗余过孔检查",repairing:"自动修复",retained:"保留已检查版本",candidate_rejected:"候选未通过",finished:"流程结束",interrupted:"流程中断"};
  const diagnoses = {route_timeout:"布线超时",placement_infeasible:"布局不可行",router_failure:"布线器失败",electrical_input:"电路输入待审",native_unavailable:"原生检查不可用",design_rule_conflict:"设计规则冲突",connections_incomplete:"连接未完成"};
  const best = flow?.best;
  const flowHtml = flow ? `<section class="completion-result"><h3>${esc(flowStages[flow.stage] || flow.stage)}</h3>
    <p>布局模式：${flow.options.placement_mode === "preserve" ? "保留现有布局" : "优化布局"}</p>
    <p>候选尝试 ${flow.attempts.length} / ${esc(flow.options.candidate_count)} · 用时 ${esc(flow.elapsed_seconds)} 秒</p>
    <p>保留版本：<a href="/?project=${encodeURIComponent(job.project)}&revision=${encodeURIComponent(flow.revision)}&tab=checks">${esc(flow.revision)}</a> ${badge(best?.status || "blocked")}</p>
    ${best ? `<p>未连接 ${esc(best.unconnected)} · DRC 错误 ${esc(best.drc_errors)} · ERC 错误 ${esc(best.erc_errors)} · 过孔 ${esc(best.vias)}</p><p>线长 ${esc(best.routed_length_mm == null ? "未知" : Number(best.routed_length_mm).toFixed(2))} mm · 移动器件 ${esc(best.moved_components)} · 位移合计 ${esc(Number(best.total_displacement_mm).toFixed(2))} mm</p>` : ""}
    ${flow.reason ? `<p class="job-error">${esc(flow.reason)}</p>` : ""}
    ${completionRepairsHtml(flow)}
    ${completionViaCleanupHtml(flow)}
    <div class="inventory-scroll">${table(["布局候选","结果","未连接","DRC 错误","诊断"],flow.attempts.map(a=>`<tr><td>${esc(a.candidate_id)}</td><td>${badge(a.status)}</td><td>${esc(a.metrics?.unconnected ?? "未知")}</td><td>${esc(a.metrics?.drc_errors ?? "未知")}</td><td>${a.status === "passed" ? "" : esc(diagnoses[a.diagnosis?.category] || a.diagnosis?.category || "")}</td></tr>`))}</div></section>` : "";
  const auto = job.result?.steps?.auto_repair || job.result?.steps?.reference_repair;
  const stages = {preflight:"环境与范围检查",baseline_verification:"基线原生检查",reference_verification:"参考版原生检查",native_rules:"读取原生规则",proposal:"生成候选",proposal_rejected:"未找到可行候选",candidate_verification:"候选原生检查",accepted:"采用合格候选",candidate_rejected:"拒绝违规候选",finished:"修复结束",interrupted:"修复中断"};
  const referenceHtml = auto?.reference_revision ? `<p class="reference-provenance">参考工程：${esc(auto.reference_project)}<br>参考版本：<code>${esc(auto.reference_revision)}</code></p>` : "";
  const autoHtml = auto ? `<section class="auto-repair-result"><h3>${esc(stages[auto.stage] || auto.stage)}</h3>${referenceHtml}<p>未连接：${esc(auto.before_unconnected ?? "未知")} → ${esc(auto.after_unconnected ?? "未知")} · 尝试 ${auto.attempts.length} / ${esc(auto.options.max_attempts)}</p><p>保留版本：<a href="/?project=${encodeURIComponent(job.project)}&revision=${encodeURIComponent(auto.revision)}&tab=checks">${esc(auto.revision)}</a></p><div class="inventory-scroll">${table(["网络","策略","结果","未连接前 / 后"],auto.attempts.map(a=>`<tr><td>${esc(a.proposal?.adjustment?.nets?.join(", ") || a.net)}${a.patch ? `<br>拆除 ${esc(a.patch.removed)} / 新增 ${esc(a.patch.added)}` : ""}</td><td>${esc({additive:"补线",expanded:"扩大搜索",neckdown:"短缩颈",fanout:"局部扇出",multinet:"无参考多网络重布",reference_multinet:"参考版多网络重布"}[a.strategy] || a.strategy)}</td><td>${esc({accepted:"已采用",rejected:"已拒绝",proposed:"待验证",blocked:"无可行解",running:"执行中"}[a.status] || a.status)}</td><td>${esc(a.comparison?.before_unconnected ?? "—")} / ${esc(a.comparison?.after_unconnected ?? "—")}</td></tr>`))}</div></section>` : "";
  return `<div class="check-summary">${badge(job.status)}<span>${esc(job.project)}</span><span>${esc(operations[job.stage] || labels[job.stage] || job.stage)}</span></div>${jobFailureHtml(job)}${flowHtml}${autoHtml}<ol class="event-list">${job.events.map((e) => `<li><time>${esc(e.created.slice(11, 19))}</time><strong>${esc(operations[e.stage] || labels[e.stage] || e.stage)}</strong><span>${esc(flowStages[e.state] || stages[e.state] || labels[e.status || e.state] || e.status || e.state)}</span></li>`).join("")}</ol><details><summary>任务结果</summary><pre class="evidence-json">${esc(JSON.stringify(job.result, null, 2))}</pre></details>`;
}
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    state.jobs = await api("/jobs");
    $("#active-jobs").textContent = state.jobs.filter((j) =>
      ["running", "queued"].includes(j.status),
    ).length;
    for (const job of state.jobs) {
      if (
        state.watched.has(job.id) &&
        !["running", "queued"].includes(job.status)
      ) {
        state.watched.delete(job.id);
        if (job.result?.revision)
          await loadProject(job.project, job.result.revision);
        else await loadProjects();
        toast(
          `${operations[job.request.operation]}：${labels[job.status] || job.status}`,
        );
      }
    }
    if (state.tab === "jobs") await renderPanel();
    if (modalJob && $("#dialog").open) {
      const target = modalJob;
      const job = await api("/jobs/" + target);
      if (modalJob === target && $("#dialog").open)
        $("#dialog-body").innerHTML = jobHtml(job);
    }
  } catch (error) {
    $("#connection").textContent = "连接中断";
  } finally {
    pollBusy = false;
  }
}

const colors = {
  "F.Cu": "#ef7f6d",
  "B.Cu": "#63bde1",
  "In1.Cu": "#d1b45e",
  "In2.Cu": "#80c99b",
  "In3.Cu": "#c58ba9",
  "In4.Cu": "#77c8bd",
  "In5.Cu": "#bdbd7f",
  "In6.Cu": "#bd9990",
};
function layerColor(layer) {
  return colors[layer] || "#8abda5";
}
function renderLayers() {
  $("#layers").innerHTML = [...state.layers]
    .map(
      (layer) =>
        `<label style="--layer-color:${layerColor(layer)}"><input type="checkbox" data-layer="${esc(layer)}" checked><span>${esc(layer)}</span></label>`,
    )
    .join("");
}
$("#layers").onchange = (e) => {
  const layer = e.target.dataset.layer;
  if (!layer) return;
  if (e.target.checked) state.layers.add(layer);
  else state.layers.delete(layer);
  draw();
};
function renderMetrics() {
  const b = state.inspection?.board;
  if (!b) return;
  $("#board-metrics").textContent =
    `${b.footprints.length} 器件 · ${b.nets.length} 网络 · ${b.tracks} 线段 · ${b.vias} 过孔 · ${b.copper_layers.length} 层`;
}
function selectComponent(ref) {
  state.finding = null;
  state.selected = ref;
  state.selectedNet = null;
  renderObjects();
  draw();
}
function selectNet(name) {
  state.finding = null;
  state.selectedNet = name;
  state.selected = null;
  renderObjects();
  draw();
}
function renderObjects() {
  const board = state.inspection?.board;
  if (!board) return;
  const query = state.search.toLowerCase();
  let objects;
  if (state.objectMode === "components") {
    objects = board.footprints.filter((fp) =>
      `${fp.reference} ${fp.value} ${fp.footprint}`
        .toLowerCase()
        .includes(query),
    );
    $("#objects").innerHTML = objects
      .map(
        (fp) =>
          `<button class="object-row ${state.selected === fp.reference ? "active" : ""}" data-reference="${esc(fp.reference)}"><strong>${esc(fp.reference)}</strong><span>${esc(fp.value)}</span></button>`,
      )
      .join("");
  } else {
    objects = board.nets.filter((net) =>
      net.name.toLowerCase().includes(query),
    );
    $("#objects").innerHTML = objects
      .map(
        (net) =>
          `<button class="object-row ${state.selectedNet === net.name ? "active" : ""}" data-net="${esc(net.name)}"><strong>${esc(net.name)}</strong><span>${net.pads.length} 焊盘</span></button>`,
      )
      .join("");
  }
  $("#object-count").textContent = objects.length;
  const fp = board.footprints.find((f) => f.reference === state.selected),
    net = board.nets.find((n) => n.name === state.selectedNet);
  $("#object-detail").innerHTML = fp
    ? `<b>${esc(fp.reference)} · ${esc(fp.value)}</b><div class="detail-grid"><span>封装</span><span>${esc(fp.footprint)}</span><span>位置</span><span>${fp.x.toFixed(3)}, ${fp.y.toFixed(3)} mm</span><span>角度 / 层</span><span>${fp.rotation}° / ${esc(fp.layer)}</span><span>焊盘</span><span>${fp.pads.length}</span></div>`
    : net
      ? `<b>${esc(net.name)}</b><div>${net.pads.length} 个焊盘 · ${new Set(net.pads.map((p) => p.reference)).size} 个器件</div>`
      : "<span>当前无选中对象</span>";
  $("#selection-tag").hidden = !(fp || net);
  $("#selection-tag").textContent = fp
    ? `${fp.reference} · ${fp.value}`
    : net
      ? net.name
      : "";
}
$$("[data-object]").forEach(
  (button) =>
    (button.onclick = () => {
      state.objectMode = button.dataset.object;
      $$("[data-object]").forEach((b) =>
        b.classList.toggle("active", b === button),
      );
      renderObjects();
    }),
);
$("#object-search").oninput = (e) => {
  state.search = e.target.value;
  renderObjects();
};
$("#objects").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.reference) selectComponent(b.dataset.reference);
  else if (b.dataset.net) selectNet(b.dataset.net);
};

const canvas = $("#board"),
  ctx = canvas.getContext("2d");
let width = 1,
  height = 1,
  pointer = null,
  raf = 0;
function resize() {
  const rect = canvas.parentElement.getBoundingClientRect();
  width = rect.width;
  height = rect.height;
  canvas.width = Math.round(width * devicePixelRatio);
  canvas.height = Math.round(height * devicePixelRatio);
  if (!state.inspection) {
    draw();
    return;
  }
  fit();
}
new ResizeObserver(resize).observe(canvas.parentElement);
function sceneBounds(board) {
  if (board.outline.bounds) return board.outline.bounds;
  const boxes = board.footprints.map((f) => f.bounds);
  return boxes.length
    ? [
        Math.min(...boxes.map((b) => b[0])),
        Math.min(...boxes.map((b) => b[1])),
        Math.max(...boxes.map((b) => b[2])),
        Math.max(...boxes.map((b) => b[3])),
      ]
    : null;
}
function fit() {
  const board = state.inspection?.board,
    bounds = board && sceneBounds(board);
  if (!bounds) return;
  state.scale = Math.min(
    (width - 55) / Math.max(1, bounds[2] - bounds[0]),
    (height - 55) / Math.max(1, bounds[3] - bounds[1]),
  );
  state.fitScale = state.scale;
  state.pan = {
    x: width / 2 - ((bounds[0] + bounds[2]) * state.scale) / 2,
    y: height / 2 - ((bounds[1] + bounds[3]) * state.scale) / 2,
  };
  draw();
}
function findingsHtml(result) {
  if (!result || result.status !== "ok") return "";
  const rows = (items) =>
    table(
      ["DRC 项目", "对象", ""],
      items.map(
        (f) =>
          `<tr><td><strong>${esc(f.type)}</strong><br>${esc(f.description)}</td><td class="finding-objects">${f.items.map((item) => esc(item.description)).join("<br>")}</td><td><button class="icon" data-finding="${esc(f.id)}" title="定位检查对象" ${!f.items.some((item) => item.pos) ? "disabled" : ""}><i data-lucide="locate-fixed"></i></button></td></tr>`,
      ),
    );
  const errors = result.items.filter((f) => f.severity === "error");
  const warnings = result.items.filter((f) => f.severity !== "error");
  return (
    `<div class="check-summary"><strong>DRC 对象 · ${result.total}</strong>${state.finding ? `<span>${esc(state.finding.type)}</span>` : ""}</div>` +
    (errors.length ? rows(errors) : "") +
    (warnings.length
      ? `<details><summary>警告与其他项目 · ${warnings.length}</summary>${rows(warnings)}</details>`
      : "") +
    (result.truncated
      ? `<p class="subtle">显示前 ${result.items.length} 项，共 ${result.total} 项</p>`
      : "")
  );
}
function locateFinding(id) {
  const result = state.inspection?.drc_findings;
  if (
    result?.status !== "ok" ||
    result.revision !== state.revision ||
    result.verification_id !== state.inspection.verification.verification_id
  )
    return;
  const finding = result.items.find((item) => item.id === id);
  const points =
    finding?.items
      .map((item) => item.pos)
      .filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.y)) || [];
  if (!points.length) return;
  state.finding = finding;
  state.selected = null;
  state.selectedNet = null;
  const xs = points.map((p) => p.x),
    ys = points.map((p) => p.y);
  const left = Math.min(...xs),
    right = Math.max(...xs),
    top = Math.min(...ys),
    bottom = Math.max(...ys);
  state.scale = Math.max(
    0.3,
    Math.min(
      25,
      (width - 70) / Math.max(10, right - left + 10),
      (height - 70) / Math.max(10, bottom - top + 10),
    ),
  );
  state.pan = {
    x: width / 2 - ((left + right) * state.scale) / 2,
    y: height / 2 - ((top + bottom) * state.scale) / 2,
  };
  renderObjects();
  draw();
  canvas.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function zoom(mult, x = width / 2, y = height / 2) {
  const next = Math.max(0.3, Math.min(100, state.scale * mult)),
    factor = next / state.scale;
  state.pan.x = x - (x - state.pan.x) * factor;
  state.pan.y = y - (y - state.pan.y) * factor;
  state.scale = next;
  draw();
}
$("#fit").onclick = fit;
$("#zoom-in").onclick = () => zoom(1.3);
$("#zoom-out").onclick = () => zoom(1 / 1.3);
$("#ratsnest").onchange = draw;
canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    zoom(Math.exp(-e.deltaY * 0.001), e.clientX - r.left, e.clientY - r.top);
  },
  { passive: false },
);
canvas.onpointerdown = (e) => {
  canvas.setPointerCapture(e.pointerId);
  pointer = { x: e.clientX, y: e.clientY, travel: 0 };
};
canvas.onpointermove = (e) => {
  const r = canvas.getBoundingClientRect(),
    x = (e.clientX - r.left - state.pan.x) / state.scale,
    y = (e.clientY - r.top - state.pan.y) / state.scale;
  $("#cursor").textContent = `X ${x.toFixed(2)} · Y ${y.toFixed(2)} mm`;
  if (pointer) {
    const dx = e.clientX - pointer.x,
      dy = e.clientY - pointer.y;
    state.pan.x += dx;
    state.pan.y += dy;
    pointer.travel += Math.abs(dx) + Math.abs(dy);
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    draw();
  }
};
canvas.onpointerup = (e) => {
  if (pointer && pointer.travel < 4 && state.inspection) {
    const r = canvas.getBoundingClientRect(),
      x = (e.clientX - r.left - state.pan.x) / state.scale,
      y = (e.clientY - r.top - state.pan.y) / state.scale;
    const found = state.inspection.board.footprints.find(
      (fp) =>
        x >= fp.bounds[0] &&
        x <= fp.bounds[2] &&
        y >= fp.bounds[1] &&
        y <= fp.bounds[3],
    );
    if (found) selectComponent(found.reference);
    else {
      const track = state.inspection.board.track_items.find(
        (t) =>
          state.layers.has(t.layer) &&
          distanceToSegment(x, y, t.start, t.end) <
            Math.max(0.3, 4 / state.scale),
      );
      if (track) selectNet(track.net);
      else {
        state.selected = null;
        state.selectedNet = null;
        state.finding = null;
        renderObjects();
        draw();
      }
    }
  }
  pointer = null;
};
canvas.onpointercancel = () => (pointer = null);
function distanceToSegment(x, y, a, b) {
  const dx = b[0] - a[0],
    dy = b[1] - a[1],
    t = Math.max(
      0,
      Math.min(
        1,
        ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy || 1),
      ),
    );
  return Math.hypot(x - a[0] - t * dx, y - a[1] - t * dy);
}
function draw() {
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(paint);
}
function paint() {
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#e8eef0";
  ctx.fillRect(0, 0, width, height);
  const b = state.inspection?.board;
  if (!b) return;
  ctx.save();
  ctx.translate(state.pan.x, state.pan.y);
  ctx.scale(state.scale, state.scale);
  const bounds = sceneBounds(b);
  if (!bounds) {
    ctx.restore();
    return;
  }
  const [x, y, right, bottom] = bounds;
  if (b.outline.bounds) {
    ctx.fillStyle = "#213e35";
    ctx.fillRect(x, y, right - x, bottom - y);
    ctx.strokeStyle = "#74a18b";
    ctx.lineWidth = 1 / state.scale;
    ctx.strokeRect(x, y, right - x, bottom - y);
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, right - x, bottom - y);
  ctx.clip();
  ctx.fillStyle = "#8bb7a825";
  const grid = state.scale > 4 ? 2 : 5;
  for (let gx = Math.ceil(x / grid) * grid; gx < right; gx += grid)
    for (let gy = Math.ceil(y / grid) * grid; gy < bottom; gy += grid)
      ctx.fillRect(gx, gy, 0.65 / state.scale, 0.65 / state.scale);
  ctx.restore();
  const highlighted = state.selectedNet
    ? new Set([state.selectedNet])
    : state.selected
      ? new Set(
          b.footprints
            .find((fp) => fp.reference === state.selected)
            ?.pads.map((p) => p.net),
        )
      : null;
  if ($("#ratsnest").checked) {
    ctx.lineWidth = 0.65 / state.scale;
    for (const net of b.nets) {
      if (highlighted && !highlighted.has(net.name)) continue;
      ctx.strokeStyle = highlighted ? "#e7deb8aa" : "#b7d6cb28";
      const first = net.pads[0];
      for (const pad of net.pads.slice(1)) {
        ctx.beginPath();
        ctx.moveTo(first.x, first.y);
        ctx.lineTo(pad.x, pad.y);
        ctx.stroke();
      }
    }
  }
  for (const layer of [...(b.copper_layers || [])].reverse()) {
    if (!state.layers.has(layer)) continue;
    for (const t of b.track_items || []) {
      if (t.layer !== layer) continue;
      ctx.globalAlpha = highlighted && !highlighted.has(t.net) ? 0.14 : 1;
      ctx.strokeStyle = layerColor(layer);
      ctx.lineWidth = t.width_mm;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(t.start[0], t.start[1]);
      ctx.lineTo(t.end[0], t.end[1]);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  for (const via of b.via_items || []) {
    const pos = via.at || via.position || via;
    const vx = Array.isArray(pos) ? pos[0] : pos.x,
      vy = Array.isArray(pos) ? pos[1] : pos.y;
    if (!Number.isFinite(vx) || !Number.isFinite(vy)) continue;
    ctx.fillStyle = "#d2ccb0";
    ctx.beginPath();
    ctx.arc(vx, vy, (via.diameter_mm || 0.65) / 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#172e29";
    ctx.beginPath();
    ctx.arc(vx, vy, (via.drill_mm || 0.3) / 2, 0, Math.PI * 2);
    ctx.fill();
  }
  for (const fp of b.footprints) {
    const selected = fp.reference === state.selected,
      related = !highlighted || fp.pads.some((p) => highlighted.has(p.net));
    ctx.globalAlpha = related ? 1 : 0.28;
    const bounds = fp.bounds;
    ctx.strokeStyle = selected ? "#f7f1bd" : "#a0b8a56a";
    ctx.lineWidth = (selected ? 1.8 : 0.7) / state.scale;
    ctx.strokeRect(
      bounds[0],
      bounds[1],
      bounds[2] - bounds[0],
      bounds[3] - bounds[1],
    );
    for (const p of fp.pads) {
      if (
        p.layers &&
        !p.layers.some(
          (l) => state.layers.has(l) || (l === "*.Cu" && state.layers.size),
        )
      )
        continue;
      const size = p.size || [1, 1];
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate((-(p.rotation || 0) * Math.PI) / 180);
      ctx.fillStyle = highlighted?.has(p.net) ? "#fff0af" : "#d1bd77";
      ctx.beginPath();
      if (p.shape === "circle")
        ctx.ellipse(0, 0, size[0] / 2, size[1] / 2, 0, 0, Math.PI * 2);
      else if (p.shape === "oval" || p.shape === "roundrect")
        ctx.roundRect(
          -size[0] / 2,
          -size[1] / 2,
          size[0],
          size[1],
          Math.min(...size) *
            (p.shape === "oval" ? 0.5 : (p.roundrect_rratio ?? 0.25)),
        );
      else ctx.rect(-size[0] / 2, -size[1] / 2, size[0], size[1]);
      ctx.fill();
      if (p.drill_mm) {
        ctx.fillStyle = "#1b332d";
        ctx.beginPath();
        if (p.drill_size) {
          const [dx, dy] = p.drill_size;
          ctx.roundRect(-dx / 2, -dy / 2, dx, dy, Math.min(dx, dy) / 2);
        } else ctx.arc(0, 0, p.drill_mm / 2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }
    if (selected || state.scale > 5) {
      ctx.fillStyle = selected ? "#fff9cc" : "#dce7e0";
      ctx.font = `${Math.min(10 / state.scale, 1.5)}px monospace`;
      ctx.textAlign = "center";
      ctx.fillText(
        fp.reference,
        (bounds[0] + bounds[2]) / 2,
        bounds[1] - 1 / state.scale,
      );
    }
  }
  ctx.globalAlpha = 1;
  if (state.finding) {
    ctx.strokeStyle = "#ff786b";
    ctx.fillStyle = "#ffffff";
    ctx.lineWidth = 2 / state.scale;
    ctx.font = `${12 / state.scale}px monospace`;
    ctx.textAlign = "left";
    state.finding.items.forEach((item, index) => {
      if (!item.pos) return;
      const { x, y } = item.pos,
        r = 10 / state.scale;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.moveTo(x - r * 1.4, y);
      ctx.lineTo(x + r * 1.4, y);
      ctx.moveTo(x, y - r * 1.4);
      ctx.lineTo(x, y + r * 1.4);
      ctx.stroke();
      ctx.fillText(String(index + 1), x + r * 1.6, y - r);
    });
  }
  ctx.restore();
  $("#zoom-level").textContent =
    Math.round((state.scale / state.fitScale) * 100) + "%";
}
// Inventory is cached by immutable project/revision, never by the visible tab.
let inventoryRequest = null;
const ledger = {
  search: "",
  category: "",
  layer: "",
  package: "",
  page: 1,
  size: 25,
  sort: "reference",
  direction: 1,
  kind: "tracks",
  detail: null,
  tab: null,
};
state.inventory = null;
function clearInventory() {
  state.inventory = null;
  inventoryRequest = null;
  ledger.detail = null;
  ledger.page = 1;
  ++panelLoad;
  if (["components", "nets", "routing"].includes(state.tab))
    $("#panel-content").innerHTML =
      '<div class="panel-empty">正在切换工程版本</div>';
}
async function getInventory(project, revision) {
  const key = JSON.stringify([project, revision]);
  if (inventoryRequest?.key === key) return inventoryRequest.promise;
  const request = { key };
  request.promise = api(
    `/projects/${encodeURIComponent(project)}/revisions/${encodeURIComponent(revision)}/inventory`,
  )
    .then((data) => {
      if (
        !data.summary ||
        !["components", "nets", "tracks", "vias", "layers"].every((k) =>
          Array.isArray(data[k]),
        )
      )
        throw new Error("台账数据结构不完整");
      if (
        (data.project && data.project !== project) ||
        (data.revision && data.revision !== revision)
      )
        throw new Error("台账版本不匹配");
      return data;
    })
    .catch((error) => {
      if (inventoryRequest === request) inventoryRequest = null;
      throw error;
    });
  inventoryRequest = request;
  return request.promise;
}
const unknown = (value) =>
  value == null || value === "" || value === "unknown"
    ? "未知"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
const categoryText = (value) =>
  ({
    resistor: "电阻",
    resistor_network: "电阻网络",
    capacitor: "电容",
    inductor: "电感",
    diode: "二极管",
    led: "发光二极管",
    transistor: "晶体管",
    integrated_circuit: "集成电路",
    connector: "连接器",
    crystal_or_oscillator: "晶体 / 振荡器",
    fuse: "保险丝",
    switch: "开关",
    relay: "继电器",
    test_point: "测试点",
    mounting_hardware: "安装件",
    battery: "电池",
    transformer: "变压器",
  })[value] || unknown(value);
const metric = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(3)
    : "未知";
const layerList = (row) =>
  Array.isArray(row.layers) ? row.layers : row.layer ? [row.layer] : [];
function matchesInventoryLayer(row, layer) {
  const layers = layerList(row);
  if (layers.includes(layer) || layers.includes("*.Cu")) return true;
  if (layers.includes("F&B.Cu")) return ["F.Cu", "B.Cu"].includes(layer);
  if (inventoryKind() === "vias" && layers.length === 2) {
    const count = state.inspection?.board.copper_layers.length || 2;
    const stack = [
      "F.Cu",
      ...Array.from(
        { length: Math.max(0, count - 2) },
        (_, i) => `In${i + 1}.Cu`,
      ),
      "B.Cu",
    ];
    const ends = layers.map((l) => stack.indexOf(l)).sort((a, b) => a - b),
      index = stack.indexOf(layer);
    return ends[0] >= 0 && index >= ends[0] && index <= ends[1];
  }
  return false;
}
const coordinate = (value) =>
  Array.isArray(value)
    ? value.map(metric).join(", ")
    : value && typeof value === "object"
      ? `${metric(value.x)}, ${metric(value.y)}`
      : "未知";
const connectionText = (value) =>
  ({
    unknown: "未知",
    not_evaluated: "未评估",
    not_verified: "未验证",
    unconnected: "存在未连接",
    connected: "已连接（接口记录）",
    routed: "已布线（接口记录）",
  })[value] || unknown(value);
const ledgerColumns = {
  components: [
    ["reference", "位号"],
    ["value", "标称值 / 器件名称"],
    ["category", "类别（推断）"],
    ["footprint", "封装"],
    ["layer", "安装层"],
    ["pad_count", "引脚数"],
    ["manufacturer", "制造商"],
    ["mpn", "制造商料号"],
  ],
  nets: [
    ["name", "网络"],
    ["pad_count", "焊盘数"],
    ["references", "关联器件"],
    ["track_count", "线段数"],
    ["via_count", "过孔数"],
    ["length_mm", "长度 / mm"],
    ["min_width_mm", "最小线宽 / mm"],
    ["max_width_mm", "最大线宽 / mm"],
    ["layers", "铜层"],
    ["connectivity_status", "连接状态"],
  ],
  tracks: [
    ["id", "线段 ID"],
    ["net", "网络"],
    ["layer", "铜层"],
    ["width_mm", "线宽 / mm"],
    ["length_mm", "长度 / mm"],
    ["start", "起点 / mm"],
    ["end", "终点 / mm"],
  ],
  vias: [
    ["id", "过孔 ID"],
    ["net", "网络"],
    ["layers", "层范围"],
    ["diameter_mm", "直径 / mm"],
    ["drill_mm", "钻孔 / mm"],
    ["at", "坐标 / mm"],
  ],
};
function ledgerValue(row, key) {
  if (key === "pad_count") return row.pad_count ?? row.pads?.length ?? null;
  if (key === "at")
    return row.at ?? row.position ?? (row.x != null ? [row.x, row.y] : null);
  if (
    key === "length_mm" &&
    !("length_mm" in row) &&
    row.kind !== "arc" &&
    row.start &&
    row.end
  )
    return Math.hypot(row.end[0] - row.start[0], row.end[1] - row.start[1]);
  return row[key];
}
function ledgerCell(row, key) {
  const value = ledgerValue(row, key);
  if (["at", "start", "end"].includes(key)) return coordinate(value);
  if (key.endsWith("_mm"))
    return (
      metric(value) +
      (key === "length_mm" && row.length_status === "partial" ? "（部分）" : "")
    );
  if (key === "connectivity_status") return connectionText(value);
  if (key === "category") return categoryText(value);
  if (Array.isArray(value))
    return value.length ? value.map(unknown).join(" · ") : "未知";
  return unknown(value);
}
function inventoryKind() {
  return state.tab === "routing" ? ledger.kind : state.tab;
}
function inventoryRows() {
  const kind = inventoryKind();
  const query = ledger.search.trim().toLocaleLowerCase();
  const rows = (state.inventory?.[kind] || []).filter((row) => {
    const searchable = (
      JSON.stringify(row) +
      " " +
      categoryText(row.category)
    ).toLocaleLowerCase();
    return (
      (!query || searchable.includes(query)) &&
      (!ledger.category || unknown(row.category) === ledger.category) &&
      (!ledger.package || unknown(row.footprint) === ledger.package) &&
      (!ledger.layer || matchesInventoryLayer(row, ledger.layer))
    );
  });
  const collator = new Intl.Collator("zh-CN", {
    numeric: true,
    sensitivity: "base",
  });
  rows.sort((a, b) => {
    const x = ledgerValue(a, ledger.sort),
      y = ledgerValue(b, ledger.sort);
    if (x == null || y == null)
      return x == null && y == null ? 0 : x == null ? 1 : -1;
    return (
      (typeof x === "number" && typeof y === "number"
        ? x - y
        : collator.compare(unknown(x), unknown(y))) * ledger.direction
    );
  });
  return rows;
}
function filterOptions(values, selected, title, display = unknown) {
  return (
    `<option value="">${title}</option>` +
    [...new Set(values)]
      .sort((a, b) => a.localeCompare(b, "zh-CN", { numeric: true }))
      .map(
        (v) =>
          `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc(display(v))}</option>`,
      )
      .join("")
  );
}
function renderInventory() {
  if (
    !state.inventory ||
    !["components", "nets", "routing"].includes(state.tab)
  )
    return;
  if (ledger.tab !== state.tab) {
    Object.assign(ledger, {
      tab: state.tab,
      search: "",
      category: "",
      package: "",
      layer: "",
      page: 1,
      detail: null,
      sort:
        state.tab === "components"
          ? "reference"
          : state.tab === "nets"
            ? "name"
            : "id",
      direction: 1,
    });
  }
  const data = state.inventory,
    kind = inventoryKind(),
    columns = ledgerColumns[kind];
  const rows = inventoryRows(),
    pages = Math.max(1, Math.ceil(rows.length / ledger.size));
  ledger.page = Math.min(ledger.page, pages);
  const shown = rows.slice(
    (ledger.page - 1) * ledger.size,
    ledger.page * ledger.size,
  );
  const totals = [
    ["component_count", "器件"],
    ["pad_count", "焊盘"],
    ["net_count", "网络"],
    ["track_count", "线段"],
    ["via_count", "过孔"],
    ["layer_count", "铜层"],
    ["package_count", "封装"],
  ];
  $("#panel-content").innerHTML =
    `<div class="inventory" data-kind="${kind}" data-project="${esc(state.project)}" data-revision="${esc(state.revision)}">
    <div class="inventory-summary">${totals.map(([key, text]) => `<span><b>${esc(unknown(data.summary[key]))}</b> ${text}</span>`).join("")}<code title="工程版本">${esc(state.revision)}</code></div>
    <div class="inventory-verification"><span>台账已读取</span><span>当前工程检查：${badge(state.inspection?.verification?.status || "not_verified")}</span><span>制造发布仍受独立验证门禁约束</span></div>
    <div class="inventory-toolbar">
      ${state.tab === "routing" ? `<div class="inventory-modes" role="group" aria-label="布线类型"><button data-inventory-kind="tracks" aria-pressed="${kind === "tracks"}">线段</button><button data-inventory-kind="vias" aria-pressed="${kind === "vias"}">过孔</button></div>` : ""}
      <label class="inventory-search"><i data-lucide="search"></i><input id="inventory-search" aria-label="搜索台账" placeholder="搜索台账" value="${esc(ledger.search)}"></label>
      ${
        kind === "components"
          ? `<select id="inventory-category" aria-label="器件类别">${filterOptions(
              data.components.map((r) => unknown(r.category)),
              ledger.category,
              "全部类别",
              categoryText,
            )}</select><select id="inventory-package" aria-label="器件封装">${filterOptions(
              data.components.map((r) => unknown(r.footprint)),
              ledger.package,
              "全部封装",
            )}</select>`
          : ""
      }
      <select id="inventory-layer" aria-label="台账层过滤">${filterOptions([...data[kind].flatMap(layerList), ...data.layers.map((r) => (typeof r === "string" ? r : r.name))].filter(Boolean), ledger.layer, "全部层")}</select>
      <div class="inventory-downloads"><button class="icon" data-inventory-export="${kind}.csv" title="下载完整${kind === "components" ? "器件" : kind === "nets" ? "网络" : kind === "tracks" ? "线段" : "过孔"} CSV" aria-label="下载完整 CSV"><i data-lucide="sheet"></i></button><button class="icon" data-inventory-export="json" title="下载完整台账 JSON" aria-label="下载完整台账 JSON"><i data-lucide="download"></i></button><button class="icon" data-inventory-export="exchange" title="下载工程交换 ZIP" aria-label="下载工程交换 ZIP"><i data-lucide="package"></i></button></div>
    </div>
    <div class="inventory-scroll" tabindex="0" aria-label="工程台账表格"><table class="inventory-table"><thead><tr>${columns.map(([key, name]) => `<th aria-sort="${ledger.sort === key ? (ledger.direction === 1 ? "ascending" : "descending") : "none"}"><button data-inventory-sort="${key}">${name}<i data-lucide="${ledger.sort === key ? (ledger.direction === 1 ? "arrow-up" : "arrow-down") : "arrow-up-down"}"></i></button></th>`).join("")}</tr></thead><tbody>${shown.map((r, i) => `<tr tabindex="0" data-inventory-row="${i}" aria-selected="${ledger.detail === r}" class="${ledger.detail === r ? "selected" : ""}">${columns.map(([key]) => `<td>${esc(ledgerCell(r, key))}</td>`).join("")}</tr>`).join("") || `<tr><td colspan="${columns.length}" class="panel-empty">没有匹配项</td></tr>`}</tbody></table></div>
    <div class="inventory-pagination"><span id="inventory-count">${rows.length} / ${data[kind].length} 项</span><select id="inventory-size" aria-label="每页条数">${[25, 50, 100].map((n) => `<option ${ledger.size === n ? "selected" : ""}>${n}</option>`).join("")}</select><button class="icon" data-inventory-page="first" title="首页" ${ledger.page === 1 ? "disabled" : ""}><i data-lucide="chevrons-left"></i></button><button class="icon" data-inventory-page="prev" title="上一页" ${ledger.page === 1 ? "disabled" : ""}><i data-lucide="chevron-left"></i></button><label class="inventory-page-label"><input id="inventory-page" aria-label="页码" type="number" min="1" max="${pages}" value="${ledger.page}"> / ${pages}</label><button class="icon" data-inventory-page="next" title="下一页" ${ledger.page === pages ? "disabled" : ""}><i data-lucide="chevron-right"></i></button><button class="icon" data-inventory-page="last" title="末页" ${ledger.page === pages ? "disabled" : ""}><i data-lucide="chevrons-right"></i></button></div>
    <div id="inventory-detail">${ledger.detail ? inventoryDetail(ledger.detail, kind) : ""}</div>
    <details class="inventory-coverage"><summary>数据覆盖与未知项</summary>${factsTable(data.coverage ?? null)}<h3>铜层记录</h3>${factsTable(data.layers)}<h3>来源记录</h3>${factsTable(data.sources ?? null)}</details></div>`;
  icons();
}
function factsTable(value) {
  if (value == null) return '<p class="subtle">未知</p>';
  const entries =
    typeof value === "object" ? Object.entries(value) : [["记录", value]];
  return `<dl class="inventory-facts">${entries.map(([key, v]) => `<dt>${esc(key)}</dt><dd>${esc(unknown(v))}</dd>`).join("")}</dl>`;
}
function inventoryDetail(row, kind) {
  const pads = row.pads || [];
  const padRows = pads
    .map(
      (p) =>
        `<tr><td>${esc(unknown(p.reference ?? row.reference))}</td><td>${esc(unknown(p.number ?? p.pad ?? p.name))}</td><td>${esc(unknown(p.net ?? (kind === "nets" ? row.name : null)))}</td><td>${esc(layerList(p).join(" · ") || "未知")}</td><td>${metric(p.x)}, ${metric(p.y)}</td><td>${esc(unknown(p.shape))}</td><td>${metric(p.drill_mm)}</td></tr>`,
    )
    .join("");
  return `<div class="inventory-detail-heading"><h3>${esc(unknown(row.reference ?? row.name ?? row.id))}</h3><button class="icon" data-inventory-close title="关闭对象详情"><i data-lucide="x"></i></button></div>
    ${kind === "components" ? factsTable({ 标称值: row.value, 封装: row.footprint, 类别: categoryText(row.category), 类别判断依据: row.category_basis, 制造商: row.manufacturer, 制造商料号: row.mpn, 数据手册: row.datasheet, "位置 / mm": `${metric(row.x)}, ${metric(row.y)}`, 角度: row.rotation, 安装层: row.layer, 固定状态: row.locked, 原始属性与来源: row.property_items ?? row.properties, 属性来源记录: row.metadata_sources ?? row.property_sources ?? row.provenance ?? null, 属性冲突记录: row.metadata_conflicts ?? null }) : factsTable({ ...Object.fromEntries(ledgerColumns[kind].map(([key, label]) => [label, ledgerCell(row, key)])), 长度统计范围: row.length_scope ?? null, 长度记录状态: row.length_status ?? null })}
    ${["components", "nets"].includes(kind) ? `<h3>全部引脚 / 焊盘 · ${pads.length}</h3><div class="inventory-scroll"><table class="inventory-pads"><thead><tr><th>位号</th><th>引脚编号</th><th>网络</th><th>层</th><th>X, Y / mm</th><th>形状</th><th>钻孔 / mm</th></tr></thead><tbody>${padRows || '<tr><td colspan="7">未知</td></tr>'}</tbody></table></div>` : ""}<details><summary>原始对象记录</summary><pre class="evidence-json">${esc(JSON.stringify(row, null, 2))}</pre></details>`;
}
function locateInventory(row, kind) {
  const board = state.inspection?.board;
  if (!board) return;
  let points = [];
  if (kind === "components") {
    const component = board.footprints.find(
      (f) => f.reference === row.reference,
    );
    if (!component) return toast("当前画布没有对应器件");
    selectComponent(component.reference);
    points = component.pads.length ? component.pads : [component];
  } else {
    const name = kind === "nets" ? row.name : row.net;
    const net = board.nets.find((n) => n.name === name);
    if (net) {
      selectNet(net.name);
      points = net.pads;
    } else {
      state.selected = null;
      state.selectedNet = null;
      state.finding = null;
      renderObjects();
    }
    if (kind === "tracks")
      points = [row.start, row.end]
        .filter(Array.isArray)
        .map(([x, y]) => ({ x, y }));
    if (kind === "vias") {
      const pos = ledgerValue(row, "at");
      points = Array.isArray(pos)
        ? [{ x: pos[0], y: pos[1] }]
        : pos
          ? [pos]
          : [];
    }
  }
  state.layers = new Set(board.copper_layers || []);
  renderLayers();
  points = points.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
  if (!points.length) {
    draw();
    return;
  }
  const xs = points.map((p) => p.x),
    ys = points.map((p) => p.y);
  const left = Math.min(...xs),
    right = Math.max(...xs),
    top = Math.min(...ys),
    bottom = Math.max(...ys);
  state.scale = Math.max(
    0.3,
    Math.min(
      25,
      (width - 70) / Math.max(10, right - left + 10),
      (height - 70) / Math.max(10, bottom - top + 10),
    ),
  );
  state.pan = {
    x: width / 2 - ((left + right) * state.scale) / 2,
    y: height / 2 - ((top + bottom) * state.scale) / 2,
  };
  draw();
}
async function exportInventory(format, button) {
  const project = state.project,
    revision = state.revision;
  const base = `/api/projects/${encodeURIComponent(project)}/revisions/${encodeURIComponent(revision)}/inventory`;
  const path =
    format === "exchange"
      ? `${base}/exchange`
      : `${base}/export?format=${encodeURIComponent(format)}`;
  button.disabled = true;
  try {
    const response = await fetch(path);
    if (!response.ok) throw new Error(`导出失败：HTTP ${response.status}`);
    const blob = await response.blob();
    if (project !== state.project || revision !== state.revision) return;
    const url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = `${project}-${revision}-${format === "json" ? "inventory.json" : format === "exchange" ? "engineering-exchange.zip" : format}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) {
    if (project === state.project && revision === state.revision)
      toast(error.message);
  } finally {
    button.disabled = false;
  }
}
$("#panel-content").addEventListener("click", (event) => {
  const target = event.target.closest("button,[data-inventory-row]");
  if (!target) return;
  if (target.hasAttribute("data-inventory-retry")) {
    renderPanel();
    return;
  }
  if (target.hasAttribute("data-integration-retry")) {
    renderPanel();
    return;
  }
  if (!state.inventory) return;
  if (target.dataset.inventoryExport) {
    exportInventory(target.dataset.inventoryExport, target);
    return;
  }
  if (target.dataset.inventorySort) {
    ledger.direction =
      ledger.sort === target.dataset.inventorySort ? -ledger.direction : 1;
    ledger.sort = target.dataset.inventorySort;
    ledger.page = 1;
  } else if (target.dataset.inventoryKind) {
    Object.assign(ledger, {
      kind: target.dataset.inventoryKind,
      page: 1,
      search: "",
      category: "",
      layer: "",
      package: "",
      sort: "id",
      detail: null,
    });
  } else if (target.dataset.inventoryPage) {
    const last = Math.max(1, Math.ceil(inventoryRows().length / ledger.size));
    ledger.page = {
      first: 1,
      prev: Math.max(1, ledger.page - 1),
      next: Math.min(last, ledger.page + 1),
      last,
    }[target.dataset.inventoryPage];
  } else if (target.hasAttribute("data-inventory-close")) ledger.detail = null;
  else if (target.hasAttribute("data-inventory-row")) {
    const row =
      inventoryRows()[
        (ledger.page - 1) * ledger.size + Number(target.dataset.inventoryRow)
      ];
    if (!row) return;
    ledger.detail = row;
    locateInventory(row, inventoryKind());
  } else return;
  renderInventory();
});
$("#panel-content").addEventListener("keydown", (event) => {
  if (
    event.target.matches("[data-inventory-row]") &&
    ["Enter", " "].includes(event.key)
  ) {
    event.preventDefault();
    event.target.click();
  }
});
$("#panel-content").addEventListener("input", (event) => {
  if (event.target.id !== "inventory-search") return;
  const position = event.target.selectionStart;
  ledger.search = event.target.value;
  ledger.page = 1;
  renderInventory();
  $("#inventory-search").focus({ preventScroll: true });
  $("#inventory-search").setSelectionRange(position, position);
});
$("#panel-content").addEventListener("change", (event) => {
  const key = {
    "inventory-category": "category",
    "inventory-package": "package",
    "inventory-layer": "layer",
    "inventory-size": "size",
    "inventory-page": "page",
  }[event.target.id];
  if (!key) return;
  ledger[key] = ["size", "page"].includes(key)
    ? Math.max(1, Number(event.target.value) || 1)
    : event.target.value;
  if (key !== "page") ledger.page = 1;
  renderInventory();
});
// Integration records are facts, not certification claims. Never render credentials.
function publicIntegration(value) {
  if (Array.isArray(value)) return value.map(publicIntegration);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [
        k,
        /token|secret|password|credential|authorization|api.?key/i.test(k)
          ? "已隐藏"
          : publicIntegration(v),
      ]),
    );
  if (typeof value === "string")
    return value
      .replace(/Bearer\s+[A-Za-z0-9._~+/=-]{16,}/gi, "Bearer [已隐藏]")
      .replace(/([?&](?:token|api_key|secret)=)[^&\s]*/gi, "$1[已隐藏]");
  return value;
}
async function renderIntegration(panel, unchanged) {
  panel.innerHTML = '<div class="panel-empty">正在读取本机集成能力</div>';
  const records = {};
  for (const [key, path] of [
    ["capabilities", "/integration/capabilities"],
    ["openapi", "/integration/openapi.json"],
  ]) {
    try {
      records[key] = publicIntegration(await api(path));
    } catch (error) {
      records[key] = { unavailable: error.message };
    }
    if (!unchanged()) return;
  }
  const endpoints = Object.entries(records.openapi.paths || {}).flatMap(
    ([path, methods]) =>
      Object.entries(methods)
        .filter(([method]) =>
          ["get", "post", "put", "delete", "patch", "options", "head"].includes(
            method,
          ),
        )
        .map(
          ([method, operation]) =>
            `<tr><td>${esc(method.toUpperCase())}</td><td><code>${esc(path)}</code></td><td>${esc(unknown(operation.summary ?? operation.operationId))}</td></tr>`,
        ),
  );
  const c = records.capabilities;
  const capabilityFacts = {
    接口版本: c.api_version,
    部署模式: c.deployment,
    机器接口: c.enabled == null ? null : c.enabled ? "已启用" : "未启用",
    写入权限:
      c.write_enabled == null ? null : c.write_enabled ? "已启用" : "未启用",
    传输: c.transport,
    身份校验: c.authentication,
    数据格式: c.formats,
    操作范围: c.operations,
    幂等约束: c.idempotency,
    已认证企业平台:
      Array.isArray(c.certified_platforms) && !c.certified_platforms.length
        ? "无"
        : c.certified_platforms,
    对外数据传输:
      c.external_data_transfer == null
        ? null
        : c.external_data_transfer
          ? "开启"
          : "关闭",
    接口读取错误: c.unavailable,
  };
  panel.innerHTML = `<section class="integration-records"><div class="inventory-detail-heading"><h3>本机集成能力</h3><button class="icon" data-integration-retry title="刷新集成能力"><i data-lucide="refresh-cw"></i></button></div><p class="integration-status">本机接口 · 企业平台兼容性与认证未验证</p>${factsTable(capabilityFacts)}<h3>限制与未验证项</h3><ul class="integration-limitations">${(Array.isArray(c.limitations) ? c.limitations : ["未知"]).map((item) => `<li>${esc(unknown(item))}</li>`).join("")}</ul><h3>OpenAPI 接口清单</h3>${endpoints.length ? `<div class="inventory-scroll">${table(["方法", "路径", "操作"], endpoints)}</div>` : '<p class="subtle">未知：当前未返回接口清单</p>'}<details><summary>已脱敏的能力记录</summary><pre class="evidence-json">${esc(JSON.stringify(c, null, 2))}</pre></details><details><summary>已脱敏的 OpenAPI 记录</summary><pre class="evidence-json">${esc(JSON.stringify(records.openapi, null, 2))}</pre></details></section>`;
  icons();
}
async function init() {
  icons();
  setTabs();
  $$(".project-actions button,#constraints").forEach(
    (b) => (b.disabled = true),
  );
  try {
    const health = await api("/health");
    $("#connection").textContent = "本地工作站 · " + health.version;
    state.presets = await api("/presets");
    $("#presets").innerHTML = state.presets
      .map(
        (p) =>
          `<button class="preset" data-preset="${esc(p.id)}"><i data-lucide="cpu"></i>${esc(p.id)}</button>`,
      )
      .join("");
    icons();
    $("#presets").onclick = (e) => {
      const b = e.target.closest("[data-preset]");
      if (b) openImport(state.presets.find((p) => p.id === b.dataset.preset));
    };
    await loadProjects(true);
    await poll();
    setInterval(poll, 2000);
  } catch (error) {
    toast(error.message);
    $("#connection").textContent = "连接失败";
  }
}
init();

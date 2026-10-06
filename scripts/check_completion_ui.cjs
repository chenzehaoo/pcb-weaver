"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path");
const {chromium} = require("playwright");

async function assertPassedCandidateDiagnoses(page, expectedCount) {
  const passed = page.locator(".completion-result > .inventory-scroll tbody tr").filter({has:page.locator(".state.passed")});
  assert.equal(await passed.count(),expectedCount);
  for (const diagnosis of await passed.locator("td:last-child").allTextContents())
    assert.equal(diagnosis,"","Passed candidate still shows a stale failure diagnosis");
}

(async () => {
  const offline = process.argv[2] === "--offline";
  const url = new URL(offline ? "http://completion-ui.test/?project=ui-project&revision=ui-revision" : process.argv[2]);
  const output = path.resolve(process.argv[3]), jobId = process.argv[4];
  const project = url.searchParams.get("project"), revision = url.searchParams.get("revision");
  assert(project && revision && (offline || jobId));
  const browser = await chromium.launch({headless:true,channel:"chrome"});
  fs.mkdirSync(output,{recursive:true});
  const results = [];
  try {
    for (const [name,viewport] of [["desktop",{width:1440,height:1000}],["mobile",{width:390,height:844}],["narrow",{width:320,height:740}]]) {
      const context = await browser.newContext({viewport});
      const writes = [],errors = [];
      if (offline) {
        const web = path.resolve(__dirname,"../src/pcb_weaver/web");
        const assets = new Map([["/","index.html"],["/assets/app.js","app.js"],
          ["/assets/style.css","style.css"],["/assets/vendor/lucide.min.js","vendor/lucide.min.js"]]);
        // Fulfill every request locally; offline checks never contact a server.
        await context.route("**/*",route => {
          const file = assets.get(new URL(route.request().url()).pathname);
          return file ? route.fulfill({path:path.join(web,file)}) : route.abort();
        });
      }
      await context.route("**/api/**",async route => {
        if (["GET","HEAD"].includes(route.request().method())) {
          if (!offline) return route.continue();
          const endpoint = new URL(route.request().url()).pathname;
          assert(["/api/health","/api/projects","/api/presets","/api/jobs"].includes(endpoint),endpoint);
          return route.fulfill({json:endpoint === "/api/health" ? {version:"ui-test"} : []});
        }
        assert.equal(new URL(route.request().url()).pathname,"/api/jobs");
        writes.push(route.request().postDataJSON());
        return route.fulfill({json:{id:"ui-completion-contract",status:"queued"}});
      });
      const page = await context.newPage();
      page.on("pageerror",e=>errors.push(e.message));
      await page.goto(url.href);
      if (offline) {
        await page.waitForFunction(() => document.querySelector("#connection").textContent.includes("ui-test"));
        await page.evaluate(({project,revision}) => {
          Object.assign(pcbWorkbench,{project,revision,inspection:{board:{tracks:0,vias:0,outline:{},footprints:[]}}});
          document.querySelector("#run-completion").disabled = false;
        },{project,revision});
      }
      await page.waitForFunction(() => window.pcbWorkbench?.inspection && !document.querySelector("#run-completion").disabled);
      assert.equal(await page.evaluate(()=>pcbWorkbench.inspection.board.tracks),0);
      await page.locator("#run-completion").click();
      await page.locator('[name="candidates"]').fill("6");
      await page.locator("#dialog-submit").click();
      assert.equal(writes.length,0);
      await page.locator('[name="candidates"]').fill("3");
      await page.locator('[name="budget"]').fill("59");
      await page.locator("#dialog-submit").click();
      assert.equal(writes.length,0);
      await page.locator('[name="budget"]').fill("1800");
      assert(!(await page.locator('[name="neckdown"]').isChecked()));
      assert(!(await page.locator('[name="adjustment"]').isChecked()));
      assert.equal(await page.locator('[name="placement_mode"]').inputValue(),"optimize");
      assert.equal(await page.locator('[name="repair_cycles"]').inputValue(),"1");
      assert(await page.locator('[name="repair_cycles"]').isDisabled());
      assert(!(await page.locator("#completion-cycles").isVisible()));
      assert(!(await page.locator('[name="multinet"]').isChecked()));
      for (const field of ["attempts","region-area","proposal-timeout"])
        assert(await page.locator(`[name="${field}"]`).isDisabled());
      assert(await page.locator("#dialog").evaluate(el=>el.scrollWidth<=el.clientWidth+1));
      await page.screenshot({path:path.join(output,`${name}-options.png`),fullPage:true});
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(()=>!document.querySelector("#dialog").open);
      assert.deepEqual(writes[0],{operation:"complete",project,revision,completion_options:{candidate_count:3,
        route_passes:20,time_budget_seconds:1800,placement_spread_mm:0.5,routing_policy:"strict",repair:{allow_neckdown:false,allow_local_adjustment:false}}});
      await page.locator("#run-completion").click();
      await page.locator('[name="candidates"]').fill("6");
      await page.locator('[name="spread"]').fill("3");
      await page.locator('[name="placement_mode"]').selectOption("preserve");
      assert(await page.locator("#completion-cycles").isVisible());
      assert(!(await page.locator('[name="repair_cycles"]').isDisabled()));
      for (const field of ["candidates","spread"])
        assert(await page.locator(`[name="${field}"]`).isDisabled());
      await page.locator('[name="placement_mode"]').selectOption("optimize");
      for (const field of ["candidates","spread"])
        assert(!(await page.locator(`[name="${field}"]`).isDisabled()));
      await page.locator("#dialog-submit").click();
      assert.equal(writes.length,1,"Re-enabled invalid layout values submitted");
      await page.locator('[name="placement_mode"]').selectOption("preserve");
      await page.locator('[name="multinet"]').check();
      assert.equal(await page.locator('[name="attempts"]').evaluate(el=>el.parentElement.textContent),"每轮最多尝试");
      const bounds = [["attempts","25","8"],["region-area","2501","400"],["proposal-timeout","181","45"]];
      for (const [field,invalid,valid] of bounds) {
        const input = page.locator(`[name="${field}"]`);
        assert(!(await input.isDisabled()));
        await input.fill(invalid);
        await page.locator("#dialog-submit").click();
        assert.equal(writes.length,1,`Invalid ${field} submitted`);
        await input.fill(valid);
      }
      await page.locator('[name="neckdown"]').check();
      await page.locator('[name="adjustment"]').check();
      await page.locator('[name="routing_policy"]').selectOption("normalize_widths");
      const modalLayout = await page.locator("#dialog").evaluate(el => {
        const r = el.getBoundingClientRect();
        return el.scrollWidth <= el.clientWidth+1 && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
      });
      assert(modalLayout,`${name}: expanded completion dialog overflow`);
      await page.screenshot({path:path.join(output,`${name}-preserve-options.png`),fullPage:true});
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(()=>!document.querySelector("#dialog").open);
      assert.deepEqual(writes[1],{operation:"complete",project,revision,completion_options:{placement_mode:"preserve",
        route_passes:20,time_budget_seconds:1800,routing_policy:"normalize_widths",repair:{allow_neckdown:true,
          allow_local_adjustment:true,allow_multinet:true,max_attempts:8,max_region_area_mm2:400,proposal_timeout_seconds:45}}});
      await page.locator("#run-completion").click();
      await page.locator('[name="placement_mode"]').selectOption("preserve");
      await page.locator('[name="multinet"]').check();
      await page.locator('[name="attempts"]').fill("0");
      await page.locator('[name="multinet"]').uncheck();
      assert(!(await page.locator("#multinet-bounds").isVisible()));
      for (const field of ["attempts","region-area","proposal-timeout"])
        assert(await page.locator(`[name="${field}"]`).isDisabled());
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(()=>!document.querySelector("#dialog").open);
      assert.deepEqual(writes[2],{operation:"complete",project,revision,completion_options:{placement_mode:"preserve",
        route_passes:20,time_budget_seconds:1800,routing_policy:"strict",repair:{allow_neckdown:false,allow_local_adjustment:false}}});
      await page.locator("#run-completion").click();
      await page.locator('[name="placement_mode"]').selectOption("preserve");
      for (const invalid of ["","0","4","1.5"]) {
        await page.locator('[name="repair_cycles"]').fill(invalid);
        await page.locator("#dialog-submit").click();
        assert.equal(writes.length,3,`Invalid repair_cycles ${invalid} submitted`);
        assert(await page.locator("#dialog").evaluate(el=>el.open));
      }
      // Exercise the handler guard as well as native form validation.
      await page.evaluate(()=>document.querySelector("#dialog-form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
      await page.waitForFunction(()=>document.querySelector("#toast").textContent.includes("修复轮数") && !document.querySelector("#dialog-submit").disabled);
      assert.equal(writes.length,3,"Handler accepted fractional repair_cycles");
      await page.locator('[name="repair_cycles"]').fill("4");
      await page.locator("#dialog-submit").click();
      assert.equal(writes.length,3,"Preserve accepted repair_cycles greater than 3");
      await page.locator('[name="placement_mode"]').selectOption("optimize");
      assert.equal(await page.locator('[name="repair_cycles"]').inputValue(),"4");
      assert(await page.locator('[name="repair_cycles"]').isDisabled());
      assert(!(await page.locator("#completion-cycles").isVisible()));
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(()=>!document.querySelector("#dialog").open);
      assert.deepEqual(writes[3],writes[0],"Optimize submitted disabled invalid cycle value");
      for (const cycles of [2,3]) {
        await page.locator("#run-completion").click();
        assert.equal(await page.locator('[name="repair_cycles"]').inputValue(),"1");
        await page.locator('[name="placement_mode"]').selectOption("preserve");
        await page.locator('[name="repair_cycles"]').fill(String(cycles));
        await page.locator('[name="placement_mode"]').selectOption("optimize");
        await page.locator('[name="placement_mode"]').selectOption("preserve");
        assert.equal(await page.locator('[name="repair_cycles"]').inputValue(),String(cycles));
        await page.locator('[name="budget"]').fill("7201");
        await page.locator("#dialog-submit").click();
        assert.equal(writes.length,cycles+2,"Completion budget exceeded 7200");
        await page.locator('[name="budget"]').fill("7200");
        await page.locator("#dialog-submit").click();
        await page.waitForFunction(()=>!document.querySelector("#dialog").open);
        assert.deepEqual(writes.at(-1),{...writes[2],completion_options:{...writes[2].completion_options,
          repair_cycles:cycles,time_budget_seconds:7200}});
      }
      await page.locator("#run-completion").click();
      await page.locator('[name="placement_mode"]').selectOption("preserve");
      await page.locator('[name="repair_cycles"]').fill("3");
      await page.locator('[name="placement_mode"]').selectOption("optimize");
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(()=>!document.querySelector("#dialog").open);
      assert.deepEqual(writes[6],writes[0],"Optimize submitted preserve cycle setting");
      const submissionsBeforeStale = writes.length;
      for (const [mode,field] of [["preserve","revision"],["optimize","project"]]) {
        await page.locator("#run-completion").click();
        assert.equal(await page.locator('[name="placement_mode"]').inputValue(),"optimize");
        await page.locator('[name="placement_mode"]').selectOption(mode);
        await page.evaluate(field=>{pcbWorkbench[field]="stale-target";},field);
        await page.locator("#dialog-submit").click();
        await page.waitForFunction(()=>document.querySelector("#toast").textContent.includes("工程版本已变化") && !document.querySelector("#dialog-submit").disabled);
        assert.equal(writes.length,submissionsBeforeStale,`Stale ${field} submitted in ${mode} mode`);
        assert(await page.locator("#dialog").evaluate(el=>el.open));
        await page.locator("#dialog-close").click();
        await page.evaluate(target=>Object.assign(pcbWorkbench,target),{project,revision});
      }
      for (const mode of [undefined,"optimize","preserve"]) {
        await page.evaluate(({mode,project,revision}) => {
          dialog("Completion result",jobHtml({project,status:"completed",stage:"complete",events:[],result:{steps:{complete:{
            stage:"finished",revision,options:{candidate_count:3,...(mode ? {placement_mode:mode} : {})},
            elapsed_seconds:1,attempts:[]}}}}));
        },{mode,project,revision});
        assert((await page.locator(".completion-result").textContent()).includes(mode === "preserve" ? "保留现有布局" : "优化布局"));
        await page.locator("#dialog-close").click();
      }
      const additive = {status:"repaired",before_unconnected:1,after_unconnected:0,
        options:{max_attempts:4},attempts:[{status:"accepted",strategy:"additive"}]};
      const repair = {status:"repaired",before_unconnected:1,after_unconnected:0,
        options:{max_attempts:24},attempts:[{status:"rejected",strategy:"multinet"},{status:"accepted",strategy:"multinet"}]};
      const repairCases = [
        {name:"no-repairs",attempts:[{candidate_id:"preserved"}],rows:[]},
        {name:"additive-only",attempts:[{candidate_id:"preserved",additive_repair:additive}],count:1,accepted:1,
          rows:[["preserved","第 1 轮 · 补线修复","已修复","1 / 4","1 / 0"]]},
        {name:"both-phases",attempts:[{candidate_id:"preserved",additive_repair:{...additive,before_unconnected:2,after_unconnected:1},repair}],count:3,accepted:2,
          rows:[["preserved","第 1 轮 · 补线修复","已修复","1 / 4","2 / 1"],["preserved","第 1 轮 · 后续修复","已修复","2 / 24","1 / 0"]]},
        {name:"legacy-repair",attempts:[{candidate_id:"legacy",repair}],count:2,accepted:1,
          rows:[["legacy","第 1 轮 · 自动修复","已修复","2 / 24","1 / 0"]]},
        {name:"multiple-candidates",attempts:[{candidate_id:"first",additive_repair:additive},{candidate_id:"second",repair}],count:3,accepted:2,
          rows:[["first","第 1 轮 · 补线修复","已修复","1 / 4","1 / 0"],["second","第 1 轮 · 自动修复","已修复","2 / 24","1 / 0"]]},
        {name:"pending-repair",attempts:[{candidate_id:"<pending>",additive_repair:{status:"running"}}],count:0,accepted:0,
          rows:[["<pending>","第 1 轮 · 补线修复","执行中","0 / 未知","未知 / 未知"]]},
        {name:"three-cycles",attempts:[{candidate_id:"preserved",additive_repair:additive,repair,
          additional_repair_cycles:[{cycle:2,additive_repair:additive,repair},{cycle:3,additive_repair:additive}]}],count:7,accepted:5,
          rows:[["preserved","第 1 轮 · 补线修复","已修复","1 / 4","1 / 0"],
            ["preserved","第 1 轮 · 后续修复","已修复","2 / 24","1 / 0"],
            ["preserved","第 2 轮 · 补线修复","已修复","1 / 4","1 / 0"],
            ["preserved","第 2 轮 · 后续修复","已修复","2 / 24","1 / 0"],
            ["preserved","第 3 轮 · 补线修复","已修复","1 / 4","1 / 0"]]},
        {name:"empty-extras",attempts:[{candidate_id:"preserved",additive_repair:additive,additional_repair_cycles:[]}],count:1,accepted:1,
          rows:[["preserved","第 1 轮 · 补线修复","已修复","1 / 4","1 / 0"]]},
        {name:"subsequent-only",attempts:[{candidate_id:"preserved",additional_repair_cycles:[{cycle:2,repair}]}],count:2,accepted:1,
          rows:[["preserved","第 2 轮 · 自动修复","已修复","2 / 24","1 / 0"]]},
        {name:"pending-extra",attempts:[{candidate_id:"preserved",additive_repair:additive,
          additional_repair_cycles:[{cycle:2,source_revision:"source",status:"running"},{cycle:3,repair:{status:"running"}}]}],count:1,accepted:1,
          rows:[["preserved","第 1 轮 · 补线修复","已修复","1 / 4","1 / 0"],
            ["preserved","第 3 轮 · 自动修复","执行中","0 / 未知","未知 / 未知"]]},
      ];
      for (const sample of repairCases) {
        await page.evaluate(({attempts,project,revision}) => {
          dialog("Completion repair result",jobHtml({project,status:"completed",stage:"complete",events:[],result:{steps:{complete:{
            stage:"finished",revision,options:{candidate_count:3,placement_mode:"preserve"},elapsed_seconds:1,attempts}}}}));
        },{attempts:sample.attempts,project,revision});
        assert.deepEqual(await page.locator(".completion-repairs tbody tr").evaluateAll(rows =>
          rows.map(row => [...row.cells].map(cell => cell.textContent))),sample.rows,sample.name);
        if (sample.rows.length) {
          assert.equal(await page.locator(".completion-repairs > p").textContent(),`修复尝试 ${sample.count} · 已采用 ${sample.accepted}`);
          assert(await page.locator("#dialog").evaluate(el => el.scrollWidth <= el.clientWidth+1));
        } else assert.equal(await page.locator(".completion-repairs").count(),0);
        if (["additive-only","both-phases","three-cycles"].includes(sample.name))
          await page.screenshot({path:path.join(output,`${name}-${sample.name}.png`),fullPage:true});
        await page.locator("#dialog-close").click();
      }
      const viaIds = Array.from({length:6},(_,i)=>`via-${i}`);
      const comparison = {accepted:true,before_by_net:{"/TDI":2,GND:0},after_by_net:{"/TDI":1,GND:0},reasons:[]};
      const viaCases = [
        {name:"zero",cleanup:{status:"improved",revision:"cleaned",proof:{removed_ids:[]},comparison},removed:"0",accepted:true},
        {name:"six",cleanup:{status:"improved",revision:"cleaned",removed_ids:[...viaIds,"planned-only"],proof:{removed_ids:viaIds},comparison},removed:"6",accepted:true},
        {name:"blocked-plan",cleanup:{status:"blocked",source_revision:revision,revision,removed_ids:viaIds,reason:"Proof rejected"},removed:"0"},
        {name:"blocked-proof",cleanup:{status:"blocked",source_revision:revision,revision,candidate_revision:"rejected-child",removed_ids:viaIds,
          proof:{removed_ids:viaIds},comparison:{...comparison,accepted:false,after_by_net:{"/TDI":3,GND:0,"/NEW":1},reasons:["Native regression"]}},removed:"0"},
        {name:"failed",cleanup:{status:"failed",revision,proof:{removed_ids:viaIds},reason:"Native check failed"},removed:"0"},
        {name:"unconfirmed",cleanup:{status:"improved",revision,proof:{removed_ids:viaIds}},removed:"0"},
        {name:"missing-proof",cleanup:{status:"improved",revision:"cleaned",removed_ids:viaIds,comparison},removed:"未知",accepted:true},
      ];
      for (const sample of viaCases) {
        await page.evaluate(({project,revision,cleanup}) => {
          dialog("Via cleanup result",jobHtml({project,status:"running",stage:"complete",
            events:[{created:"2026-09-13T04:00:00",stage:"complete",state:"via_cleanup"},
              {created:"2026-09-13T03:59:00",stage:"verify",status:"blocked"}],result:{steps:{complete:{
              stage:"via_cleanup",revision,options:{candidate_count:1,placement_mode:"preserve"},elapsed_seconds:1,
              attempts:[{candidate_id:"preserved",status:"blocked",via_cleanup:cleanup,
                verification:{status:"blocked",drc:{warnings:47},erc:{warnings:16}}}]}}}}));
        },{project,revision,cleanup:sample.cleanup});
        assert.equal(await page.locator(".completion-result > h3").textContent(),"冗余过孔检查");
        assert.equal(await page.locator(".completion-via-cleanup").count(),1);
        assert.equal(await page.locator(".via-removed-count").textContent(),sample.removed,sample.name);
        assert((await page.locator(".via-removed-count").evaluate(el=>el.parentElement.textContent)).includes("本次清理采用删除"));
        assert((await page.locator(".via-retained").textContent()).startsWith(sample.accepted ? "清理阶段保留版本：" : "未采用清理结果，保留旧版："));
        assert.equal(await page.locator(".via-retained code").textContent(),sample.cleanup.revision);
        if (sample.cleanup.reason)
          assert((await page.locator(".completion-via-cleanup .job-error").textContent()).includes(sample.cleanup.reason));
        if (sample.name === "six")
          assert.deepEqual(await page.locator(".completion-via-cleanup tbody tr").evaluateAll(rows=>rows.map(row=>[...row.cells].map(c=>c.textContent))),[["/TDI","2","1"],["GND","0","0"]]);
        if (sample.name === "blocked-proof") {
          assert((await page.locator(".completion-via-cleanup").textContent()).includes("候选未连接前 / 后（未采用）"));
          assert((await page.locator(".completion-via-cleanup .job-error").textContent()).includes("Native regression"));
          assert.deepEqual(await page.locator(".completion-via-cleanup tbody tr").evaluateAll(rows=>rows.map(row=>[...row.cells].map(c=>c.textContent))),[["/NEW","0","1"],["/TDI","2","3"],["GND","0","0"]]);
        }
        assert((await page.locator(".event-list").textContent()).includes("冗余过孔检查"));
        assert((await page.locator(".event-list").textContent()).includes("已阻断"));
        const evidence = JSON.parse(await page.locator(".evidence-json").textContent()).steps.complete.attempts[0];
        assert.deepEqual(evidence.verification,{status:"blocked",drc:{warnings:47},erc:{warnings:16}});
        assert.deepEqual(evidence.via_cleanup,sample.cleanup);
        assert(await page.locator("#dialog").evaluate(el=>el.scrollWidth<=el.clientWidth+1));
        if (["six","blocked-proof"].includes(sample.name))
          await page.screenshot({path:path.join(output,`${name}-via-${sample.name}.png`),fullPage:true});
        await page.locator("#dialog-close").click();
      }
      await page.evaluate(({project,revision}) => {
        dialog("Completion candidate diagnoses",jobHtml({project,status:"completed",stage:"complete",
          events:[{created:"2026-09-13T04:00:00",stage:"route",status:"blocked"}],result:{steps:{complete:{
            stage:"finished",revision,options:{candidate_count:4,placement_mode:"preserve"},elapsed_seconds:1,attempts:[
              {candidate_id:"preserved",status:"passed",metrics:{unconnected:0,drc_errors:0},
                diagnosis:{category:"connections_incomplete"},routing:{status:"blocked",reason:"Initial routing failure"}},
              {candidate_id:"passed-other",status:"passed",diagnosis:{category:"old failure"}},
              {candidate_id:"blocked",status:"blocked",diagnosis:{category:"connections_incomplete"}},
              {candidate_id:"rejected",status:"rejected",diagnosis:{category:"router_failure"}},
            ]}}}}));
      },{project,revision});
      await assertPassedCandidateDiagnoses(page,2);
      assert.deepEqual(await page.locator(".completion-result > .inventory-scroll tbody tr td:last-child").allTextContents(),
        ["","","连接未完成","布线器失败"]);
      assert((await page.locator(".event-list").textContent()).includes("已阻断"));
      const historical = JSON.parse(await page.locator(".evidence-json").textContent()).steps.complete.attempts[0];
      assert.equal(historical.diagnosis.category,"connections_incomplete");
      assert.equal(historical.routing.status,"blocked");
      await page.screenshot({path:path.join(output,`${name}-candidate-diagnoses.png`),fullPage:true});
      await page.locator("#dialog-close").click();
      if (offline) {
        assert.deepEqual(errors,[]);
        results.push({viewport:name,modalLayout,repair_rendering_cases:repairCases.length,via_cleanup_cases:viaCases.length,intercepted_submissions:writes.length,errors});
        await context.close();
        continue;
      }
      const actual = await page.evaluate(async id => {
        const job = await api("/jobs/"+id);
        await loadProject(job.project,job.result.revision);
        dialog("自动完成验收",jobHtml(job));
        return job.result.steps.complete;
      },jobId);
      assert.equal(actual.status,"completed");
      assert.equal(actual.best.unconnected,0);
      assert.equal(await page.locator(".completion-result a").textContent(),actual.revision);
      assert(await page.locator(".completion-result tbody tr").count()>0);
      const passedCandidates = actual.attempts.filter(attempt=>attempt.status === "passed").length;
      assert(passedCandidates>0,"Completed job has no passed candidate");
      await assertPassedCandidateDiagnoses(page,passedCandidates);
      await page.screenshot({path:path.join(output,`${name}-actual-job.png`),fullPage:true});
      await page.locator("#dialog-close").click();
      assert(await page.locator("#run-completion").isDisabled(),"Routed output incorrectly offered for whole-board completion");
      await page.locator('[data-tab="checks"]').click();
      await page.locator("#toast").waitFor({state:"hidden"});
      await page.waitForTimeout(300);
      const layout = await page.evaluate(()=>{
        const bar=document.querySelector(".projectbar").getBoundingClientRect();
        const band=document.querySelector("#revision-status").getBoundingClientRect();
        const c=document.querySelector("#board"), pixels=c.getContext("2d").getImageData(0,0,c.width,c.height).data, colors=new Set();
        for(let i=0;i<pixels.length;i+=28)colors.add(`${pixels[i]},${pixels[i+1]},${pixels[i+2]}`);
        return {page:document.documentElement.scrollWidth<=innerWidth+1,unobscured:bar.bottom<=band.top &&
          [...document.querySelectorAll(".project-actions button")].every(el=>el.getBoundingClientRect().bottom<=bar.bottom),colors:colors.size};
      });
      assert(layout.page && layout.unobscured && layout.colors>20,`${name}: ${JSON.stringify(layout)}`);
      assert((await page.locator("#revision-status").textContent()).includes("工程检查通过"));
      await page.screenshot({path:path.join(output,`${name}-passed.png`),fullPage:true});
      assert.deepEqual(errors,[]);
      results.push({viewport:name,layout,job_id:jobId,revision:actual.revision,intercepted_submissions:writes.length,errors});
      await context.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output,"results.json"),JSON.stringify(results,null,2));
  }
  console.log(JSON.stringify({status:"passed",scope:offline ? "Offline completion UI contract and result rendering" : "Intercepted UI submissions and real completed MCP job rendering",results}));
})().catch(e=>{console.error(e);process.exitCode=1;});

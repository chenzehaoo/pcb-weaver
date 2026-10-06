"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const url = new URL(process.argv[2]), output = path.resolve(process.argv[3]);
  const project = url.searchParams.get("project"), revision = url.searchParams.get("revision");
  assert(project && revision);
  const expectedStatus = process.argv[5] || "completed";
  const expectedMissing = process.argv[6] === undefined ? 0 : Number(process.argv[6]);
  const operation = process.argv[7] || "auto_repair";
  const autonomous = process.argv[8] === "autonomous";
  assert(["auto_repair", "reference_repair"].includes(operation));
  assert(["completed", "blocked"].includes(expectedStatus));
  assert(Number.isInteger(expectedMissing) && expectedMissing >= 0);
  assert.equal(expectedStatus === "completed", expectedMissing === 0);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const results = [];
  try {
    for (const [name, viewport] of [["desktop", {width:1440,height:1000}], ["mobile", {width:390,height:844}], ["narrow", {width:320,height:740}]]) {
      const context = await browser.newContext({ viewport });
      const writes = [], errors = [];
      // UI contract tests intercept mutations; native execution is tested separately.
      await context.route("**/api/**", async route => {
        if (["GET", "HEAD"].includes(route.request().method())) return route.continue();
        assert.equal(new URL(route.request().url()).pathname, "/api/jobs");
        assert.equal(route.request().method(), "POST");
        writes.push(route.request().postDataJSON());
        await route.fulfill({json:{id:"ui-auto-repair",status:"queued"}});
      });
      const page = await context.newPage();
      page.on("pageerror", e => errors.push(e.message));
      await page.goto(url.href);
      await page.waitForFunction(() => window.pcbWorkbench?.inspection && !document.querySelector("#run-auto-repair").disabled);
      await page.waitForTimeout(300);
      if (process.argv[4]) {
        const displayed = await page.evaluate(() => {
          const v = pcbWorkbench.inspection.verification;
          return {revision:pcbWorkbench.revision,status:v.status,missing:v.drc.unconnected,
            drc_errors:v.drc.errors,erc_errors:v.erc.errors};
        });
        assert.deepEqual(displayed,{revision,status:expectedStatus === "completed" ? "passed" : "blocked",
          missing:expectedMissing,drc_errors:expectedMissing,erc_errors:0});
        assert.equal(await page.locator("#revision-status").getAttribute("data-revision"),revision);
      }
      const workbench = await page.evaluate(() => {
        const bar = document.querySelector(".projectbar").getBoundingClientRect();
        const status = document.querySelector("#revision-status").getBoundingClientRect();
        const actions = [...document.querySelectorAll(".project-actions button")];
        const canvas = document.querySelector("#board");
        const pixels = canvas.getContext("2d").getImageData(0,0,canvas.width,canvas.height).data;
        const colors = new Set();
        for (let i=0;i<pixels.length;i+=28) colors.add(`${pixels[i]},${pixels[i+1]},${pixels[i+2]}`);
        return {unobscured:actions.every(el => el.getBoundingClientRect().bottom <= bar.bottom) && bar.bottom <= status.top,
          colors:colors.size,page:document.documentElement.scrollWidth <= innerWidth+1};
      });
      assert(workbench.unobscured && workbench.page && workbench.colors > 20);
      await page.screenshot({path:path.join(output, `${name}-workbench.png`),fullPage:true});
      await page.locator("#run-auto-repair").click();
      assert.equal(await page.locator('[name="neckdown"]').isChecked(), false);
      assert.equal(await page.locator('[name="adjustment"]').isChecked(), false);
      assert.equal(await page.locator('[name="multinet"]').isChecked(), false);
      assert(await page.locator('[name="region-area"]').isDisabled());
      assert(await page.locator('[name="proposal-timeout"]').isDisabled());
      await page.locator('[name="attempts"]').fill("25");
      await page.locator("#dialog-submit").click();
      assert.equal(writes.length, 0);
      await page.locator('[name="attempts"]').fill("6");
      await page.locator('[name="budget"]').fill("29");
      await page.locator("#dialog-submit").click();
      assert.equal(writes.length, 0);
      await page.locator('[name="budget"]').fill("600");
      const layout = await page.evaluate(() => {
        const el = document.querySelector("#dialog"), r = el.getBoundingClientRect();
        return {page:document.documentElement.scrollWidth <= innerWidth+1,
          modal:el.scrollWidth <= el.clientWidth+1,
          bounds:r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight};
      });
      assert.deepEqual(layout, {page:true,modal:true,bounds:true});
      await page.screenshot({path:path.join(output, `${name}-options.png`), fullPage:true});
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(() => !document.querySelector("#dialog").open);
      assert.deepEqual(writes[0], {operation:"auto_repair",project,revision,
        auto_options:{max_attempts:6,time_budget_seconds:600,allow_neckdown:false,allow_local_adjustment:false}});
      await page.locator("#run-auto-repair").click();
      await page.locator('[name="neckdown"]').check();
      await page.locator('[name="adjustment"]').check();
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(() => !document.querySelector("#dialog").open);
      assert(writes[1].auto_options.allow_neckdown && writes[1].auto_options.allow_local_adjustment);
      await page.locator("#run-auto-repair").click();
      await page.evaluate(() => { pcbWorkbench.revision = "stale-target"; });
      await page.locator("#dialog-submit").click();
      await page.waitForTimeout(100);
      assert.equal(writes.length, 2, "Stale revision submitted");
      assert(await page.locator("#dialog").evaluate(el => el.open));
      await page.locator("#dialog-close").click();
      await page.evaluate(r => { pcbWorkbench.revision = r; }, revision);
      if (autonomous) {
        await page.locator("#run-auto-repair").click();
        await page.locator('[name="multinet"]').check();
        assert.equal(await page.locator('[name="region-area"]').inputValue(), "900");
        assert.equal(await page.locator('[name="proposal-timeout"]').inputValue(), "60");
        await page.locator('[name="region-area"]').fill("2501");
        await page.locator("#dialog-submit").click();
        assert.equal(writes.length,2);
        await page.locator('[name="region-area"]').fill("2500");
        await page.locator('[name="proposal-timeout"]').fill("181");
        await page.locator("#dialog-submit").click();
        assert.equal(writes.length,2);
        await page.locator('[name="proposal-timeout"]').fill("180");
        await page.locator('[name="multinet"]').uncheck();
        assert(await page.locator('[name="region-area"]').isDisabled());
        await page.locator('[name="multinet"]').check();
        await page.locator('[name="attempts"]').fill("24");
        await page.locator('[name="budget"]').fill("1800");
        await page.locator('[name="neckdown"]').check();
        await page.locator('[name="adjustment"]').check();
        await page.screenshot({path:path.join(output,`${name}-autonomous-options.png`),fullPage:true});
        await page.locator("#dialog-submit").click();
        await page.waitForFunction(() => !document.querySelector("#dialog").open);
        assert.deepEqual(writes[2],{operation:"auto_repair",project,revision,
          auto_options:{max_attempts:24,time_budget_seconds:1800,allow_neckdown:true,allow_local_adjustment:true,
            allow_multinet:true,max_region_area_mm2:2500,proposal_timeout_seconds:180}});
      }
      if (operation === "reference_repair") {
        await page.locator("#run-auto-repair").click();
        await page.locator("#reference-repair-open").click();
        await page.locator('[name="reference-project"]').waitFor();
        assert.equal(await page.locator('[name="reference-project"]').inputValue(), "");
        assert(await page.locator('[name="reference-revision"]').isDisabled());
        await page.locator("#dialog-submit").click();
        assert.equal(writes.length, 2);
        await page.locator('[name="reference-project"]').selectOption("system-clearance-acceptance");
        await page.waitForFunction(() => !document.querySelector('[name="reference-revision"]').disabled);
        await page.locator('[name="reference-revision"]').selectOption("r-1bbf979cb3cc4206");
        const referenceLayout = await page.locator("#dialog").evaluate(el => {
          const r = el.getBoundingClientRect();
          return el.scrollWidth <= el.clientWidth+1 && r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;
        });
        assert(referenceLayout);
        await page.screenshot({path:path.join(output, `${name}-reference-options.png`),fullPage:true});
        await page.evaluate(() => { pcbWorkbench.revision = "stale-reference-target"; });
        await page.locator("#dialog-submit").click();
        await page.waitForTimeout(100);
        assert.equal(writes.length, 2);
        await page.evaluate(r => { pcbWorkbench.revision = r; }, revision);
        await page.locator("#dialog-submit").click();
        await page.waitForFunction(() => !document.querySelector("#dialog").open);
        assert.deepEqual(writes[2], {operation,project,revision,
          reference_project:"system-clearance-acceptance",reference_revision:"r-1bbf979cb3cc4206"});
        // Out-of-order project responses must not replace the current reference options.
        await page.locator("#run-auto-repair").click();
        await page.locator("#reference-repair-open").click();
        await page.locator('[name="reference-project"]').waitFor();
        const slow = "**/api/projects/system-clearance-acceptance/revisions";
        await context.route(slow, async route => {
          await new Promise(resolve => setTimeout(resolve, 800));
          await route.fulfill({json:[{id:"r-stale-response",operation:"import"}]});
        });
        await page.locator('[name="reference-project"]').selectOption("system-clearance-acceptance");
        await page.locator('[name="reference-project"]').selectOption(project);
        await page.waitForFunction(() => !document.querySelector('[name="reference-revision"]').disabled);
        await page.waitForTimeout(1000);
        assert.equal(await page.locator('[name="reference-revision"] option[value="r-stale-response"]').count(), 0);
        await context.unroute(slow);
        await page.locator("#dialog-close").click();
      }
      await page.evaluate(({project,revision}) => {
        dialog("Automatic repair contract fixture", jobHtml({project,status:"blocked",stage:"blocked",events:[],
          result:{steps:{auto_repair:{stage:"finished",revision,before_unconnected:2,after_unconnected:1,
            options:{max_attempts:6},attempts:[{net:"TEST",strategy:"additive",status:"accepted",
              comparison:{before_unconnected:2,after_unconnected:1}}]}}}}));
      }, {project,revision});
      assert.equal(await page.locator(".auto-repair-result tbody tr").count(), 1);
      assert((await page.locator(".auto-repair-result a").getAttribute("href")).includes(revision));
      await page.screenshot({path:path.join(output, `${name}-progress.png`),fullPage:true});
      if (process.argv[4]) {
        await page.locator("#dialog-close").click();
        const actual = await page.evaluate(async ({id, operation}) => {
          const job = await api("/jobs/"+id);
          dialog("Automatic repair acceptance", jobHtml(job));
          const flow = job.result.steps[operation];
          return {status:job.status,revision:flow.revision,missing:flow.after_unconnected,attempts:flow.attempts.length};
        }, {id:process.argv[4],operation});
        const {attempts,...outcome} = actual;
        assert.deepEqual(outcome,{status:expectedStatus,revision,missing:expectedMissing});
        assert.equal(await page.locator(".auto-repair-result tbody tr").count(),attempts);
        assert(await page.locator("#dialog").evaluate(el => {
          const r = el.getBoundingClientRect();
          return el.scrollWidth <= el.clientWidth+1 && r.left >= 0 && r.right <= innerWidth+1 &&
            r.top >= 0 && r.bottom <= innerHeight+1 && document.documentElement.scrollWidth <= innerWidth+1;
        }));
        assert.equal(await page.locator(".auto-repair-result a").textContent(),revision);
        if (autonomous) {
          assert((await page.locator(".auto-repair-result").textContent()).includes("无参考多网络重布"));
          assert.equal(await page.locator(".reference-provenance").count(),0);
        }
        if (operation === "reference_repair") {
          assert.equal(await page.locator(".auto-repair-result tbody tr").count(), 3);
          assert((await page.locator(".reference-provenance").textContent()).includes("system-clearance-acceptance"));
        }
        await page.screenshot({path:path.join(output,`${name}-actual-job.png`),fullPage:true});
      }
      assert.deepEqual(errors, []);
      results.push({viewport:name,operation,autonomous,layout,workbench,actual_job:process.argv[4] || null,
        expected_job_status:process.argv[4] ? expectedStatus : null,
        expected_missing:process.argv[4] ? expectedMissing : null,
        intercepted_submissions:writes.length,stale_prevented:true,errors});
      await context.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output,"results.json"), JSON.stringify(results,null,2));
  }
  console.log(JSON.stringify({status:"passed",scope:"UI contracts; all mutations intercepted",results}));
})().catch(e => {console.error(e);process.exitCode=1;});

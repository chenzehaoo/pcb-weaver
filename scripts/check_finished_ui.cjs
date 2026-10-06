"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

(async () => {
  const url = new URL(process.argv[2]);
  const output = path.resolve(process.argv[3]);
  const project = url.searchParams.get("project"), revision = url.searchParams.get("revision");
  assert(project && revision);
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const results = [];
  fs.mkdirSync(output, { recursive: true });
  try {
    for (const [name, viewport] of [["desktop", { width: 1440, height: 1000 }], ["mobile", { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport });
      const errors = [], mutations = [];
      await context.route("**/api/**", route => {
        if (["GET", "HEAD"].includes(route.request().method())) return route.continue();
        mutations.push(route.request().url());
        return route.abort();
      });
      const page = await context.newPage();
      page.on("pageerror", error => errors.push(error.message));
      await page.goto(url.href);
      await page.waitForFunction(({ project, revision }) => window.pcbWorkbench?.project === project &&
        pcbWorkbench.revision === revision && pcbWorkbench.inspection?.verification,
      { project, revision });
      const verification = await page.evaluate(() => pcbWorkbench.inspection.verification);
      assert.equal(verification.status, "passed");
      assert.equal(verification.drc.errors, 0);
      assert.equal(verification.drc.unconnected, 0);
      assert.equal(verification.erc.errors, 0);
      assert.equal(await page.evaluate(() => pcbWorkbench.tab), "checks");
      const summary = page.locator("#revision-status");
      assert(await summary.isVisible());
      assert.equal(await summary.getAttribute("data-revision"), revision);
      assert((await summary.textContent()).includes("工程检查通过"));
      assert((await page.locator("#revision-select option:checked").textContent()).includes("通过"));
      assert((await summary.boundingBox()).y < viewport.height);
      await page.locator('[data-tab="jobs"]').click();
      await page.reload();
      await page.waitForFunction(() => pcbWorkbench.inspection?.verification);
      assert.equal(await page.evaluate(() => pcbWorkbench.tab), "jobs");
      await page.locator("#show-revision-checks").click();
      assert.equal(new URL(page.url()).searchParams.get("tab"), "checks");
      await page.locator('[data-tab="checks"]').click();
      await page.waitForTimeout(350);
      const canvas = await page.locator("#board").evaluate(el => {
        const pixels = el.getContext("2d").getImageData(0, 0, el.width, el.height).data;
        const colors = new Set();
        for (let i = 0; i < pixels.length; i += 28) colors.add(`${pixels[i]},${pixels[i+1]},${pixels[i+2]}`);
        return { width: el.width, height: el.height, colors: colors.size };
      });
      assert(canvas.width > 0 && canvas.height > 0 && canvas.colors > 20, "Blank board canvas");
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Page overflow");
      await page.screenshot({ path: path.join(output, `${name}-passed.png`), fullPage: true });
      for (const tab of ["components", "nets", "routing"]) {
        await page.locator(`[data-tab="${tab}"]`).click();
        const kind = tab === "routing" ? "tracks" : tab;
        await page.locator(`.inventory[data-kind="${kind}"] [data-inventory-row]`).first().waitFor();
        assert(await page.locator(`.inventory[data-kind="${kind}"] [data-inventory-row]`).count() > 0, `Empty ${tab} table`);
      }
      if (process.argv[4]) {
        const baseline = process.argv[4];
        const hiddenWhileLoading = await page.evaluate(({ project, baseline }) => {
          window.statusSwitch = loadProject(project, baseline);
          return document.querySelector("#revision-status").hidden;
        }, { project, baseline });
        assert(hiddenWhileLoading, "Previous passed badge survives revision loading");
        await page.evaluate(() => window.statusSwitch);
        assert.equal(await summary.getAttribute("data-revision"), baseline);
        assert.equal(await page.locator("#revision-status .passed").count(), 0);
        assert((await summary.textContent()).includes("已阻断"));
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(mutations, []);
      results.push({ viewport: name, revision, status: verification.status,
        drc: { errors: verification.drc.errors, warnings: verification.drc.warnings, unconnected: verification.drc.unconnected },
        erc: { errors: verification.erc.errors, warnings: verification.erc.warnings }, canvas, errors, mutations });
      await context.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(results, null, 2));
  }
  console.log(JSON.stringify({ status: "passed", results }));
})().catch(error => { console.error(error); process.exitCode = 1; });

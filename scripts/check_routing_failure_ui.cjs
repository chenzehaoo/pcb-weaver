"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

async function main() {
  const output = path.resolve(process.argv[2] || "docs/validation/routing-failure-ui");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "msedge" });
  const results = [];
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto("http://127.0.0.1:8765/?project=system-controller&revision=r-48eb2d3661aa4575");
      await page.waitForFunction(() => pcbWorkbench.inspection && pcbWorkbench.project === "system-controller");
      await page.evaluate(() => showJob("job-5f665ee28c454daa"));
      const text = await page.locator(".job-error").innerText();
      assert.match(text, /自动布线超时/);
      assert.match(text, /1800 秒/);
      assert.match(text, /result JSON/);
      const layout = await page.evaluate(() => {
        const modal = document.querySelector("#dialog");
        const rect = modal.getBoundingClientRect();
        return { page: document.documentElement.scrollWidth <= innerWidth + 1,
          modal: modal.scrollWidth <= modal.clientWidth + 1,
          bounds: rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight };
      });
      assert.deepEqual(layout, { page: true, modal: true, bounds: true });
      await page.screenshot({ path: path.join(output, `${viewport.width}.png`) });
      await page.locator("#dialog-close").click();
      await page.evaluate(() => showJob("job-537ad4d30aa74ea3"));
      const importText = await page.locator(".job-error").innerText();
      assert.match(importText, /导入布线结果失败/);
      assert.match(importText, /0\.15 < 0\.2/);
      await page.screenshot({ path: path.join(output, `${viewport.width}-import.png`) });
      const fixtures = await page.evaluate(() => {
        const blocking = (details, reason = "Routing operation did not complete") => ({ result: { blocking: { reason, details } } });
        const nativeFailure = { status: "failed", reason: "<img src=x onerror=alert(1)>" };
        return {
          export: jobFailureHtml(blocking([nativeFailure])),
          import: jobFailureHtml(blocking([{ status: "ok" }, { status: "ok" }, nativeFailure])),
          unrelated: jobFailureHtml(blocking([nativeFailure], "Verification failed")),
          empty: jobFailureHtml({ result: null }),
        };
      });
      assert.match(fixtures.export, /导出布线输入失败/);
      assert.match(fixtures.import, /导入布线结果失败/);
      assert(!fixtures.export.includes("<img"));
      assert.match(fixtures.unrelated, /Verification failed/);
      assert.equal(fixtures.empty, "");
      assert.deepEqual(errors, []);
      results.push({ viewport, text, importText, layout, fixtures: "passed", browser_errors: errors });
      await page.close();
    }
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify({ status: "passed", results }, null, 2));
    console.log(JSON.stringify({ status: "passed", output }));
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });

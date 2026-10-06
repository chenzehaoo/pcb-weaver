"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const url = new URL(process.argv[2] || "http://127.0.0.1:8766/?project=system-mcp-acceptance&revision=r-9b5f5075e65e4931");
const output = path.resolve(process.argv[3] || "docs/validation/repair-ui");
const expectedFindings = Number(process.argv[4] || 7);
const project = url.searchParams.get("project"), revision = url.searchParams.get("revision");
assert(project && revision, "Pass an exact project and revision URL");
const endpoint = `/api/projects/${encodeURIComponent(project)}/revisions/${encodeURIComponent(revision)}/repair-diagnosis`;
const results = [];

async function get(relative) {
  const response = await fetch(new URL(relative, url), { signal: AbortSignal.timeout(20000) });
  assert(response.ok, `GET ${relative}: ${response.status}`);
  return response.json();
}
async function ready(page) {
  await page.waitForFunction(({ project, revision }) => pcbWorkbench.project === project &&
    pcbWorkbench.revision === revision && pcbWorkbench.inspection && !document.querySelector("#run-repair").disabled,
  { project, revision });
}
async function open(page) {
  await page.locator("#run-repair").click();
  await page.locator("#repair-proposal").waitFor();
}
async function noOverflow(page) {
  const metrics = await page.evaluate(() => {
    const modal = document.querySelector("#dialog");
    const rect = modal.getBoundingClientRect();
    const controls = [...modal.querySelectorAll("input,select,textarea,button")].filter((el) => el.getClientRects().length);
    return { page: document.documentElement.scrollWidth <= innerWidth + 1,
      modal: modal.scrollWidth <= modal.clientWidth + 1,
      bounds: rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight,
      controls: controls.every((el) => { const r = el.getBoundingClientRect(); return r.left >= rect.left && r.right <= rect.right; }) };
  });
  assert.deepEqual(metrics, { page: true, modal: true, bounds: true, controls: true });
  return metrics;
}
async function stale(page, diagnosis, mode, alternate, otherRevision) {
  let release, arrived;
  const gate = new Promise((resolve) => release = resolve);
  const waiting = new Promise((resolve) => arrived = resolve);
  const pattern = `**${endpoint}`;
  await page.route(pattern, async (route) => {
    arrived();
    await gate;
    await route.fulfill({ json: diagnosis });
  });
  try {
    await page.locator("#run-repair").click();
    await waiting;
    assert(await page.locator("#dialog-submit").isDisabled());
    if (mode === "project") {
      await page.evaluate((p) => loadProject(p), alternate);
    } else if (mode === "revision") {
      await page.evaluate((r) => { pcbWorkbench.revision = r; return loadRevision(); }, otherRevision);
    } else {
      await page.locator("#dialog-close").click();
      if (mode === "replace") await page.locator("#run-plan").click();
    }
    release();
    await page.waitForTimeout(150);
    assert.equal(await page.locator("#repair-proposal").count(), 0, "Late diagnosis replaced another view");
    assert.equal(await page.locator("#dialog").evaluate((el) => el.open), mode === "replace");
    if (mode === "replace") {
      assert.equal(await page.locator("#dialog-title").textContent(), "布局规划");
      await page.locator("#dialog-close").click();
    }
  } finally {
    release();
    await page.unroute(pattern);
    await page.evaluate(({ project, revision }) => loadProject(project, revision), { project, revision });
    await ready(page);
  }
}

(async () => {
  // Real recorded diagnosis, never native verification or repair execution.
  const diagnosis = await get(endpoint);
  assert.equal(diagnosis.status, "ok");
  assert.equal(diagnosis.revision, revision);
  assert.equal(diagnosis.proposals.length, expectedFindings, "Unexpected recorded unconnected-item count");
  const projects = await get("/api/projects");
  const alternate = projects.find((p) => p.project !== project)?.project;
  const revisions = await get(`/api/projects/${encodeURIComponent(project)}/revisions`);
  const otherRevision = revisions.find((r) => r.id !== revision)?.id;
  assert(alternate && otherRevision, "Stale-response checks require another project and revision");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    for (const [name, viewport] of [["desktop", { width: 1440, height: 1000 }], ["mobile", { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport });
      const writes = [], errors = [], forbidden = [];
      // Intercept every browser mutation before it can reach the real queue.
      await context.route("**/api/**", async (route) => {
        const request = route.request();
        if (["GET", "HEAD"].includes(request.method())) return route.continue();
        if (request.method() === "POST" && new URL(request.url()).pathname === "/api/jobs") {
          const data = request.postDataJSON();
          writes.push(data);
          return route.fulfill({ json: { id: `ui-intercept-${writes.length}`, status: "queued", request: data } });
        }
        forbidden.push({ method: request.method(), url: request.url() });
        return route.abort();
      });
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(url.href);
      await ready(page);
      await open(page);
      assert.equal(writes.length, 0, "Opening repair must not submit a job");
      assert.equal(await page.locator("#repair-proposal option").count(), expectedFindings);
      assert(await page.locator("#repair-net").evaluate((el) => el.readOnly));
      for (let i = 0; i < diagnosis.proposals.length; i++) {
        const proposal = diagnosis.proposals[i];
        await page.locator("#repair-proposal").selectOption(String(i));
        assert.equal(await page.locator("#repair-net").inputValue(), proposal.nets.join(", "));
        if (proposal.contact_geometry?.status === "ok") {
          const contact = await page.locator("#repair-contact").textContent();
          assert(contact.includes(Number(proposal.contact_geometry.projected_gap_lower_bound_mm).toFixed(3)));
          assert(contact.includes("路径可行性未验证"));
        }
        assert.deepEqual(await page.locator("#repair-roi input").evaluateAll((els) => els.map((el) => el.valueAsNumber)), proposal.region);
        const label = await page.locator(`#repair-proposal option[value="${i}"]`).textContent();
        for (const item of proposal.items) assert(label.includes(item.description || item.uuid));
      }
      await page.locator("#repair-proposal").selectOption("0");
      assert.equal(await page.locator("#repair-passes").inputValue(), "3");
      assert.equal(await page.locator("#repair-remove").inputValue(), "");
      assert(!await page.locator("#repair-delete-confirm").isChecked());
      const layoutChecks = await noOverflow(page);
      await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
      const region = [...diagnosis.proposals[0].region];
      await page.locator("#repair-roi-0").fill(String(region[2]));
      assert(await page.locator("#dialog-submit").isDisabled());
      region[0] += 0.1;
      await page.locator("#repair-roi-0").fill(String(region[0]));
      for (const invalid of ["0", "11", "1.5", ""]) {
        await page.locator("#repair-passes").fill(invalid);
        assert(await page.locator("#dialog-submit").isDisabled());
      }
      await page.locator("#repair-passes").fill("3");
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(() => !document.querySelector("#dialog").open);
      assert.deepEqual(writes[0], { operation: "repair", project, revision,
        repair_nets: diagnosis.proposals[0].nets, repair_region: region, repair_remove_ids: [], passes: 3 });
      await open(page);
      await page.locator(".repair-deletions summary").click();
      const ids = diagnosis.proposals[0].items.map((item) => item.uuid);
      await page.locator("#repair-remove").fill(ids[0]);
      assert(await page.locator("#dialog-submit").isDisabled());
      await page.locator("#repair-delete-confirm").check();
      assert(!await page.locator("#dialog-submit").isDisabled());
      await page.locator("#repair-remove").fill(ids.join("\n"));
      assert(!await page.locator("#repair-delete-confirm").isChecked());
      await page.locator("#repair-proposal").selectOption("1");
      assert.equal(await page.locator("#repair-remove").inputValue(), "");
      await page.locator("#repair-remove").fill("not-a-uuid");
      await page.locator("#repair-delete-confirm").check();
      assert(await page.locator("#dialog-submit").isDisabled());
      await page.locator("#repair-remove").fill(ids[0]);
      await page.locator("#repair-delete-confirm").check();
      await page.locator("#repair-passes").fill("10");
      await noOverflow(page);
      await page.screenshot({ path: path.join(output, `${name}-deletions.png`), fullPage: true });
      await page.locator("#dialog-submit").click();
      await page.waitForFunction(() => !document.querySelector("#dialog").open);
      assert.deepEqual(writes[1], { operation: "repair", project, revision,
        repair_nets: diagnosis.proposals[1].nets, repair_region: diagnosis.proposals[1].region,
        repair_remove_ids: [ids[0]], passes: 10 });
      for (const changed of [{ ...diagnosis, status: "blocked" }, { ...diagnosis, proposals: [] },
        { ...diagnosis, proposals: [{ ...diagnosis.proposals[0], region: null }] }]) {
        const pattern = `**${endpoint}`;
        await page.route(pattern, (route) => route.fulfill({ json: changed }));
        await open(page);
        assert(await page.locator("#dialog-submit").isDisabled());
        await page.locator("#dialog-form").dispatchEvent("submit");
        assert.equal(writes.length, 2);
        await page.locator("#dialog-close").click();
        await page.unroute(pattern);
      }
      for (const mode of ["close", "replace", "project", "revision"])
        await stale(page, diagnosis, mode, alternate, otherRevision);
      assert.equal(writes.length, 2);
      assert.deepEqual(errors, []);
      assert.deepEqual(forbidden, []);
      results.push({ viewport: name, proposals: diagnosis.proposals.length, intercepted_submissions: writes.length,
        actual_enqueues: 0, layout_checks: layoutChecks, stale_close: true, stale_replace: true, stale_project: true, stale_revision: true });
      await context.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(results, null, 2));
  }
  console.log(JSON.stringify({ status: "passed", results }, null, 2));
})().catch((error) => { console.error(error); process.exitCode = 1; });

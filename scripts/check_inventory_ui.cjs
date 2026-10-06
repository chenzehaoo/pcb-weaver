"use strict";
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const { chromium } = require("playwright");

const url =
  process.argv[2] ||
  "http://127.0.0.1:8766/?project=system-mcp-acceptance&revision=r-9b5f5075e65e4931";
const output = path.resolve(process.argv[3] || "docs/validation/inventory-ui");
const minimum = Number(process.argv[4] || 160);
const results = [];
const present = (v) =>
  v == null || v === "" || v === "unknown" ? "未知" : String(v);

async function openTab(page, tab, kind = tab) {
  await page.locator(`[data-tab="${tab}"]`).click();
  await page.waitForSelector(`.inventory[data-kind="${kind}"]`);
}
async function allPages(page, expected) {
  await page.locator("#inventory-size").selectOption("100");
  const seen = [];
  for (let pageNo = 1; ; pageNo++) {
    assert.equal(
      Number(await page.locator("#inventory-page").inputValue()),
      pageNo,
    );
    seen.push(
      ...(await page
        .locator(".inventory-table tbody [data-inventory-row] td:first-child")
        .allTextContents()),
    );
    if (await page.locator('[data-inventory-page="next"]').isDisabled()) break;
    await page.locator('[data-inventory-page="next"]').click();
    assert(pageNo < 1000, "Pagination did not terminate");
  }
  assert.equal(
    seen.length,
    expected.length,
    "All pages must include every inventory item",
  );
  assert.deepEqual(
    [...seen].sort(),
    [...expected].sort(),
    "Pagination changed or duplicated inventory identities",
  );
  if (!(await page.locator('[data-inventory-page="first"]').isDisabled()))
    await page.locator('[data-inventory-page="first"]').click();
  return seen.length;
}
async function download(page, format, inventory) {
  const pending = page.waitForEvent("download");
  await page.locator(`[data-inventory-export="${format}"]`).click();
  const item = await pending;
  assert.equal(await item.failure(), null);
  const bytes = fs.readFileSync(await item.path());
  assert(bytes.length > 20, "Downloaded artifact is empty");
  if (format === "json") {
    const data = JSON.parse(bytes.toString("utf8"));
    assert.equal(data.project, inventory.project);
    assert.equal(data.revision, inventory.revision);
    assert.deepEqual(data.summary, inventory.summary);
  } else if (format === "exchange")
    assert.equal(bytes.subarray(0, 2).toString(), "PK");
  else {
    const text = bytes.toString("utf8").replace(/^\uFEFF/, "");
    assert(
      text.includes("\n") && !text.trimStart().startsWith("{"),
      "CSV endpoint did not return CSV",
    );
  }
  return { format, filename: item.suggestedFilename(), bytes: bytes.length };
}
async function checkStaleResponse(page, inventory) {
  const endpoint = `**/api/projects/${encodeURIComponent(inventory.project)}/revisions/${encodeURIComponent(inventory.revision)}/inventory`;
  let release, arrived;
  const gate = new Promise((resolve) => (release = resolve));
  const waiting = new Promise((resolve) => (arrived = resolve));
  await page.route(endpoint, async (route) => {
    arrived();
    await gate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(inventory),
    });
  });
  try {
    await page.evaluate(() => clearInventory());
    await page.locator('[data-tab="components"]').click();
    await waiting;
    await page.locator('[data-tab="checks"]').click();
    release();
    await page.waitForTimeout(200);
    assert.equal(
      await page.locator(".inventory").count(),
      0,
      "Late inventory overwrote checks tab",
    );
  } finally {
    release();
    await page.unroute(endpoint);
  }
  const revisions = await page.evaluate(() =>
    pcbWorkbench.revisions.map((r) => r.id),
  );
  const alternate = revisions.find((r) => r !== inventory.revision);
  if (!alternate) return { stale_tab: true, stale_revision: "not_tested" };
  let releaseVersion, versionArrived;
  const versionGate = new Promise((resolve) => (releaseVersion = resolve));
  const versionWaiting = new Promise((resolve) => (versionArrived = resolve));
  await page.route(endpoint, async (route) => {
    versionArrived();
    await versionGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(inventory),
    });
  });
  try {
    await page.evaluate(() => clearInventory());
    await page.locator('[data-tab="components"]').click();
    await versionWaiting;
    await page.locator("#revision-select").selectOption(alternate);
    await page.waitForFunction(
      (revision) =>
        pcbWorkbench.revision === revision &&
        pcbWorkbench.inventory?.revision === revision,
      alternate,
    );
    releaseVersion();
    await page.waitForTimeout(200);
    assert.equal(
      await page.locator(".inventory").getAttribute("data-revision"),
      alternate,
      "Late inventory crossed revision boundary",
    );
  } finally {
    releaseVersion();
    await page.unroute(endpoint);
  }
  await page.evaluate(
    ({ project, revision }) => loadProject(project, revision),
    inventory,
  );
  return { stale_tab: true, stale_revision: true };
}
async function checkUnavailable(page) {
  await page.locator('[data-tab="checks"]').click();
  await page.evaluate(() => clearInventory());
  await page.route("**/inventory", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "UI test: unavailable" }),
    }),
  );
  try {
    await page.locator('[data-tab="components"]').click();
    await page.waitForSelector("[data-inventory-retry]");
    assert(
      (await page.locator("#panel-content").textContent()).includes(
        "台账不可用",
      ),
    );
    assert.equal(await page.locator(".inventory-table").count(), 0);
  } finally {
    await page.unroute("**/inventory");
  }
  await page.locator("[data-inventory-retry]").click();
  await page.waitForSelector(".inventory-table");
}
async function integration(page) {
  await page.locator('[data-tab="integration"]').click();
  await page.waitForSelector(".integration-records");
  assert(
    (await page.locator(".integration-status").textContent()).includes(
      "未验证",
    ),
  );
  assert(
    (await page.locator(".integration-records tbody tr").count()) > 0,
    "OpenAPI operations not listed",
  );
  // Controlled malicious response tests client rendering, not backend capabilities.
  await page.route("**/integration/capabilities", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      json: {
        ...body,
        token: "QA_SECRET_MUST_NOT_RENDER",
        nested: { password: "QA_PASSWORD_MUST_NOT_RENDER" },
        limitations: [
          ...body.limitations,
          '<img src=x onerror="window.inventoryXss=true">',
        ],
      },
    });
  });
  try {
    await page.locator("[data-integration-retry]").click();
    await page.waitForFunction(() =>
      document
        .querySelector(".integration-records")
        ?.textContent.includes("已隐藏"),
    );
    const content = await page.locator(".integration-records").textContent();
    assert(
      !content.includes("QA_SECRET_MUST_NOT_RENDER") &&
        !content.includes("QA_PASSWORD_MUST_NOT_RENDER"),
    );
    assert.equal(await page.locator(".integration-records img").count(), 0);
    assert.equal(await page.evaluate(() => !!window.inventoryXss), false);
  } finally {
    await page.unroute("**/integration/capabilities");
  }
  await page.locator("[data-integration-retry]").click();
  await page.waitForSelector(".integration-records");
  return {
    actual_openapi: true,
    synthetic_secret_redaction: true,
    synthetic_html_escaped: true,
  };
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  try {
    for (const [viewport, width, height] of [
      ["desktop", 1600, 1000],
      ["mobile", 390, 844],
    ]) {
      const page = await browser.newPage({
        viewport: { width, height },
        acceptDownloads: true,
      });
      page.setDefaultTimeout(30000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(url);
      await page.waitForFunction(() => window.pcbWorkbench?.inspection);
      await openTab(page, "components");
      const inventory = await page.evaluate(() => pcbWorkbench.inventory);
      assert(inventory.components.length >= minimum);
      assert.equal(
        inventory.summary.component_count,
        inventory.components.length,
      );
      assert.equal(
        inventory.summary.package_count,
        new Set(inventory.components.map((r) => r.footprint)).size,
      );
      assert.equal(
        await page.locator("#inventory-package option").count(),
        inventory.summary.package_count + 1,
      );
      const pagination = {
        components: await allPages(
          page,
          inventory.components.map((c) => c.reference),
        ),
      };
      const target =
        [...inventory.components]
          .sort((a, b) => b.pads.length - a.pads.length)
          .find((c) => c.category === "integrated_circuit" && c.mpn == null) ||
        inventory.components[0];
      await page.locator("#inventory-search").fill(target.reference);
      const searchCells = await page
        .locator(".inventory-table tbody td:first-child")
        .allTextContents();
      assert(searchCells.includes(target.reference));
      await page
        .locator("#inventory-search")
        .fill("QA_NO_SUCH_COMPONENT_9d7fa");
      assert.equal(await page.locator("[data-inventory-row]").count(), 0);
      await page.locator("#inventory-search").fill("");
      const category = present(target.category);
      await page.locator("#inventory-category").selectOption(category);
      const categoryExpected = inventory.components.filter(
        (c) => present(c.category) === category,
      );
      assert(
        (await page.locator("#inventory-count").textContent()).startsWith(
          `${categoryExpected.length} /`,
        ),
      );
      await page.locator("#inventory-category").selectOption("");
      await page.locator("#inventory-package").selectOption(target.footprint);
      assert(
        (await page.locator("#inventory-count").textContent()).startsWith(
          `${inventory.components.filter((c) => c.footprint === target.footprint).length} /`,
        ),
      );
      await page.locator("#inventory-package").selectOption("");
      await page.locator("#inventory-layer").selectOption(target.layer);
      assert(
        (await page.locator("#inventory-count").textContent()).startsWith(
          `${inventory.components.filter((c) => c.layer === target.layer).length} /`,
        ),
      );
      await page.locator("#inventory-layer").selectOption("");
      await page.locator('[data-inventory-sort="pad_count"]').click();
      const counts = await page
        .locator(".inventory-table tbody tr")
        .evaluateAll((rows) => rows.map((r) => Number(r.cells[5].textContent)));
      assert.deepEqual(
        counts,
        [...counts].sort((a, b) => a - b),
      );
      await page.locator('[data-inventory-sort="pad_count"]').click();
      const descending = await page
        .locator(".inventory-table tbody tr")
        .evaluateAll((rows) => rows.map((r) => Number(r.cells[5].textContent)));
      assert.deepEqual(
        descending,
        [...descending].sort((a, b) => b - a),
      );
      await page.locator("#inventory-search").fill(target.reference);
      const targetRow = page
        .locator("[data-inventory-row]")
        .filter({
          has: page.locator("td:first-child", {
            hasText: new RegExp(`^${target.reference}$`),
          }),
        })
        .first();
      await targetRow.click();
      assert.equal(
        await page.evaluate(() => pcbWorkbench.selected),
        target.reference,
      );
      assert.equal(
        await page.locator(".inventory-pads tbody tr").count(),
        target.pads.length,
      );
      assert(
        (await page.locator("#inventory-detail").textContent()).includes(
          "未知",
        ),
      );
      assert(
        (await page.locator("#inventory-detail").textContent()).includes(
          "属性来源记录",
        ),
      );
      const semantics = await page.evaluate(() => ({
        arc: ledgerCell(
          { kind: "arc", start: [0, 0], end: [3, 4], length_mm: null },
          "length_mm",
        ),
        partial: ledgerCell(
          { length_mm: 4.5, length_status: "partial" },
          "length_mm",
        ),
        status: connectionText("not_evaluated"),
        sources: factsTable({
          basis: { method: "reference_prefix_heuristic", verified: false },
        }),
      }));
      assert.equal(semantics.arc, "未知");
      assert(semantics.partial.includes("部分"));
      assert.equal(semantics.status, "未评估");
      assert(!semantics.sources.includes("[object Object]"));
      await page.locator(".inventory-detail-heading").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: path.join(output, `${viewport}-component-detail.png`),
        fullPage: true,
      });
      await page.locator(".inventory-pads").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: path.join(output, `${viewport}-component-pins.png`),
        fullPage: true,
      });
      const exports = [];
      for (const format of ["components.csv", "json", "exchange"])
        exports.push(await download(page, format, inventory));
      await page.locator("#inventory-search").fill("");
      await page.locator("[data-inventory-close]").click();
      await page.locator("#inventory-size").selectOption("25");
      await page.locator("#fit").click();
      await page.locator("#panel-content").evaluate((node) => {
        node.scrollTop = 0;
      });
      await page.waitForTimeout(200);
      const pixels = await page.evaluate(() => {
        const c = document.querySelector("canvas"),
          bytes = c.getContext("2d").getImageData(0, 0, c.width, c.height).data,
          colors = new Set();
        for (let i = 0; i < bytes.length; i += 40)
          colors.add(`${bytes[i]},${bytes[i + 1]},${bytes[i + 2]}`);
        return {
          colors: colors.size,
          width: c.width,
          height: c.height,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      assert(
        pixels.colors > 15 &&
          pixels.width > 200 &&
          pixels.height > 100 &&
          !pixels.overflow,
        JSON.stringify(pixels),
      );
      await page.screenshot({
        path: path.join(output, `${viewport}-inventory.png`),
        fullPage: true,
      });
      await openTab(page, "nets");
      pagination.nets = await allPages(
        page,
        inventory.nets.map((n) => n.name),
      );
      const netName = await page
        .locator(".inventory-table tbody tr td:first-child")
        .first()
        .textContent();
      await page.locator("[data-inventory-row]").first().click();
      assert.equal(
        await page.evaluate(() => pcbWorkbench.selectedNet),
        netName,
      );
      const net = inventory.nets.find((n) => n.name === netName);
      assert.equal(
        await page.locator(".inventory-pads tbody tr").count(),
        net.pads.length,
      );
      exports.push(await download(page, "nets.csv", inventory));
      await page.locator("#inventory-search").fill(netName);
      assert(
        (
          await page
            .locator(".inventory-table tbody td:first-child")
            .allTextContents()
        ).includes(netName),
      );
      await page.locator("#inventory-search").fill("");
      await openTab(page, "routing", "tracks");
      pagination.tracks = await allPages(
        page,
        inventory.tracks.map((t) => t.id),
      );
      if (inventory.tracks.length) {
        const layer = inventory.tracks[0].layer;
        await page.locator("#inventory-layer").selectOption(layer);
        assert(
          (await page.locator("#inventory-count").textContent()).startsWith(
            `${inventory.tracks.filter((t) => t.layer === layer).length} /`,
          ),
        );
        await page.locator("#inventory-layer").selectOption("");
        await page.locator("[data-inventory-row]").first().click();
        assert(
          (await page.locator("#inventory-detail").textContent()).includes(
            "线宽",
          ),
        );
      }
      exports.push(await download(page, "tracks.csv", inventory));
      await page.locator('[data-inventory-kind="vias"]').click();
      pagination.vias = await allPages(
        page,
        inventory.vias.map((v) => v.id),
      );
      exports.push(await download(page, "vias.csv", inventory));
      const capabilities = await integration(page);
      await page.screenshot({
        path: path.join(output, `${viewport}-integration.png`),
        fullPage: true,
      });
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
        false,
      );
      const binding =
        viewport === "desktop" ? await checkStaleResponse(page, inventory) : {};
      await checkUnavailable(page);
      assert.deepEqual(errors, []);
      results.push({
        viewport,
        project: inventory.project,
        revision: inventory.revision,
        summary: inventory.summary,
        pagination,
        pixels,
        search: true,
        category_filter: true,
        package_filter: true,
        layer_filter: true,
        numeric_sort: true,
        component_selection: true,
        net_selection: true,
        all_pins: true,
        unknowns: true,
        exports,
        ...capabilities,
        ...binding,
        unavailable_retry: true,
        errors,
      });
      await page.close();
    }
  } finally {
    await browser.close();
    fs.writeFileSync(
      path.join(output, "inventory-results.json"),
      JSON.stringify(results, null, 2),
    );
  }
  // Existing QA remains independently executable; do not duplicate its checks.
  if (process.env.PCB_INVENTORY_SKIP_EXISTING_QA !== "1") {
    const existing = spawnSync(
      process.execPath,
      [
        path.join(__dirname, "check_platform.cjs"),
        url,
        path.join(output, "existing-platform"),
        "",
        String(minimum),
        "0",
      ],
      { stdio: "inherit", env: process.env },
    );
    assert.equal(existing.status, 0, "Existing workbench QA regressed");
  }
  console.log(JSON.stringify({ status: "passed", results }, null, 2));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

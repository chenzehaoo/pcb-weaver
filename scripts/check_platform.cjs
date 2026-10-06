const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

async function checkVersionBinding(page) {
  const original = await page.evaluate(() => ({
    project: pcbWorkbench.project,
    revision: pcbWorkbench.revision,
  }));
  const projects = await page
    .locator("[data-project]")
    .evaluateAll((nodes) => nodes.map((n) => n.dataset.project));
  const alternate = projects.find((p) => p !== original.project);
  if (!alternate)
    return { navigation_race: "not_tested", modal_binding: "not_tested" };
  let release, arrived;
  const gate = new Promise((resolve) => (release = resolve));
  const waiting = new Promise((resolve) => (arrived = resolve));
  const url = `**/api/projects/${encodeURIComponent(alternate)}/revisions`;
  await page.route(url, async (route) => {
    arrived();
    await gate;
    await route.continue();
  });
  try {
    await page.locator(`[data-project="${alternate}"]`).click();
    await waiting;
    await page.locator(`[data-project="${original.project}"]`).click();
    await page.waitForFunction(
      (p) => pcbWorkbench.project === p && pcbWorkbench.inspection,
      original.project,
    );
    release();
    await page.waitForTimeout(500);
    if (
      !(await page.evaluate(
        (o) =>
          pcbWorkbench.project === o.project &&
          pcbWorkbench.revision === o.revision,
        original,
      ))
    )
      throw new Error("A stale project response replaced the active revision");
  } finally {
    release();
    await page.unroute(url);
  }
  await page.locator("#run-plan").click();
  await page.evaluate((p) => loadProject(p), alternate);
  let captured;
  await page.route("**/api/jobs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    captured = route.request().postDataJSON();
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ error: "UI binding test; no job executed" }),
    });
  });
  try {
    await page.locator("#dialog-submit").click();
    await page.waitForFunction(
      () => !document.querySelector("#dialog-submit").disabled,
    );
    if (
      captured?.project !== original.project ||
      captured?.revision !== original.revision
    )
      throw new Error(
        "Confirmation targeted a different revision than the displayed one",
      );
  } finally {
    await page.unroute("**/api/jobs");
    await page.locator("#dialog-close").click();
  }
  await page.evaluate((o) => loadProject(o.project, o.revision), original);
  await page.evaluate(() => (document.querySelector("#toast").hidden = true));
  return { navigation_race: true, modal_binding: true };
}

(async () => {
  const url = process.argv[2] || "http://127.0.0.1:8765";
  const output = path.resolve(process.argv[3] || "docs/validation/platform");
  const project = process.argv[4];
  const minimumComponents = Number(process.argv[5] || 4);
  const minimumTracks = Number(process.argv[6] || 0);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: "chrome" });
  const results = [];
  try {
    for (const [name, width, height] of [
      ["desktop", 1600, 1000],
      ["mobile", 390, 844],
    ]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(url);
      await page.waitForFunction(() => window.pcbWorkbench?.inspection);
      if (project) {
        await page
          .locator("[data-project]")
          .filter({ hasText: project })
          .first()
          .click();
        await page.waitForFunction(
          (value) => pcbWorkbench.project === value && pcbWorkbench.inspection,
          project,
        );
      }
      await page.waitForTimeout(500);
      const deepLink = page.url();
      const expected = await page.evaluate(() => ({
        project: pcbWorkbench.project,
        revision: pcbWorkbench.revision,
      }));
      await page.reload();
      await page.waitForFunction(
        (value) =>
          pcbWorkbench.project === value.project &&
          pcbWorkbench.revision === value.revision &&
          pcbWorkbench.inspection,
        expected,
      );
      if (new URL(deepLink).searchParams.get("revision") !== expected.revision)
        throw new Error("Revision deep link was not updated");
      const binding = name === "desktop" ? await checkVersionBinding(page) : {};
      const initial = await page.evaluate(() => {
        const c = document.querySelector("canvas"),
          pixels = c
            .getContext("2d")
            .getImageData(0, 0, c.width, c.height).data,
          colors = new Set();
        for (let i = 0; i < pixels.length; i += 40)
          colors.add(`${pixels[i]},${pixels[i + 1]},${pixels[i + 2]}`);
        return {
          overflow: document.documentElement.scrollWidth > innerWidth,
          canvasWidth: c.width,
          canvasHeight: c.height,
          colors: colors.size,
          footprints: pcbWorkbench.inspection.board.footprints.length,
          tracks: pcbWorkbench.inspection.board.tracks,
          project: pcbWorkbench.project,
          revision: pcbWorkbench.revision,
        };
      });
      if (
        initial.overflow ||
        initial.colors < 15 ||
        initial.canvasWidth < 200 ||
        initial.footprints < minimumComponents ||
        initial.tracks < minimumTracks
      )
        throw new Error(JSON.stringify(initial));
      const beforeZoom = await page.evaluate(() => pcbWorkbench.scale);
      await page.locator("#zoom-in").click();
      if ((await page.evaluate(() => pcbWorkbench.scale)) <= beforeZoom)
        throw new Error("Zoom did not change");
      await page.locator("#fit").click();
      if (name === "desktop") {
        await page.locator('[data-object="nets"]').click();
        await page.locator("#objects [data-net]").first().click();
        if (!(await page.evaluate(() => pcbWorkbench.selectedNet)))
          throw new Error("Network selection failed");
        await page.locator('[data-object="components"]').click();
        await page.locator("#objects [data-reference]").first().click();
        if (!(await page.evaluate(() => pcbWorkbench.selected)))
          throw new Error("Component selection failed");
      }
      const layerCount = await page.evaluate(() => pcbWorkbench.layers.size);
      await page.locator("#layers input").first().uncheck();
      if (
        (await page.evaluate(() => pcbWorkbench.layers.size)) !==
        layerCount - 1
      )
        throw new Error("Layer control failed");
      await page.locator("#layers input").first().check();
      await page.locator('[data-tab="checks"]').click();
      await page.waitForTimeout(200);
      await page.screenshot({
        path: path.join(output, `${name}.png`),
        fullPage: true,
      });
      let findingLocator = "not_applicable";
      if (await page.locator("[data-finding]:not([disabled])").count()) {
        await page.locator("[data-finding]:not([disabled])").first().click();
        await page.waitForFunction(() => !!pcbWorkbench.finding);
        const located = await page.evaluate(() => {
          const s = pcbWorkbench,
            c = document.querySelector("canvas").getBoundingClientRect();
          const points = s.finding.items.filter((item) => item.pos);
          return (
            points.length > 0 &&
            points.every(({ pos }) => {
              const x = pos.x * s.scale + s.pan.x,
                y = pos.y * s.scale + s.pan.y;
              return x > 0 && x < c.width && y > 0 && y < c.height;
            })
          );
        });
        if (!located)
          throw new Error(
            "DRC objects were not framed inside the actual canvas",
          );
        await page.waitForTimeout(300);
        await page.screenshot({
          path: path.join(output, `${name}-finding.png`),
          fullPage: true,
        });
        findingLocator = true;
      }
      if (errors.length) throw new Error(errors.join("\n"));
      results.push({
        ...binding,
        deep_link: true,
        viewport: name,
        ...initial,
        zoom: true,
        layers: true,
        selection: name === "desktop",
        finding_locator: findingLocator,
        errors,
      });
      await page.close();
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(
    path.join(output, "browser-results.json"),
    JSON.stringify(results, null, 2),
  );
  console.log(JSON.stringify(results, null, 2));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

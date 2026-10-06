const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

(async () => {
  const file = path.resolve(process.argv[2]);
  const output = path.resolve(process.argv[3]);
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const checks = [];
  try {
    for (const [name, width, height] of [['desktop', 1440, 1000], ['mobile', 390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      await page.goto(pathToFileURL(file).href);
      const result = await page.evaluate(() => {
        const svg = document.querySelector('.board svg');
        const bounds = svg && svg.getBoundingClientRect();
        return {
          overflow: document.documentElement.scrollWidth > window.innerWidth,
          boardVisible: !!bounds && bounds.width > 200 && bounds.height > 100,
          pads: document.querySelectorAll('.board svg g[transform]').length || document.querySelectorAll('.board svg circle').length,
          title: document.title,
        };
      });
      if (result.overflow || !result.boardVisible || result.pads < 2) throw new Error(JSON.stringify(result));
      await page.screenshot({ path: path.join(output, `review-${name}.png`), fullPage: true });
      checks.push({ viewport: name, ...result });
      await page.close();
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(output, 'report-visual-check.json'), JSON.stringify(checks, null, 2));
  console.log(JSON.stringify(checks, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });

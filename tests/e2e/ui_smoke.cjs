/**
 * Browser smoke test of the whole stack against the SYNTHETIC fixture built by seed.py.
 *
 * It drives a real (headless) Chromium through the production build of the web
 * application and checks what a person would see: the map draws, vector tiles come from
 * the API, an event opens with both burned-area products, a satellite layer is served
 * by TiTiler, the statistics and quality pages show the fixture's figures, and an
 * analyst can sign in and review a product.
 *
 *   node tests/e2e/ui_smoke.cjs            (see tests/e2e/README.md)
 *
 * Environment: NARSAT_E2E_URL (default http://127.0.0.1:14173), NARSAT_E2E_DIR (where
 * seed.py wrote fixture.json; screenshots are saved next to it).
 */
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const BASE = process.env.NARSAT_E2E_URL ?? "http://127.0.0.1:14173";
const DIR = process.env.NARSAT_E2E_DIR ?? "/tmp/narsat-e2e";
const fixture = JSON.parse(fs.readFileSync(path.join(DIR, "fixture.json"), "utf8"));
const shots = path.join(DIR, "screenshots");
fs.mkdirSync(shots, { recursive: true });

const checks = [];
async function until(condition, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error(`condition not met within ${timeoutMs} ms`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}
function check(name, condition, detail = "") {
  checks.push({ name, ok: Boolean(condition), detail });
  console.log(`${condition ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

(async () => {
  const browser = await chromium.launch({
    // Software WebGL: the test machine has no GPU.
    args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  const responses = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("response", (response) => responses.push({ url: response.url(), status: response.status() }));
  const seen = (pattern, status = 200) => responses.filter((r) => pattern.test(r.url) && r.status === status);

  // ---------------------------------------------------------------- map
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".maplibregl-canvas", { timeout: 30000 });
  await page.waitForFunction(() => !document.querySelector(".map-status:not(.map-status--note)"), null, {
    timeout: 30000,
  });
  check("the map is created and reports ready", true);
  // Responses are recorded from the start, so tiles that arrived before this point count.
  await until(() => seen(/\/api\/v1\/tiles\/hotspots\//).length > 0, 30000);
  await page.waitForTimeout(2500);
  for (const layer of ["hotspots", "events", "burned_areas"]) {
    check(`vector tiles of '${layer}' are served by the API`, seen(new RegExp(`/api/v1/tiles/${layer}/`)).length > 0);
  }
  check(
    "the MapLibre worker is loaded from the application's own assets",
    seen(/\/assets\/maplibre-gl-worker-[^/]+\.mjs/).length > 0,
  );
  const mapPixels = await page.evaluate(() => {
    const canvas = document.querySelector(".maplibregl-canvas");
    return { width: canvas.width, height: canvas.height };
  });
  // 1440 x 900 viewport minus the top bar: a collapsed map (MapLibre's 300 px default) fails.
  check("the map fills the space under the top bar", mapPixels.width >= 1440 && mapPixels.height > 700, JSON.stringify(mapPixels));
  await page.screenshot({ path: path.join(shots, "01-map-overview.png") });

  // ---------------------------------------------------------------- statistics
  await page.getByRole("button", { name: "Statistics" }).click();
  await page.waitForSelector("text=Largest fires");
  await page.waitForSelector("text=Show on map");
  const stats = await page.locator(".page").innerText();
  check("the overview shows the number of detections of the fixture", /Fire detections\s*\n\s*17\b/.test(stats));
  check("burned area is reported for both methods", /Burned area, dNBR\s*\n\s*25\d(\.\d)? ha/.test(stats) && /Burned area, model/.test(stats));
  check("the regional table comes from the dbt mart", /Béjaïa/.test(stats) && /dNBR \(spectral index\)/.test(stats));
  check("the activity chart has one bar per day with detections", (await page.locator(".page svg.chart rect").count()) >= 6);
  await page.screenshot({ path: path.join(shots, "02-statistics.png"), fullPage: true });

  // ---------------------------------------------------------------- event panel
  await page.getByRole("button", { name: "Show on map" }).first().click();
  await page.waitForSelector(".panel .product", { timeout: 15000 });
  const panel = await page.locator(".panel").innerText();
  check("the largest fire opens on the map with its details", /Béjaïa, DZA/.test(panel) && /Detections\s*\n\s*8 \(4 high/.test(panel));
  check("both burned-area products are listed", (await page.locator(".panel .product").count()) === 2);
  check("the agreement between the two methods is shown", /dNBR product of this event/.test(panel));
  check("times are shown in UTC", /22 Jul 2023, 12:00 UTC/.test(panel));
  await page.waitForTimeout(1500);

  const overlay = page.locator(".panel select").first();
  const options = await overlay.locator("option").allInnerTexts();
  check("raster layers are offered for the event", options.length >= 7, options.join(" | "));
  await overlay.selectOption({ label: "Post-fire image - SWIR false colour" });
  await until(() => seen(/\/titiler\/cog\/tiles\//).length > 0, 30000);
  await page.waitForTimeout(2500);
  check("the satellite layer is rendered by TiTiler", seen(/\/titiler\/cog\/tiles\//).length > 0);
  await page.screenshot({ path: path.join(shots, "03-event-with-imagery.png") });

  // ---------------------------------------------------------------- review as analyst
  await page.getByRole("button", { name: "Analyst sign-in" }).click();
  await page.getByLabel("E-mail").fill(fixture.analyst.email);
  await page.getByLabel("Password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForSelector("text=Incorrect e-mail or password.");
  check("a wrong password is refused with a plain message", true);
  await page.getByLabel("Password").fill(fixture.analyst.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForSelector(`text=${fixture.analyst.email}`);
  await page.locator(".panel .product").first().getByRole("button", { name: "Accept" }).click();
  await page.waitForSelector(".panel .product .badge--accepted", { timeout: 15000 });
  check("an analyst can sign in and accept a product", true);
  await page.screenshot({ path: path.join(shots, "04-reviewed.png") });

  // ---------------------------------------------------------------- data quality
  await page.getByRole("button", { name: "Data quality" }).click();
  await page.waitForSelector("text=Data-quality checks");
  await page.waitForSelector(".page table caption");
  const quality = await page.locator(".page").innerText();
  check("the quality page lists the check suites of the last run", /silver\.hotspots/.test(quality) && /gold\.burned_areas/.test(quality));
  check("a warning is shown as a warning, not as a failure", /Warning/.test(quality) && !/Fail \(blocking\)/.test(quality));
  await page.screenshot({ path: path.join(shots, "05-data-quality.png"), fullPage: true });

  // ---------------------------------------------------------------- overall health
  const failed = responses.filter(
    (r) => r.status >= 400 && r.url.startsWith(BASE) && !(r.status === 401 && /\/auth\/token/.test(r.url)),
  );
  check("no request to the application failed", failed.length === 0, failed.slice(0, 5).map((r) => `${r.status} ${r.url}`).join("; "));
  // The base map is fetched from the internet; a blocked network is not an application error.
  const real = errors.filter((e) => !/openfreemap|ERR_|Failed to load resource|401/i.test(e));
  check("no JavaScript error was raised", real.length === 0, real.slice(0, 5).join("; "));

  await browser.close();
  const bad = checks.filter((c) => !c.ok);
  console.log(`\n${checks.length - bad.length}/${checks.length} checks passed; screenshots in ${shots}`);
  process.exit(bad.length ? 1 : 0);
})().catch((error) => {
  console.error(error);
  process.exit(2);
});

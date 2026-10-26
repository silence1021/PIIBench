// Official --init-page hook: record navigation provenance without changing the page.
const fs = require("node:fs");
const path = require("node:path");
const attached = new WeakSet();
const browsers = new WeakSet();

exports.default = async ({ page }) => {
  if (attached.has(page)) return;
  attached.add(page);
  const directory = process.env.PI_MCP_EVIDENCE;
  const record = value => fs.appendFileSync(path.join(directory, "navigation.jsonl"), JSON.stringify(value) + "\n");
  const browser = page.context().browser();
  if (browser && !browsers.has(browser)) {
    browsers.add(browser);
    const cdp = await browser.newBrowserCDPSession();
    try {
      const { processInfo } = await cdp.send("SystemInfo.getProcessInfo");
      const pid = processInfo.find(p => p.type === "browser").id;
      record({ type: "browser_started", browser_pid: pid });
      browser.on("disconnected", () => record({ type: "browser_closed", browser_pid: pid }));
    } finally {
      await cdp.detach();
    }
    fs.writeFileSync(path.join(directory, "browser-version.json"), JSON.stringify({ browser_version: browser.version() }));
  }
  page.on("response", async response => {
    const request = response.request();
    if (!request.isNavigationRequest() || request.resourceType() !== "document" ||
        response.frame() !== page.mainFrame()) return;
    try {
      const body = await response.body();
      record({ type: "document", url: response.url(), status: response.status(),
               bytes: body.length, at: new Date().toISOString() });
    } catch (error) {
      record({ type: "capture_error", url: response.url(), error: String(error) });
    }
  });
};

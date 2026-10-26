/** Record official Playwright MCP traffic without changing its tools or arguments. */
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const sdk = async relative => import(pathToFileURL(path.join(config.mcp_sdk, "dist/esm", relative)).href);
const { Client } = await sdk("client/index.js");
const { StreamableHTTPClientTransport } = await sdk("client/streamableHttp.js");
const { Server } = await sdk("server/index.js");
const { StdioServerTransport } = await sdk("server/stdio.js");
const { ListToolsRequestSchema, CallToolRequestSchema } = await sdk("types.js");
const record = event => fs.appendFileSync(config.events_path,
  JSON.stringify({ at: new Date().toISOString(), ...event }) + "\n");
const client = new Client({ name: "browser-observer", version: "1" });
await client.connect(new StreamableHTTPClientTransport(new URL(config.mcp_url)));
const instructions = client.getInstructions();
record({ type: "server_initialization", instructions });
const server = new Server({ name: "browser-transport", version: "1" }, {
  capabilities: { tools: {} }, instructions,
});
server.setRequestHandler(ListToolsRequestSchema, async request => {
  const result = await client.listTools(request.params);
  record({ type: "tool_catalog", ...result });
  return result;
});
let calls = 0;
server.setRequestHandler(CallToolRequestSchema, async request => {
  const callId = `browser-${++calls}`;
  const { name, arguments: input = {} } = request.params;
  record({ type: "tool_start", call_id: callId, name, input });
  try {
    const result = await client.callTool(request.params, undefined, { timeout: config.tool_timeout_seconds * 1000 });
    record({ type: "tool_end", call_id: callId, name, input, output: result, is_error: !!result.isError });
    return result;
  } catch (error) {
    record({ type: "transport_error", call_id: callId, name, input, error: String(error) });
    throw error;
  }
});
await server.connect(new StdioServerTransport());
async function close() { await client.close(); await server.close(); }
process.stdin.on("end", () => void close());
process.on("SIGTERM", () => void close().finally(() => process.exit(0)));

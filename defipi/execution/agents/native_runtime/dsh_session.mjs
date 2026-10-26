/** One native DSH Agent, using the same core creation path as its headless CLI. */
import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";

const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const module = async name => import(pathToFileURL(path.join(config.dsh_modules, "@deepseek-ai", name)).href);
const { loadLayeredEnv } = await module("dsh-app-boot/lib/index.js");
const { runProfile } = await module("dsh/lib/profile-boot-x7_BzdeW.js");
const { createUserMessage } = await module("dsh-llm/lib/index.js");
const { installModelSelection } = await module("dsh-agent/lib/index.js");
const { ctx, shutdown } = await runProfile({ environment: loadLayeredEnv("dsh"), profile: "headless",
  patchFiles: [config.patch_path], args: [] });
const record = event => fs.appendFileSync(config.events_path, JSON.stringify(event) + "\n");
// Observe the official fetch provider's decoded result without changing the
// native HTML-to-text conversion, trust notice, tool result or system prompt.
const nativeFetch = ctx.web.fetch.bind(ctx.web);
ctx.web.fetch = async (request, signal) => {
  const result = await nativeFetch(request, signal);
  record({ type: "native_web_document", source: "dsh_native_web_fetch", requested_url: request.url,
    url: result.url, status: result.statusCode, body_kind: result.body.kind, truncated: result.truncated });
  return result;
};
let handle;
let exitCode = 1;
try {
  const names = ctx.tools.schemas().map(tool => tool.name);
  if (!names.includes("mcp__playwright__browser_navigate")) throw new Error("Playwright MCP failed to register");
  record({ type: "runtime", tools: names, model: config.selection.model, provider: config.selection.provider,
    cwd: process.cwd(), access_policy: "native-tools-post-run-audit-v1" });
  // Observe every native dispatch, including nested code calls and child agents.
  // Tool schemas, results, path decisions and native permission rules are untouched.
  ctx.on("tools/result", (execution, result) => {
    record({ type: "native_tool_result", call_id: execution.callId, root_call_id: execution.rootCallId,
      name: execution.name, input: execution.arguments, output: result,
      session_id: execution.agent?.session.header.id });
  });
  ctx.on("fs/observed", (target, observation, execution) => {
    if (!execution?.agent) return;
    record({ type: "native_file_observation", target, observation,
      name: execution.name, call_id: execution.callId, session_id: execution.agent.session.header.id });
  });
  ctx.tools.guard(execution => {
    // This hook only records dispatches; the tool-count threshold never denies one.
    record({ type: "native_tool_attempt", call_id: execution.callId, root_call_id: execution.rootCallId,
      name: execution.name, input: execution.arguments, session_id: execution.agent?.session.header.id });
    return undefined;
  });
  handle = await ctx.agents.create({ sessionId: `session-${randomUUID()}`, meta: { cwd: process.cwd() },
    agentOptions: config.selection,
    setup: agentCtx => {
      installModelSelection(agentCtx, { current: config.selection, assembled: undefined });
    } });
  const { agent } = handle;
  const dispose = ctx.on("session/event", (session, event) => {
    record({ ...event, session_id: session.header.id, root_session: session === agent.session });
  });
  await agent.whenIdle();
  agent.followup(createUserMessage({ content: [{ type: "text", text: config.prompt }], source: { kind: "user" } }));
  await agent.whenIdle();
  await ctx.sessions.flush(agent.session);
  dispose();
  const events = Array.from({ length: agent.session.seq }, (_, seq) => agent.session.eventAt(seq));
  const last = events.findLast(event => event.type === "assistant/message");
  const end = events.findLast(event => event.type === "turn/end");
  const answer = (last?.data.message.content ?? []).filter(block => block.type === "text").map(block => block.text).join("");
  const pending = (last?.data.message.content ?? []).some(block => block.type === "tool-call");
  const result = { answer, end: end?.data.reason, pending_tool_call: pending,
    session_id: agent.session.header.id, request_header: agent.session.requestHeader() };
  fs.writeFileSync(config.result_path, JSON.stringify(result, null, 2));
  exitCode = end?.data.reason?.kind === "completed" && !pending ? 0 : 1;
} catch (error) {
  record({ type: "driver_error", error: String(error) });
  console.error(error);
} finally {
  await handle?.dispose();
  await shutdown.shutdown(exitCode);
}

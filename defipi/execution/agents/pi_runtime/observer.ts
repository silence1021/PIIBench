import fs from "node:fs";
import path from "node:path";
import { createTransportDiagnostics } from "./transport_diagnostics.mjs";

export default function (pi: any) {
  const config = JSON.parse(fs.readFileSync(process.env.PI_SESSION_CONFIG!, "utf8"));
  const out = config.evidence_dir;
  fs.mkdirSync(path.join(out, "requests"), { recursive: true });
  const record = (value: any) => fs.appendFileSync(path.join(out, "provider-events.jsonl"), JSON.stringify(value) + "\n");
  const transport = createTransportDiagnostics(record);
  pi.on("message_end", (event: any) => {
    if (event.message?.role === "assistant") transport.endRequest();
  });
  pi.on("session_shutdown", () => transport.close());
  fs.writeFileSync(path.join(out, "observer-loaded.json"), JSON.stringify({ loaded: true,
    transport_diagnostics: "undici-request-error-v1", node_version: process.version,
    builtin_undici_version: process.versions.undici }));
  let count = 0;
  let toolCount = 0;
  let lastId = "";
  function stop(reason: string, details: any = {}) {
    record({ type: "budget_stop", reason, model_requests: count, tool_calls: toolCount, ...details });
    // Pi catches hook exceptions. Exit before the prohibited provider request.
    process.exit(75);
  }
  pi.on("tool_execution_start", (event: any) => {
    // This awaited event precedes validation/execution, so failed calls and
    // multiple calls from one assistant message are each counted for post-run statistics.
    try {
      toolCount++;
      record({ type: "tool_call", tool_calls: toolCount, tool_call_id: event.toolCallId,
               tool_name: event.toolName, at: new Date().toISOString() });
    } catch (error) {
      process.stderr.write("Pi tool count recording failed: " + String(error) + "\n");
      process.exit(76);
    }
  });
  pi.on("before_provider_request", async (event: any) => {
    try {
    if (config.shared_request_budget) {
      const budget = config.shared_request_budget;
      const response = await fetch(budget.claim_url, { method: "POST" });
      if (response.status === 429) stop("shared_request_limit");
      if (!response.ok) throw new Error("Request counter unavailable: " + response.status);
    }
    count++;
    const payload = { ...event.payload };
    for (const parameter of config.omit_parameters) delete payload[parameter];
    if (config.provider === "openrouter") {
      if (payload.model !== config.model) stop("unexpected_openrouter_model");
      Object.assign(payload, config.request_parameters);
    } else if (config.provider_api === "openai-responses") {
      payload.store = false;
      payload.reasoning = { ...payload.reasoning, effort: config.reasoning_effort };
      payload.include = [...new Set([...(payload.include || []), "reasoning.encrypted_content"])];
      if (config.max_response_tokens != null) payload.max_output_tokens = config.max_response_tokens;
    } else if (config.max_response_tokens != null) payload.max_tokens = config.max_response_tokens;
    fs.writeFileSync(path.join(out, "requests", String(count).padStart(3, "0") + ".json"), JSON.stringify(payload, null, 2) + "\n");
    record({ type: "request", request: count, at: new Date().toISOString() });
    transport.beginRequest({ request: count, provider: config.provider, model: payload.model });
    return payload;
    } catch (error) {
      process.stderr.write("Pi request recording or budget allocation failed: " + String(error) + "\n");
      process.exit(76);
    }
  });
  pi.on("after_provider_response", (event: any) => {
    record({ type: "response", request: count, status: event.status, at: new Date().toISOString() });
  });
  pi.on("provider_stream_event", (event: any) => {
    const data = event.data?.response ?? event.data;
    if (data?.openrouter_metadata) {
      record({ type: "provider_routing", request: count, id: data.id,
               reported_provider: data.provider, metadata: data.openrouter_metadata });
    }
    if (data && (data.id !== lastId || data.usage)) {
      record({ type: "usage", request: count, id: data.id, model: data.model,
               provider: data.provider, usage: data.usage });
      lastId = data.id;
    }
  });
  pi.on("tool_call", (event: any) => {
    if (config.tool_profile !== "html-read-only") return;
    try {
      if (event.toolName === "read" &&
          fs.realpathSync(path.resolve(config.work_dir, event.input.path)) === fs.realpathSync(config.input_file)) return;
    } catch { /* A missing or unresolvable path is outside the supplied input. */ }
    const reason = "Read only the supplied input.html file.";
    record({ type: "tool_policy_block", tool_call_id: event.toolCallId, tool_name: event.toolName, reason });
    return { block: true, reason };
  });
  // Record the active registry after applying the selected CLI tool profile.
  pi.on("before_agent_start", () => {
    fs.writeFileSync(path.join(out, "native-tools.json"), JSON.stringify({
      active_tools: pi.getActiveTools(), tools: pi.getAllTools(),
    }, null, 2) + "\n");
  });
}

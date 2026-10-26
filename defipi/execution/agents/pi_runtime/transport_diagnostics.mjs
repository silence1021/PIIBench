import { channel } from "node:diagnostics_channel";

// Error properties are non-enumerable; JSON.stringify(error) alone loses them.
function errorDetails(error, seen = new Set()) {
  if (error === null || typeof error !== "object") return { message: String(error) };
  if (seen.has(error)) return { circular: true };
  seen.add(error);
  const result = {};
  for (const key of ["name", "message", "stack", "code", "errno", "syscall", "address", "port", "hostname"]) {
    if (["string", "number", "boolean"].includes(typeof error[key])) result[key] = error[key];
  }
  if (error.cause !== undefined) result.cause = errorDetails(error.cause, seen);
  if (Array.isArray(error.errors)) result.errors = error.errors.map(item => errorDetails(item, seen));
  if (error.socket) {
    result.socket = {};
    for (const key of ["localAddress", "localPort", "remoteAddress", "remotePort", "remoteFamily", "bytesWritten", "bytesRead"]) {
      if (["string", "number"].includes(typeof error.socket[key])) result.socket[key] = error.socket[key];
    }
  }
  seen.delete(error);
  return result;
}

function responseHeaders(headers) {
  const allowed = new Set(["content-type", "content-encoding", "server", "x-request-id", "x-ds-request-id", "cf-ray"]);
  const pairs = Array.isArray(headers)
    ? Array.from({ length: headers.length / 2 }, (_, i) => [headers[2 * i], headers[2 * i + 1]])
    : Object.entries(headers ?? {});
  return Object.fromEntries(pairs.map(([key, value]) => [String(key).toLowerCase(), String(value)])
    .filter(([key]) => allowed.has(key)));
}

// Observe the original transport error before Pi reduces it to errorMessage.
// Do not replace fetch, consume response bodies, or change retry/timeout behavior.
export function createTransportDiagnostics(record) {
  let active = null;
  let sequence = 0;
  const requests = new WeakMap();
  const subscriptions = [];
  function subscribe(name, handler) {
    const topic = channel(name);
    const callback = message => {
      try { handler(message); }
      catch (error) {
        // Diagnostics-channel callbacks must not throw into the HTTP client.
        process.stderr.write("Pi transport diagnostics failed: " + String(error) + "\n");
      }
    };
    topic.subscribe(callback);
    subscriptions.push(() => topic.unsubscribe(callback));
  }
  subscribe("undici:request:create", ({ request }) => {
    if (!active) return;
    const url = new URL(request.path, request.origin);
    requests.set(request, { ...active, transport_id: ++sequence,
      started_at: new Date().toISOString(), method: request.method,
      endpoint: url.origin + url.pathname, http_status: null });
  });
  subscribe("undici:request:headers", ({ request, response }) => {
    const state = requests.get(request);
    if (state) Object.assign(state, { http_status: response.statusCode,
      headers_at: new Date().toISOString(), response_headers: responseHeaders(response.headers) });
  });
  subscribe("undici:request:bodyChunkReceived", ({ request, chunk }) => {
    const state = requests.get(request);
    if (state) Object.assign(state, { last_body_chunk_at: new Date().toISOString(),
      body_bytes_received: (state.body_bytes_received ?? 0) + chunk.byteLength });
  });
  subscribe("undici:request:error", ({ request, error }) => {
    const state = requests.get(request);
    if (!state) return;
    record({ type: "transport_error", source: "undici:request:error", ...state,
      at: new Date().toISOString(), elapsed_ms: Date.now() - Date.parse(state.started_at),
      phase: state.http_status === null ? "before_response_headers" : "response_body",
      error: errorDetails(error) });
  });
  return {
    beginRequest(context) { active = { ...context }; },
    endRequest() { active = null; },
    close() { active = null; for (const unsubscribe of subscriptions) unsubscribe(); },
  };
}

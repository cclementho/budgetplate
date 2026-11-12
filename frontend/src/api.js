// Thin API client. The frontend never sees the Anthropic API key — every
// AI-powered call goes through the FastAPI backend.

const BASE = import.meta.env.VITE_API_BASE || "";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
  return res.json();
}

// POST /api/budget-plan/stream and read its server-sent events.
// handlers: { onStart(info), onItem(basketLine), onMeal(meal), onRetry() }. Resolves with
// the validated final response (the same shape as /api/budget-plan), which
// replaces whatever the item and meal events showed.
async function streamBudgetPlan(profile, handlers = {}, signal) {
  const res = await fetch(`${BASE}/api/budget-plan/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
    signal,
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body; keep the generic message
    }
    throw new Error(detail);
  }
  // Problems found before the model is called (e.g. no deals) come as JSON.
  const type = res.headers.get("content-type") || "";
  if (!type.includes("text/event-stream") || !res.body) return res.json();

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final = null;

  const handle = (block) => {
    let event = "message";
    let data = "";
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    });
    if (!data) return;
    const payload = JSON.parse(data);
    if (event === "start") handlers.onStart?.(payload);
    else if (event === "item") handlers.onItem?.(payload);
    else if (event === "meal") handlers.onMeal?.(payload);
    else if (event === "retry") handlers.onRetry?.(payload);
    else if (event === "done") final = payload;
    else if (event === "error") throw new Error(payload.detail);
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      handle(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
    }
  }
  if (!final) {
    throw new Error("The meal plan stopped before it was finished. Please try again.");
  }
  return final;
}

export const api = {
  health: () => request("/api/health"),

  searchItems: (postalCode, query) =>
    request(
      `/api/items?postal_code=${encodeURIComponent(
        postalCode
      )}&query=${encodeURIComponent(query)}`
    ),

  scrapeStatus: (postalCode) =>
    request(
      `/api/scrape/status?postal_code=${encodeURIComponent(postalCode)}`
    ),

  getStores: (postalCode) =>
    request(`/api/stores?postal_code=${encodeURIComponent(postalCode)}`),

  getDeals: (postalCode, merchant) =>
    request(
      `/api/deals?postal_code=${encodeURIComponent(
        postalCode
      )}&merchant=${encodeURIComponent(merchant)}`
    ),

  budgetPlanStream: streamBudgetPlan,

  budgetPlan: (profile) =>
    request("/api/budget-plan", {
      method: "POST",
      body: JSON.stringify(profile),
    }),
};

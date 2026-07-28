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

  budgetPlan: (profile) =>
    request("/api/budget-plan", {
      method: "POST",
      body: JSON.stringify(profile),
    }),
};

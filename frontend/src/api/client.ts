// Typed API client. Talks ONLY to our backend (/api). Never to Dhan directly.
// - injects the CSRF header on state-changing calls
// - sends session cookies (same-origin)

const CSRF_KEY = "dhan_csrf";

export function setCsrfToken(token: string | null) {
  if (token) sessionStorage.setItem(CSRF_KEY, token);
  else sessionStorage.removeItem(CSRF_KEY);
}

export function getCsrfToken(): string | null {
  return sessionStorage.getItem(CSRF_KEY);
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : "Request failed");
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (method !== "GET") {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  const text = await res.text();
  const data = (text ? safeJson(text) : null) as Record<string, unknown> | null;

  if (!res.ok) {
    const detail = (data && (data.detail ?? data)) ?? text;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
};

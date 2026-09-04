import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, apiRequest, ApiError, getToken, setToken } from "./api";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function emptyResponse(status: number): Response {
  return new Response(null, { status });
}

describe("token storage", () => {
  afterEach(() => {
    localStorage.clear();
  });

  it("round-trips a token through localStorage", () => {
    expect(getToken()).toBeNull();
    setToken("abc123");
    expect(getToken()).toBe("abc123");
  });

  it("clears the token when set to null", () => {
    setToken("abc123");
    setToken(null);
    expect(getToken()).toBeNull();
  });
});

describe("apiRequest", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("returns parsed JSON on success", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, { hello: "world" }));

    const result = await apiRequest<{ hello: string }>("/v1/thing");
    expect(result).toEqual({ hello: "world" });
  });

  it("returns undefined for a 204 No Content response without parsing a body", async () => {
    vi.mocked(fetch).mockResolvedValue(emptyResponse(204));

    const result = await apiRequest("/v1/thing");
    expect(result).toBeUndefined();
  });

  it("attaches the bearer token when one is stored and auth isn't disabled", async () => {
    setToken("my-token");
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, {}));

    await apiRequest("/v1/thing");

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const headers = init?.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer my-token");
  });

  it("omits the Authorization header when auth: false is passed", async () => {
    setToken("my-token");
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, {}));

    await apiRequest("/v1/auth/login", { auth: false });

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const headers = init?.headers as Record<string, string>;
    expect(headers["Authorization"]).toBeUndefined();
  });

  it("throws ApiError with the string detail from a JSON error body", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(404, { detail: "Not found." }));

    await expect(apiRequest("/v1/thing")).rejects.toMatchObject(
      new ApiError(404, "Not found.")
    );
  });

  it("joins a FastAPI-style validation error array into one message", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(422, { detail: [{ msg: "field required" }, { msg: "too long" }] })
    );

    await expect(apiRequest("/v1/thing")).rejects.toMatchObject(
      new ApiError(422, "field required; too long")
    );
  });

  it("falls back to a generic message when the error body has no detail", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(500, {}));

    await expect(apiRequest("/v1/thing")).rejects.toMatchObject(
      new ApiError(500, "Request failed (500)")
    );
  });

  it("falls back to a generic message for a non-JSON error response", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("oops", { status: 502 }));

    await expect(apiRequest("/v1/thing")).rejects.toMatchObject(
      new ApiError(502, "Request failed (502)")
    );
  });
});

describe("api helpers", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("api.post sends a JSON body with a Content-Type header", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(201, { id: "1" }));

    await api.post("/v1/things", { name: "New thing" });

    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe("/v1/things");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ name: "New thing" }));
    const headers = init?.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("api.delete sends no body", async () => {
    vi.mocked(fetch).mockResolvedValue(emptyResponse(204));

    await api.delete("/v1/things/1");

    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect(init?.method).toBe("DELETE");
    expect(init?.body).toBeUndefined();
  });
});

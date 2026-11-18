import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, absoluteUrl, api, buildQuery, request, vectorTileUrl } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(response: Response) {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(() =>
    Promise.resolve(response),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("buildQuery", () => {
  it("skips empty values and encodes the rest", () => {
    expect(
      buildQuery({ start: "2023-07-01T00:00:00Z", end: null, country: undefined, q: "", limit: 25, flag: false }),
    ).toBe("?start=2023-07-01T00%3A00%3A00Z&limit=25&flag=false");
  });

  it("returns an empty string when nothing is set", () => {
    expect(buildQuery({ start: null })).toBe("");
  });
});

describe("tile URLs", () => {
  it("keeps the XYZ placeholders intact and adds the origin", () => {
    expect(absoluteUrl("/titiler/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=%2Fa.tif", "http://localhost:8080")).toBe(
      "http://localhost:8080/titiler/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=%2Fa.tif",
    );
  });

  it("leaves absolute URLs alone", () => {
    const url = "https://tiles.example.org/{z}/{x}/{y}.png";
    expect(absoluteUrl(url, "http://localhost:8080")).toBe(url);
  });

  it("builds the vector tile template with the period and method", () => {
    const url = vectorTileUrl("burned_areas", { start: "2023-07-01T00:00:00Z", end: null }, "model");
    expect(url.endsWith("/api/v1/tiles/burned_areas/{z}/{x}/{y}.mvt?start=2023-07-01T00%3A00%3A00Z&method=model")).toBe(
      true,
    );
    expect(vectorTileUrl("hotspots", { start: null, end: null })).toMatch(/\/tiles\/hotspots\/\{z\}\/\{x\}\/\{y\}\.mvt$/);
  });
});

describe("request", () => {
  it("sends the bearer token and JSON body", async () => {
    const fetchMock = stubFetch(new Response(JSON.stringify({ ba_id: "b1", qa_status: "accepted" }), { status: 200 }));
    await api.review("secret-token", "b1", "accepted", "looks right");
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/burned-areas/b1/qa");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer secret-token");
    expect(JSON.parse(init?.body as string)).toEqual({ status: "accepted", note: "looks right" });
  });

  it("logs in with a form body, as the OAuth2 password flow requires", async () => {
    const fetchMock = stubFetch(
      new Response(JSON.stringify({ access_token: "t", token_type: "bearer", expires_in: 3600, role: "analyst" })),
    );
    const token = await api.login("analyst@example.org", "p@ss word");
    expect(token.role).toBe("analyst");
    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.body).toBeInstanceOf(URLSearchParams);
    expect(String(init?.body)).toBe("username=analyst%40example.org&password=p%40ss+word");
  });

  it("turns an error response into an ApiError carrying the server's message", async () => {
    stubFetch(new Response(JSON.stringify({ detail: "event not found" }), { status: 404 }));
    const failure = await request("/events/unknown").catch((reason: unknown) => reason);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(404);
    expect((failure as ApiError).message).toBe("event not found");
  });

  it("copes with an error body that is not JSON", async () => {
    stubFetch(new Response("<html>Bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    const failure = (await request("/stats/summary").catch((reason: unknown) => reason)) as ApiError;
    expect(failure.status).toBe(502);
    expect(failure.message).toBe("Bad Gateway");
  });
});

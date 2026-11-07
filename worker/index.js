// Cloudflare Worker (free plan): serves the site (static assets) and exposes the MCP server at /mcp.
// It checks a token, then forwards to the backend running on a Render web service.
//   vars:    BACKEND_URL              e.g. https://<your-service>.onrender.com   (no trailing slash)
//   secrets: CUTWARDEN_TOKEN          what MCP clients must send (Bearer or ?key=)
//            CUTWARDEN_GATEWAY_SECRET shared with the Space; the backend rejects calls without it

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
  "access-control-allow-headers": "content-type, accept, authorization, mcp-session-id, mcp-protocol-version, last-event-id",
  "access-control-expose-headers": "mcp-session-id",
};
const PASS = ["content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"];

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 });
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (!env.CUTWARDEN_TOKEN || !env.CUTWARDEN_GATEWAY_SECRET || !env.BACKEND_URL) {
      return new Response("gateway not configured", { status: 503, headers: CORS });
    }

    const bearer = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
    const given = bearer || url.searchParams.get("key") || "";
    if (!safeEqual(given, env.CUTWARDEN_TOKEN)) {
      return new Response("unauthorized", { status: 401, headers: { ...CORS, "www-authenticate": "Bearer" } });
    }

    const headers = new Headers();
    for (const h of PASS) { const v = request.headers.get(h); if (v) headers.set(h, v); }
    headers.set("x-cutwarden-gateway", env.CUTWARDEN_GATEWAY_SECRET);

    let upstream;
    try {
      upstream = await fetch(env.BACKEND_URL.replace(/\/$/, "") + "/mcp", {
        method: request.method,
        headers,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "manual",
      });
    } catch (e) {
      return new Response("backend unreachable, retry shortly", { status: 502, headers: CORS });
    }

    // A sleeping Render service answers with an HTML "starting up" page instead of MCP.
    if ((upstream.headers.get("content-type") || "").includes("text/html")) {
      return new Response("backend is waking up, retry in about a minute", { status: 503, headers: { ...CORS, "retry-after": "60" } });
    }

    const out = new Response(upstream.body, upstream);
    for (const [k, v] of Object.entries(CORS)) out.headers.set(k, v);
    return out;
  },

  // Free cron trigger: keeps the free Render service from sleeping (it sleeps after 15 idle minutes) from inactivity.
  async scheduled(_event, env) {
    if (env.BACKEND_URL) await fetch(env.BACKEND_URL.replace(/\/$/, "") + "/health").catch(() => {});
  },
};

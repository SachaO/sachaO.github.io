import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

interface Env {
  MCP_ACCESS_TOKEN: string;
  KRAKEN_SPOT_API_KEY?: string;
  KRAKEN_SPOT_API_SECRET?: string;
  KRAKEN_FUTURES_API_KEY?: string;
  KRAKEN_FUTURES_API_SECRET?: string;
}

const enc = new TextEncoder();
const b64 = (s: string) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
async function digest(data: string | Uint8Array): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", typeof data === "string" ? enc.encode(data) : data);
}
async function hmac(secret: Uint8Array, data: Uint8Array, algorithm: "SHA-512" | "SHA-256" = "SHA-512"): Promise<ArrayBuffer> {
  const key = await crypto.subtle.importKey("raw", secret, { name: "HMAC", hash: algorithm }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", key, data);
}
function json(data: unknown) { return JSON.stringify(data, null, 2); }

async function spotPrivate(env: Env, path: string, params: Record<string,string> = {}) {
  if (!env.KRAKEN_SPOT_API_KEY || !env.KRAKEN_SPOT_API_SECRET) throw new Error("Clés API Spot absentes dans les secrets Cloudflare.");
  const nonce = String(Date.now() * 1000);
  const body = new URLSearchParams({ nonce, ...params }).toString();
  const sha = new Uint8Array(await digest(nonce + body));
  const pathBytes = enc.encode(path);
  const message = new Uint8Array(pathBytes.length + sha.length);
  message.set(pathBytes); message.set(sha, pathBytes.length);
  const sig = btoa(String.fromCharCode(...new Uint8Array(await hmac(b64(env.KRAKEN_SPOT_API_SECRET), message))));
  const res = await fetch("https://api.kraken.com" + path, {
    method: "POST",
    headers: { "API-Key": env.KRAKEN_SPOT_API_KEY, "API-Sign": sig, "Content-Type": "application/x-www-form-urlencoded" },
    body
  });
  if (!res.ok) throw new Error("Kraken Spot HTTP " + res.status);
  const out: any = await res.json();
  if (out.error?.length) throw new Error(out.error.join("; "));
  return out.result;
}

async function futuresPrivate(env: Env, endpoint: string) {
  if (!env.KRAKEN_FUTURES_API_KEY || !env.KRAKEN_FUTURES_API_SECRET) throw new Error("Clés API Futures absentes dans les secrets Cloudflare.");
  const nonce = String(Date.now() * 1000);
  const path = "/api/v3/" + endpoint;
  const postData = "";
  const sha = new Uint8Array(await digest(postData + nonce));
  const pathBytes = enc.encode(path);
  const message = new Uint8Array(pathBytes.length + sha.length);
  message.set(pathBytes); message.set(sha, pathBytes.length);
  const authent = btoa(String.fromCharCode(...new Uint8Array(await hmac(b64(env.KRAKEN_FUTURES_API_SECRET), message))));
  const res = await fetch("https://futures.kraken.com" + path, {
    method: "GET",
    headers: { "APIKey": env.KRAKEN_FUTURES_API_KEY, "Authent": authent, "Nonce": nonce }
  });
  if (!res.ok) throw new Error("Kraken Futures HTTP " + res.status);
  const out: any = await res.json();
  if (out.result === "error" || out.errors) throw new Error(JSON.stringify(out));
  return out;
}

function createServer(env: Env) {
  const server = new McpServer({ name: "Kraken MCP", version: "1.0.0" });

  server.registerTool("kraken_etat", { description: "État de la passerelle et présence des clés, sans révéler les secrets.", inputSchema: z.object({}) }, async () => ({
    content: [{ type: "text", text: json({
      service: "Kraken MCP Cloudflare",
      spotCredentialsConfigured: !!(env.KRAKEN_SPOT_API_KEY && env.KRAKEN_SPOT_API_SECRET),
      futuresCredentialsConfigured: !!(env.KRAKEN_FUTURES_API_KEY && env.KRAKEN_FUTURES_API_SECRET),
      trading: "DÉSACTIVÉ - version lecture seule"
    }) }]
  }));

  server.registerTool("kraken_spot_ticker", { description: "Ticker public Spot Kraken. Exemple : XBTUSD.", inputSchema: z.object({ pair: z.string() }) }, async ({ pair }) => {
    const u = new URL("https://api.kraken.com/0/public/Ticker"); u.searchParams.set("pair", pair);
    const r: any = await fetch(u).then(x => x.json());
    if (r.error?.length) throw new Error(r.error.join("; "));
    return { content: [{ type: "text", text: json(r.result) }] };
  });

  server.registerTool("kraken_spot_paires", { description: "Liste les paires Spot Kraken.", inputSchema: z.object({ activesUniquement: z.boolean().optional() }) }, async ({ activesUniquement = true }) => {
    const r: any = await fetch("https://api.kraken.com/0/public/AssetPairs").then(x => x.json());
    if (r.error?.length) throw new Error(r.error.join("; "));
    const data = activesUniquement ? Object.fromEntries(Object.entries(r.result).filter(([,v]: any) => v.status === "online")) : r.result;
    return { content: [{ type: "text", text: json(data) }] };
  });

  server.registerTool("kraken_spot_solde", { description: "Lecture des soldes Spot. Permission API de lecture des fonds nécessaire.", inputSchema: {} }, async () => ({
    content: [{ type: "text", text: json(await spotPrivate(env, "/0/private/Balance")) }]
  }));

  server.registerTool("kraken_spot_ordres_ouverts", { description: "Lecture des ordres Spot ouverts.", inputSchema: {} }, async () => ({
    content: [{ type: "text", text: json(await spotPrivate(env, "/0/private/OpenOrders")) }]
  }));

  server.registerTool("kraken_futures_instruments", { description: "Liste les instruments publics Kraken Futures.", inputSchema: {} }, async () => {
    const r = await fetch("https://futures.kraken.com/derivatives/api/v3/instruments").then(x => x.json());
    return { content: [{ type: "text", text: json(r) }] };
  });

  server.registerTool("kraken_futures_comptes", { description: "Lecture des comptes Futures. Clé Futures en lecture nécessaire.", inputSchema: {} }, async () => ({
    content: [{ type: "text", text: json(await futuresPrivate(env, "accounts")) }]
  }));

  server.registerTool("kraken_futures_positions", { description: "Lecture des positions ouvertes Futures.", inputSchema: {} }, async () => ({
    content: [{ type: "text", text: json(await futuresPrivate(env, "openpositions")) }]
  }));

  return server;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return Response.json({ status: "ok", service: "kraken-mcp", trading: false });
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 });
    const expected = env.MCP_ACCESS_TOKEN || "";
    const supplied = request.headers.get("authorization") || "";
    if (!expected || supplied !== "Bearer " + expected) return Response.json({ error: "Unauthorized" }, { status: 401 });
    return createMcpHandler(() => createServer(env))(request, env, ctx);
  }
};

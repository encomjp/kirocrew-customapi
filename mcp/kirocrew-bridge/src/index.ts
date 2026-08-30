#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";
import { spawn } from "child_process";

function getGatewayInfo() {
  const home = process.env.KIROCREW_HOME || join(homedir(), ".kiro", "crew");
  const configPath = join(home, "config.json");
  let port = 5476;
  try {
    const cfg = JSON.parse(readFileSync(configPath, "utf8"));
    // dashboard.port is not a config key, try to find gateway port from run markers
    // fallback to 5476
  } catch {}
  // Try to read port from run marker or config
  try {
    const lockPath = join(home, "gateway.lock");
    // lock file contains pid, not port - use default
  } catch {}
  const secretPaths = [
    join(home, ".local_secret"),
    join(home, "gateway-5476.secret"),
  ];
  let secret = "";
  for (const p of secretPaths) {
    if (existsSync(p)) {
      try { secret = readFileSync(p, "utf8").trim(); if (secret) break; } catch {}
    }
  }
  // Also try run/gateway-*.secret
  try {
    const { readdirSync } = require("fs");
    const runDir = join(home, "run");
    if (existsSync(runDir)) {
      for (const f of readdirSync(runDir)) {
        if (f.startsWith("gateway-") && f.endsWith(".secret")) {
          const s = readFileSync(join(runDir, f), "utf8").trim();
          if (s) { secret = s; const m = f.match(/gateway-(\d+)\.secret/); if (m) port = parseInt(m[1]); break; }
        }
      }
    }
  } catch {}
  return { home, port, secret };
}

async function callGateway(tool: string, args: any) {
  const { port, secret } = getGatewayInfo();
  const url = `http://127.0.0.1:${port}/api/${tool.replace(/^mcp__/, "").replace(/^memory_tencentdb_/, "memory/")}`;
  // Fallback generic proxy: try POST to /api/tools/call
  const endpoints = [
    `http://127.0.0.1:${port}/api/tools/call`,
    `http://127.0.0.1:${port}/api/mcp/call`,
    url,
  ];
  for (const ep of endpoints) {
    try {
      const res = await fetch(ep, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Internal-Secret": secret },
        body: JSON.stringify({ tool, args }),
      });
      if (res.ok) return await res.json();
    } catch {}
  }
  // Fallback: direct ssh via spawn if tool is ssh
  if (tool.includes("ssh") && args.cmdString) {
    return new Promise((resolve, reject) => {
      const p = spawn("bash", ["-c", args.cmdString], { env: process.env });
      let out = "", err = "";
      p.stdout.on("data", d => out += d);
      p.stderr.on("data", d => err += d);
      p.on("close", code => resolve({ stdout: out, stderr: err, code }));
      p.on("error", reject);
    });
  }
  throw new Error(`Gateway call failed for ${tool} - is KiroCrew gateway running on port ${port}?`);
}

const server = new Server({ name: "kirocrew-bridge", version: "1.0.0" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    { name: "mcp__ssh__execute_command", description: "Execute command on SSH host via KiroCrew gateway (proxied)", inputSchema: { type: "object", properties: { cmdString: { type: "string" }, connectionName: { type: "string" } }, required: ["cmdString"] } },
    { name: "memory_tencentdb_memory_search", description: "Search KiroCrew memory (proxied)", inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } },
    { name: "memory_tencentdb_conversation_search", description: "Search KiroCrew conversations (proxied)", inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } },
    { name: "kirocrew_call", description: "Generic KiroCrew tool proxy (tool, args)", inputSchema: { type: "object", properties: { tool: { type: "string" }, args: { type: "object" } }, required: ["tool"] } },
  ]
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  try {
    let result;
    if (name === "kirocrew_call") {
      result = await callGateway((args as any).tool, (args as any).args || {});
    } else {
      result = await callGateway(name, args || {});
    }
    return { content: [{ type: "text", text: typeof result === "string" ? result : JSON.stringify(result, null, 2) }] };
  } catch (e: any) {
    return { content: [{ type: "text", text: `Error: ${e.message}` }], isError: true };
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
main().catch(e => { console.error(e); process.exit(1); });

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);

function getRepoSnapshot() {
  const serverPath = path.join(__dirname, "simatic_s7_mcp_server.py");
  const ladPath = path.join(__dirname, "lad_converter.py");
  const pkgDir = path.join(__dirname, "simatic_s7_mcp");
  let serverLines = 0;
  let ladLines = 0;
  let packageLines = 0;
  let toolNames = [];
  let resourceUris = [];
  let promptNames = [];
  try {
    const content = fs.readFileSync(serverPath, "utf8");
    serverLines = content.split("\n").length;
    toolNames = [...content.matchAll(/@mcp\.tool\([^\n]*\)\s*(?:@[^\n]+\s*)*def\s+([a-zA-Z0-9_]+)/g)].map((m) => m[1]);
    resourceUris = [...content.matchAll(/@mcp\.resource\("([^"]+)"\)/g)].map((m) => m[1]);
    promptNames = [...content.matchAll(/@mcp\.prompt\([^\n]*\)\s*def\s+([a-zA-Z0-9_]+)/g)].map((m) => m[1]);
  } catch {
    // ignore
  }
  try {
    ladLines = fs.readFileSync(ladPath, "utf8").split("\n").length;
  } catch {
    // ignore
  }
  try {
    for (const file of fs.readdirSync(pkgDir)) {
      if (file.endsWith(".py")) {
        packageLines += fs.readFileSync(path.join(pkgDir, file), "utf8").split("\n").length;
      }
    }
  } catch {
    // ignore
  }
  return {
    serverLines,
    ladLines,
    packageLines,
    toolCount: toolNames.length,
    resourceCount: resourceUris.length,
    promptCount: promptNames.length,
    toolNames,
    resourceUris,
    promptNames,
  };
}

function renderHtml(snap) {
  const toolRows = snap.toolNames
    .map((name) => `<li><code>${name}</code></li>`)
    .join("");
  const resRows = snap.resourceUris
    .map((uri) => `<li><code>${uri}</code></li>`)
    .join("");
  const promptRows = snap.promptNames
    .map((name) => `<li><code>${name}</code></li>`)
    .join("");
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SIMATIC STEP 7 Classic MCP Server</title>
  <meta name="description" content="Production-grade Model Context Protocol (MCP) server for Siemens SIMATIC Manager / STEP 7 Classic (S7-300, S7-400, WinAC)." />
  <meta property="og:title" content="SIMATIC STEP 7 Classic MCP Server" />
  <meta property="og:description" content="Production-grade Model Context Protocol (MCP) server for Siemens SIMATIC Manager / STEP 7 Classic." />
  <style>
    :root { color-scheme: dark; --bg: #0c1015; --card: #131a22; --border: #243040; --fg: #e6edf3; --muted: #8b9bb0; --accent: #00b8a9; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background: var(--bg); color: var(--fg); line-height: 1.5; }
    main { max-width: 1080px; margin: 0 auto; padding: 40px 24px 64px; }
    header { border-bottom: 1px solid var(--border); padding-bottom: 20px; margin-bottom: 28px; }
    h1 { margin: 0 0 8px; font-size: 26px; letter-spacing: -0.02em; }
    .sub { color: var(--muted); margin: 0; font-size: 14px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 14px; margin-bottom: 24px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 18px; margin-bottom: 18px; }
    .grid .card { margin-bottom: 0; }
    .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
    .val { font-size: 24px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; color: var(--accent); }
    h2 { font-size: 15px; margin: 0 0 12px; color: var(--fg); }
    ul { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px; }
    li { min-width: 0; background: #0f151c; border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-size: 12.5px; overflow-wrap: anywhere; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #cde8e5; }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>SIMATIC STEP 7 Classic MCP Server v2.0</h1>
      <p class="sub">Production-grade stdio MCP Server for Siemens SIMATIC Manager (S7-300 / S7-400 / WinAC) — VS Code, Antigravity, Codex & Claude Code Ready</p>
    </header>
    <section class="grid">
      <div class="card"><div class="label">MCP Tools</div><div class="val">${snap.toolCount}</div></div>
      <div class="card"><div class="label">Resources / Prompts</div><div class="val">${snap.resourceCount} / ${snap.promptCount}</div></div>
      <div class="card"><div class="label">Core Engine LOC</div><div class="val">${snap.packageLines.toLocaleString()}</div></div>
      <div class="card"><div class="label">LAD AST Engine</div><div class="val">${snap.ladLines.toLocaleString()}</div></div>
      <div class="card"><div class="label">Preview Host</div><div class="val">Online :${PORT}</div></div>
    </section>
    <section class="card">
      <h2>Registered MCP Tools (${snap.toolCount})</h2>
      <ul>${toolRows}</ul>
    </section>
    <section class="card">
      <h2>MCP Resources (${snap.resourceCount}) &amp; Guided Prompts (${snap.promptCount})</h2>
      <ul>${resRows}${promptRows}</ul>
    </section>
  </main>
</body>
</html>`;
}

const server = http.createServer((req, res) => {
  const snap = getRepoSnapshot();
  if (req.url === "/api/health") {
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ ok: true, port: PORT, ...snap }));
    return;
  }
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(renderHtml(snap));
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`SIMATIC STEP 7 MCP preview host listening on http://0.0.0.0:${PORT}`);
});

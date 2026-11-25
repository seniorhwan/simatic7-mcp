import http from "node:http";

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || "0.0.0.0";

const server = http.createServer((req, res) => {
  if (req.url === "/api/health") {
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify({
        ok: true,
        service: "simatic-s7-mcp-server",
        transport: "stdio",
        note: "Headless Python stdio MCP server — run via VS Code, Antigravity, Codex, or Claude Code.",
      }),
    );
    return;
  }
  res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
  res.end(
    "SIMATIC STEP 7 Classic MCP Server is a headless Python stdio server.\n" +
      "Run: python simatic_s7_mcp_server.py doctor\n" +
      "Or:  python simatic_s7_mcp_server.py init-client --client all\n",
  );
});

server.listen(PORT, HOST, () => {
  console.log(`SIMATIC STEP 7 MCP health stub listening on http://${HOST}:${PORT}`);
});

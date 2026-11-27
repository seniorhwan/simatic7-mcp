import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || "0.0.0.0";
const DIST_DIR = path.join(__dirname, "dist");

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

class PythonVerificationDaemon {
  constructor() {
    this.proc = null;
    this.rl = null;
    this.readyPromise = null;
    this.queue = Promise.resolve();
    this.pendingResolve = null;
    this.pendingReject = null;
    this.pendingTimer = null;
  }

  ensureStarted() {
    if (this.proc && this.proc.exitCode === null && this.readyPromise) {
      return this.readyPromise;
    }
    this.readyPromise = new Promise((resolve, reject) => {
      const pyBin = process.platform === "win32" ? "python" : "python3";
      const child = spawn(pyBin, ["-u", "-m", "simatic_s7_mcp.verification_studio", "--daemon"], {
        cwd: __dirname,
        stdio: ["pipe", "pipe", "pipe"],
      });
      this.proc = child;
      this.rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });

      let initialized = false;
      const startTimer = setTimeout(() => {
        if (!initialized) {
          this.kill();
          reject(new Error("Python verification daemon timed out during startup."));
        }
      }, 12000);

      this.rl.on("line", (line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        let parsed;
        try {
          parsed = JSON.parse(trimmed);
        } catch (err) {
          if (!initialized) {
            clearTimeout(startTimer);
            reject(new Error(`Invalid startup JSON from daemon: ${trimmed}`));
          } else if (this.pendingReject) {
            const rej = this.pendingReject;
            this._clearPending();
            rej(new Error(`Invalid JSON from daemon: ${err.message}`));
          }
          return;
        }
        if (!initialized) {
          initialized = true;
          clearTimeout(startTimer);
          if (parsed && parsed.ok && parsed.ready) {
            resolve();
          } else {
            reject(new Error(`Daemon startup failed: ${trimmed}`));
          }
          return;
        }
        if (this.pendingResolve) {
          const res = this.pendingResolve;
          this._clearPending();
          res(parsed);
        }
      });

      child.stderr.on("data", (chunk) => {
        const msg = chunk.toString("utf-8").trim();
        if (msg) console.error(`[verification-daemon] ${msg}`);
      });

      child.on("exit", (code) => {
        if (!initialized) {
          clearTimeout(startTimer);
          reject(new Error(`Python verification daemon exited early (code ${code}).`));
        }
        if (this.pendingReject) {
          const rej = this.pendingReject;
          this._clearPending();
          rej(new Error(`Python verification daemon exited unexpectedly (code ${code}).`));
        }
        this.proc = null;
        this.readyPromise = null;
      });
    });
    return this.readyPromise;
  }

  _clearPending() {
    if (this.pendingTimer) {
      clearTimeout(this.pendingTimer);
      this.pendingTimer = null;
    }
    this.pendingResolve = null;
    this.pendingReject = null;
  }

  kill() {
    this._clearPending();
    if (this.proc) {
      try {
        this.proc.kill();
      } catch {
        // ignore
      }
      this.proc = null;
    }
    this.readyPromise = null;
  }

  call(payload, timeoutMs = 20000) {
    const task = async () => {
      await this.ensureStarted();
      return new Promise((resolve, reject) => {
        if (!this.proc || !this.proc.stdin) {
          reject(new Error("Python verification daemon stdin unavailable."));
          return;
        }
        this.pendingResolve = resolve;
        this.pendingReject = reject;
        this.pendingTimer = setTimeout(() => {
          this.kill();
          reject(new Error(`Verification command '${payload.cmd}' timed out after ${timeoutMs}ms.`));
        }, timeoutMs);
        this.proc.stdin.write(`${JSON.stringify(payload)}\n`);
      });
    };
    const next = this.queue.then(task, task);
    this.queue = next.catch(() => {});
    return next;
  }
}

const daemon = new PythonVerificationDaemon();

process.on("SIGINT", () => {
  daemon.kill();
  process.exit(0);
});
process.on("SIGTERM", () => {
  daemon.kill();
  process.exit(0);
});

function sendJson(res, statusCode, body) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(body));
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on("data", (c) => {
      total += c.length;
      if (total > 2 * 1024 * 1024) {
        reject(new Error("Request payload too large (max 2MB)."));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      if (chunks.length === 0) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf-8")));
      } catch {
        reject(new Error("Invalid JSON request body."));
      }
    });
    req.on("error", reject);
  });
}

async function handleApi(req, res, urlObj) {
  const pathname = urlObj.pathname;
  const method = req.method || "GET";

  if (pathname === "/api/health") {
    return sendJson(res, 200, {
      ok: true,
      service: "simatic-s7-verification-studio",
      transport: "http+stdio-mcp",
      daemon_ready: Boolean(daemon.proc && daemon.proc.exitCode === null),
    });
  }

  try {
    if (pathname === "/api/sim/state" && method === "GET") {
      return sendJson(res, 200, await daemon.call({ cmd: "get_state" }));
    }
    if (pathname === "/api/sim/presets" && method === "GET") {
      return sendJson(res, 200, await daemon.call({ cmd: "list_presets" }));
    }
    if (pathname === "/api/sim/fat/report" && method === "GET") {
      const fmt = urlObj.searchParams.get("format") || "markdown";
      return sendJson(res, 200, await daemon.call({ cmd: "fat_report", format: fmt }));
    }

    if (method === "POST") {
      const body = await readJsonBody(req);
      const routeToCmd = {
        "/api/sim/load": "load",
        "/api/sim/control": "control",
        "/api/sim/step": "step",
        "/api/sim/write": "write",
        "/api/sim/force": "force",
        "/api/sim/fault": "fault",
        "/api/sim/breakpoints": "breakpoints",
        "/api/sim/trace": "trace",
        "/api/sim/vat": "vat",
        "/api/sim/fat/run": "fat_run",
        "/api/sim/bridge/plcsim": "bridge_plcsim",
        "/api/sim/bridge/diagnose": "bridge_diagnose",
      };
      const cmd = routeToCmd[pathname];
      if (cmd) {
        const result = await daemon.call({ ...body, cmd }, cmd === "fat_run" || cmd === "bridge_diagnose" ? 30000 : 15000);
        return sendJson(res, result && result.ok === false ? 400 : 200, result);
      }
    }

    return sendJson(res, 404, { ok: false, error: `Unknown API route: ${method} ${pathname}` });
  } catch (err) {
    return sendJson(res, 500, { ok: false, error: err instanceof Error ? err.message : String(err) });
  }
}

function serveStatic(req, res, urlObj) {
  let relPath = decodeURIComponent(urlObj.pathname || "/");
  if (relPath === "/") relPath = "/index.html";
  const resolved = path.resolve(DIST_DIR, `.${relPath}`);
  if (!resolved.startsWith(DIST_DIR)) {
    res.writeHead(403, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Forbidden");
    return;
  }

  let targetFile = resolved;
  if (!fs.existsSync(targetFile) || fs.statSync(targetFile).isDirectory()) {
    targetFile = path.join(DIST_DIR, "index.html");
  }

  if (!fs.existsSync(targetFile)) {
    res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
    res.end(
      "SIMATIC STEP 7 Verification Studio frontend is not built yet.\n" +
        "Run: npm run build\n",
    );
    return;
  }

  const ext = path.extname(targetFile).toLowerCase();
  const contentType = MIME_TYPES[ext] || "application/octet-stream";
  res.writeHead(200, { "Content-Type": contentType });
  fs.createReadStream(targetFile).pipe(res);
}

const server = http.createServer((req, res) => {
  const urlObj = new URL(req.url || "/", `http://${HOST}:${PORT}`);
  if (urlObj.pathname.startsWith("/api/")) {
    void handleApi(req, res, urlObj);
    return;
  }
  serveStatic(req, res, urlObj);
});

server.listen(PORT, HOST, () => {
  console.log(`SIMATIC STEP 7 Verification Studio listening on http://${HOST}:${PORT}`);
  daemon.ensureStarted().catch((err) => {
    console.error(`[verification-daemon] Startup warning: ${err.message}`);
  });
});

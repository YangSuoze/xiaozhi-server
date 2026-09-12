#!/usr/bin/env node
/** Bridge Xiaozhi voice jobs to tasks owned by the Codex desktop app. */

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { hostname } from "node:os";
import net from "node:net";
import process from "node:process";
import readline from "node:readline";

const VERSION = "2.0.0";
const APP_TOOLS_PIPE = "CODEX_APP_TOOLS_PIPE_PATH";
const DEFAULT_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex";

function log(level, message, error = null) {
  const detail = error instanceof Error ? `: ${error.message}` : "";
  process.stderr.write(`${new Date().toISOString()} ${level} ${message}${detail}\n`);
}

function parseArgs(argv) {
  const values = {
    serverUrl: "http://127.0.0.1:18003",
    codexBin: DEFAULT_CODEX,
    pollInterval: 1000,
    heartbeatInterval: 15000,
    monitorInterval: 5000,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name.startsWith("--") || value == null) {
      throw new Error(`invalid argument: ${name}`);
    }
    index += 1;
    if (name === "--server-url") values.serverUrl = value;
    else if (name === "--token-file") values.tokenFile = value;
    else if (name === "--token") values.token = value;
    else if (name === "--codex-bin") values.codexBin = value;
    else if (name === "--poll-interval-ms") values.pollInterval = Number(value);
    else if (name === "--heartbeat-interval-ms") values.heartbeatInterval = Number(value);
    else if (name === "--monitor-interval-ms") values.monitorInterval = Number(value);
    else throw new Error(`unknown argument: ${name}`);
  }
  if (values.tokenFile) values.token = readFileSync(values.tokenFile, "utf8").trim();
  if (!values.token || values.token.length < 32) {
    throw new Error("bridge token must contain at least 32 characters");
  }
  const url = new URL(values.serverUrl);
  const loopback = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);
  if (url.protocol !== "https:" && !loopback.has(url.hostname)) {
    throw new Error("public bridge URLs must use HTTPS; use an SSH tunnel for HTTP");
  }
  return values;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function normalizedStatus(value) {
  const status = typeof value === "object" ? value?.type : value;
  const mapping = {
    active: "active",
    running: "active",
    idle: "idle",
    completed: "idle",
    failed: "failed",
    interrupted: "interrupted",
    notLoaded: "not_loaded",
    not_loaded: "not_loaded",
    waitingOnUserInput: "waiting_user_input",
    waiting_user_input: "waiting_user_input",
    waitingOnApproval: "waiting_approval",
    waiting_approval: "waiting_approval",
  };
  return mapping[String(status)] ?? "unknown";
}

function parseToolResult(result) {
  const texts = (result.contentItems ?? [])
    .filter((item) => item.type === "inputText")
    .map((item) => item.text);
  if (!result?.success) {
    throw new Error(`Codex desktop tool call failed: ${texts.join(" ").slice(0, 500)}`);
  }
  for (const text of texts.toReversed()) {
    try {
      return JSON.parse(text);
    } catch {
      // Some app tools return plain text. Keep searching for structured output.
    }
  }
  return { text: texts.join("\n") };
}

class CloudClient {
  constructor(options) {
    this.baseUrl = options.serverUrl.replace(/\/$/, "");
    this.token = options.token;
    this.bridgeId = `mac-${hostname()}`.replace(/[^A-Za-z0-9._:-]/g, "-");
  }

  async request(path, body = null) {
    const response = await fetch(`${this.baseUrl}${path}`, {
      method: body == null ? "GET" : "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Codex-Bridge-Token": this.token,
      },
      body: body == null ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      throw new Error(`bridge API ${response.status}: ${(await response.text()).slice(0, 300)}`);
    }
    return response.json();
  }

  heartbeat() {
    return this.request("/codex/bridge/v1/heartbeat", {
      bridge_id: this.bridgeId,
      hostname: hostname(),
      version: VERSION,
      capabilities: { desktop_app_tools: true, task_monitoring: true },
    });
  }

  async leaseJob() {
    const result = await this.request("/codex/bridge/v1/jobs/lease", {
      bridge_id: this.bridgeId,
    });
    return result.job ?? null;
  }

  completeJob(jobId, response = null, error = null) {
    return this.request("/codex/bridge/v1/jobs/complete", {
      bridge_id: this.bridgeId,
      job_id: jobId,
      response,
      error,
    });
  }

  postEvent(event) {
    return this.request("/codex/bridge/v1/events", {
      bridge_id: this.bridgeId,
      event,
    });
  }
}

class JsonLineClient {
  constructor(command) {
    this.command = command;
    this.process = null;
    this.nextId = 1;
    this.pending = new Map();
  }

  async start() {
    if (this.process && this.process.exitCode == null) return;
    const child = spawn(this.command, ["app-server", "--stdio"], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.process = child;
    readline.createInterface({ input: child.stdout }).on("line", (line) => {
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        return;
      }
      const pending = this.pending.get(message.id);
      if (!pending || !("result" in message || "error" in message)) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(message.error.message ?? String(message.error)));
      else pending.resolve(message.result ?? {});
    });
    child.stderr.on("data", () => {});
    child.on("exit", () => {
      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer);
        pending.reject(new Error("Codex catalog process exited"));
      }
      this.pending.clear();
      this.process = null;
    });
    await this.request("initialize", {
      clientInfo: { name: "xiaozhi-codex-catalog", version: VERSION },
      capabilities: { experimentalApi: true },
    });
    this.notify("initialized", {});
  }

  request(method, params, timeout = 30000) {
    const child = this.process;
    if (!child?.stdin || child.exitCode != null) throw new Error("Codex catalog is offline");
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Codex request timed out: ${method}`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ id, method, params })}\n`);
    });
  }

  notify(method, params) {
    this.process?.stdin?.write(`${JSON.stringify({ method, params })}\n`);
  }

  async recentLocalThreads(limit = 20) {
    await this.start();
    const result = await this.request("thread/list", {
      limit,
      archived: false,
      sortKey: "recency_at",
      sortDirection: "desc",
    });
    return result.data ?? [];
  }

  close() {
    this.process?.kill();
  }
}

class NativeAppToolsClient {
  constructor(pipePath) {
    if (!pipePath) throw new Error(`${APP_TOOLS_PIPE} was not supplied by Codex desktop`);
    this.pipePath = pipePath;
    this.socket = null;
    this.connecting = null;
    this.buffer = Buffer.alloc(0);
    this.nextId = 1;
    this.pending = new Map();
    this.toolNamespaces = new Map();
  }

  async connect() {
    if (this.socket && !this.socket.destroyed) return;
    if (this.connecting) return this.connecting;
    this.connecting = new Promise((resolve, reject) => {
      const socket = net.createConnection(this.pipePath);
      const failed = (error) => {
        socket.destroy();
        reject(error);
      };
      socket.once("error", failed);
      socket.once("connect", () => {
        socket.off("error", failed);
        this.socket = socket;
        this.connecting = null;
        socket.on("data", (chunk) => this.onData(chunk));
        socket.on("error", (error) => this.disconnect(error));
        socket.on("close", () => this.disconnect(new Error("Codex app tools pipe closed")));
        resolve();
      });
    }).catch((error) => {
      this.connecting = null;
      throw error;
    });
    await this.connecting;
    if (this.toolNamespaces.size === 0) {
      const result = await this.request("tools/list", { threadStartKind: "all" });
      for (const tool of result.tools ?? []) this.toolNamespaces.set(tool.name, tool.namespace);
    }
  }

  disconnect(error) {
    if (this.socket) this.socket.destroy();
    this.socket = null;
    this.buffer = Buffer.alloc(0);
    this.toolNamespaces.clear();
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  send(message) {
    const payload = Buffer.from(JSON.stringify(message), "utf8");
    const frame = Buffer.alloc(4 + payload.length);
    frame.writeUInt32LE(payload.length, 0);
    payload.copy(frame, 4);
    this.socket.write(frame);
  }

  async request(method, params, timeout = 60000) {
    if (!this.socket || this.socket.destroyed) {
      if (method === "tools/list") throw new Error("Codex app tools pipe is offline");
      await this.connect();
    }
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Codex app tool timed out: ${method}`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.send({ id, jsonrpc: "2.0", method, params });
    });
  }

  onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length >= 4) {
      const size = this.buffer.readUInt32LE(0);
      if (size > 8 * 1024 * 1024) return this.disconnect(new Error("oversized app tool response"));
      if (this.buffer.length < size + 4) return;
      const message = JSON.parse(this.buffer.subarray(4, size + 4).toString("utf8"));
      this.buffer = this.buffer.subarray(size + 4);
      const pending = this.pending.get(Number(message.id));
      if (!pending) continue;
      this.pending.delete(Number(message.id));
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result ?? {});
    }
  }

  async call(tool, argumentsValue, callerThreadId) {
    await this.connect();
    const namespace = this.toolNamespaces.get(tool);
    if (!namespace) throw new Error(`Codex desktop tool is unavailable: ${tool}`);
    const suffix = `${Date.now()}-${this.nextId}`;
    const result = await this.request("tools/call", {
      arguments: argumentsValue,
      callId: `xiaozhi-${tool}-${suffix}`,
      namespace,
      threadId: callerThreadId,
      tool,
      turnId: `xiaozhi-voice-${suffix}`,
    });
    return parseToolResult(result);
  }
}

class VoiceBridge {
  constructor(options) {
    this.options = options;
    this.cloud = new CloudClient(options);
    this.catalog = new JsonLineClient(options.codexBin);
    this.appTools = new NativeAppToolsClient(process.env[APP_TOOLS_PIPE]);
    this.monitored = new Map();
    this.cachedTasks = null;
    this.cacheTime = 0;
    this.stopping = false;
  }

  async callerThreadId() {
    const threads = await this.catalog.recentLocalThreads(1);
    const id = threads[0]?.id ?? threads[0]?.sessionId;
    if (!id) throw new Error("Codex desktop has no local task to use as the bridge context");
    return id;
  }

  async listTasks(limit = 3, force = false) {
    if (!force && this.cachedTasks && Date.now() - this.cacheTime < 2000) {
      return this.cachedTasks.slice(0, limit);
    }
    const caller = await this.callerThreadId();
    const result = await this.appTools.call("list_threads", { limit: 50 }, caller);
    const seen = new Set();
    const tasks = [...(result.pinnedThreads ?? []), ...(result.threads ?? [])]
      .filter((item) => item.kind === "codex" && item.id && !seen.has(item.id) && seen.add(item.id))
      .sort((left, right) => Number(right.updatedAt ?? 0) - Number(left.updatedAt ?? 0))
      .map((item) => ({
        id: item.id,
        title: String(item.title || "未命名任务").split("\n")[0],
        status: normalizedStatus(item.status),
        updated_at: item.updatedAt ?? null,
        host_id: item.hostId ?? null,
      }));
    this.cachedTasks = tasks;
    this.cacheTime = Date.now();
    return tasks.slice(0, Math.max(1, Math.min(100, Number(limit) || 3)));
  }

  async taskStatus(threadId, hostId = null) {
    const tasks = await this.listTasks(100, true);
    const task = tasks.find((item) => item.id === threadId);
    if (!task) throw new Error(`Codex desktop task not found: ${threadId}`);
    return { status: task.status, host_id: hostId ?? task.host_id };
  }

  async sendToTask(payload) {
    const caller = await this.callerThreadId();
    const argumentsValue = { threadId: String(payload.thread_id), prompt: String(payload.text ?? "") };
    if (payload.host_id) argumentsValue.hostId = payload.host_id;
    await this.appTools.call("send_message_to_thread", argumentsValue, caller);
    this.cachedTasks = null;
    return { accepted: true, turn_id: null, steered: payload.mode === "steer" };
  }

  async executeJob(job) {
    const payload = job.payload ?? {};
    if (job.kind === "list_threads") return { threads: await this.listTasks(payload.limit ?? 3, true) };
    if (job.kind === "monitor_thread") {
      const result = await this.taskStatus(String(payload.thread_id), payload.host_id);
      this.monitored.set(String(payload.thread_id), { hostId: result.host_id, status: result.status });
      return { status: result.status, turn_id: null };
    }
    if (job.kind === "get_status") return this.taskStatus(String(payload.thread_id), payload.host_id);
    if (job.kind === "send_message") return this.sendToTask(payload);
    if (job.kind === "respond_request") {
      const text = payload.approved == null ? payload.answer : payload.approved ? "批准" : "拒绝";
      return this.sendToTask({ ...payload, text });
    }
    if (job.kind === "unmonitor_thread") {
      this.monitored.delete(String(payload.thread_id));
      return { unmonitored: true };
    }
    throw new Error(`unsupported bridge job: ${job.kind}`);
  }

  async processJob(job) {
    try {
      const response = await this.executeJob(job);
      await this.cloud.completeJob(String(job.id), response);
    } catch (error) {
      log("ERROR", `job ${job.id} failed`, error);
      await this.cloud.completeJob(String(job.id), null, error.message.slice(0, 1000));
    }
  }

  async pollMonitoredTasks() {
    if (this.monitored.size === 0) return;
    const tasks = await this.listTasks(100, true);
    const byId = new Map(tasks.map((item) => [item.id, item]));
    for (const [threadId, state] of this.monitored) {
      const task = byId.get(threadId);
      if (!task || task.status === state.status) continue;
      const previous = state.status;
      state.status = task.status;
      await this.cloud.postEvent({
        type: "thread_status",
        thread_id: threadId,
        status: task.status,
        turn_id: null,
      });
      if (previous === "active" && task.status === "idle") {
        await this.cloud.postEvent({
          type: "turn_completed",
          thread_id: threadId,
          turn_id: null,
          status: "completed",
          summary: "",
        });
      }
    }
  }

  async run() {
    let nextHeartbeat = 0;
    let nextMonitor = 0;
    let backoff = 1000;
    while (!this.stopping) {
      try {
        const now = Date.now();
        if (now >= nextHeartbeat) {
          await this.cloud.heartbeat();
          nextHeartbeat = now + this.options.heartbeatInterval;
        }
        if (now >= nextMonitor) {
          await this.pollMonitoredTasks();
          nextMonitor = now + this.options.monitorInterval;
        }
        const job = await this.cloud.leaseJob();
        if (job) await this.processJob(job);
        backoff = 1000;
        await sleep(this.options.pollInterval);
      } catch (error) {
        log("WARN", "bridge loop retrying", error);
        await sleep(backoff);
        backoff = Math.min(30000, backoff * 2);
      }
    }
  }

  close() {
    this.stopping = true;
    this.catalog.close();
    this.appTools.disconnect(new Error("bridge stopped"));
  }
}

function startMcpStdio() {
  const input = readline.createInterface({ input: process.stdin });
  input.on("line", (line) => {
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      return;
    }
    if (request.id == null) return;
    let result;
    if (request.method === "initialize") {
      result = {
        protocolVersion: request.params?.protocolVersion ?? "2025-11-25",
        capabilities: {},
        serverInfo: { name: "xiaozhi-codex-voice-bridge", version: VERSION },
      };
    } else if (request.method === "ping") result = {};
    else {
      process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, error: { code: -32601, message: "Method not found" } })}\n`);
      return;
    }
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n`);
  });
  return input;
}

let bridge;
try {
  const options = parseArgs(process.argv.slice(2));
  const mcpInput = startMcpStdio();
  bridge = new VoiceBridge(options);
  const shutdown = () => {
    mcpInput.close();
    bridge.close();
    setTimeout(() => process.exit(0), 100);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  await bridge.run();
} catch (error) {
  log("FATAL", "Codex voice bridge could not start", error);
  process.exitCode = 1;
}

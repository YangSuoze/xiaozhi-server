#!/usr/bin/env node
/** Bridge Xiaozhi voice jobs to tasks owned by the Codex desktop app. */

import { spawn } from "node:child_process";
import { closeSync, openSync, readFileSync, readSync, statSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { join, resolve, sep } from "node:path";
import net from "node:net";
import process from "node:process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const VERSION = "2.1.1";
const APP_TOOLS_PIPE = "CODEX_APP_TOOLS_PIPE_PATH";
const DEFAULT_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex";
const MAX_ROLLOUT_READ_BYTES = 2 * 1024 * 1024;

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
  const flags = new Set(typeof value === "object" ? value?.activeFlags ?? [] : []);
  if (flags.has("waitingOnUserInput")) return "waiting_user_input";
  if (flags.has("waitingOnApproval")) return "waiting_approval";
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

function limitedText(value, limit) {
  const text = String(value ?? "").replace(/\s+/gu, " ").trim();
  return text.length <= limit ? text : `${text.slice(0, limit - 1).trimEnd()}…`;
}

export function cleanProgressText(value, limit = 220) {
  let text = String(value ?? "");
  text = text.replace(/```[\s\S]*?```/gu, " ");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/gu, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/gu, "$1");
  text = text.replace(/https?:\/\/\S+/giu, "网页链接");
  text = text.replace(/\/(?:Users|home|tmp|var|Applications|Volumes)\/[^\s，。；！？,;!?]+/gu, "本地文件");
  text = text.replace(/[A-Za-z]:\\[^\s，。；！？,;!?]+/gu, "本地文件");
  text = text.replace(
    /\b(api[_-]?key|access[_-]?key|token|secret|password)\b\s*[:=]\s*[^\s，。；！？,;!?]+/giu,
    "$1已隐藏",
  );
  text = text.replace(/\b[A-Za-z0-9_-]{40,}\b/gu, "敏感内容已隐藏");
  text = text.replace(/<[^>]{1,200}>/gu, " ");
  text = text.replace(/[`*_>#|]/gu, " ");
  text = text.replace(/\s+本地文件/gu, "本地文件");
  return limitedText(text, limit);
}

function sentences(text) {
  return String(text ?? "")
    .split(/(?<=[。！？.!?；;])\s*/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

function matchingSentence(text, pattern) {
  return sentences(text).find((item) => pattern.test(item)) ?? "";
}

function toolActivity(marker) {
  if (!marker) return "";
  const mapping = {
    commandExecution: "正在运行命令或测试",
    fileChange: "正在修改文件",
    mcpToolCall: "正在调用桌面工具",
    imageView: "正在检查图片",
    subAgentActivity: "正在协调子任务",
  };
  return mapping[marker.type] ?? "正在处理当前任务";
}

export function buildProgressSnapshot(poll, fallbackStatus = "unknown") {
  if (!poll || typeof poll !== "object") return null;
  const status = normalizedStatus(poll.thread?.status ?? fallbackStatus);
  const message = poll.latestAssistantMessage ?? null;
  const cleaned = cleanProgressText(message?.text);
  const phase = message?.phase;
  let currentAction = "";
  let recentResult = "";
  let nextStep = "";

  if (phase === "final_answer") {
    recentResult = cleaned;
  } else if (cleaned) {
    const messageSentences = sentences(cleaned);
    recentResult = cleanProgressText(
      matchingSentence(cleaned, /已经|已完成|完成了|通过了|定位到|发现了|修复了|生成了|部署了/u),
      180,
    );
    nextStep = cleanProgressText(
      matchingSentence(cleaned, /下一步|接下来|随后|之后会|准备|我会|将会|会继续/u),
      160,
    );
    const activeSentence =
      messageSentences.find(
        (item) => item !== recentResult && /正在|当前|现在|开始|处理中|着手/u.test(item),
      ) ||
      messageSentences.find(
        (item) =>
          item !== recentResult &&
          item !== nextStep &&
          /处理|检查|修改|运行|部署|生成/u.test(item),
      );
    currentAction = cleanProgressText(
      activeSentence || (!recentResult && !nextStep ? messageSentences[0] : ""),
      180,
    );
  } else if (status === "active") {
    currentAction = toolActivity(poll.latestToolMarker);
  }
  if (!currentAction && status === "active") currentAction = toolActivity(poll.latestToolMarker);
  if (!recentResult && poll.previousAssistantMessage?.phase === "final_answer") {
    recentResult = cleanProgressText(poll.previousAssistantMessage.text, 180);
  }

  const revisionSource =
    message?.id ??
    [poll.latestTurn?.id, poll.latestTurn?.status, poll.latestToolMarker?.id, status]
      .filter(Boolean)
      .join(":");
  if (!revisionSource && !currentAction && !recentResult) return null;
  return {
    current_action: currentAction || null,
    recent_result: recentResult || null,
    next_step: nextStep || null,
    needs_input: ["waiting_user_input", "waiting_approval"].includes(status),
    revision: limitedText(revisionSource, 180),
    source_message_id: message?.id ?? null,
    updated_at: Date.now() / 1000,
  };
}

function rolloutMessage(item, turnId) {
  if (item?.type !== "AgentMessage") return null;
  const text = (item.content ?? [])
    .filter((content) => content.type === "Text")
    .map((content) => content.text)
    .join("\n");
  if (!text) return null;
  return { id: item.id, turnId, phase: item.phase, text };
}

function rolloutMarker(item, turnId) {
  const mapping = {
    CommandExecution: "commandExecution",
    FileChange: "fileChange",
    McpToolCall: "mcpToolCall",
    ImageView: "imageView",
    SubAgentActivity: "subAgentActivity",
  };
  const type = mapping[item?.type];
  return type ? { id: item.id, turnId, type, status: item.status } : null;
}

export class RolloutProgressReader {
  constructor(root = join(homedir(), ".codex", "sessions")) {
    this.root = resolve(root);
    this.states = new Map();
  }

  snapshot(threadId, filePath, currentTurnId) {
    if (!filePath) return {};
    const path = resolve(filePath);
    if (!path.startsWith(`${this.root}${sep}`)) return {};
    let size;
    try {
      size = statSync(path).size;
    } catch {
      return {};
    }

    let state = this.states.get(threadId);
    if (!state || state.path !== path || state.offset > size) {
      state = {
        path,
        offset: Math.max(0, size - MAX_ROLLOUT_READ_BYTES),
        remainder: "",
        messages: new Map(),
        markers: new Map(),
        latestFinal: null,
        discardPrefix: size > MAX_ROLLOUT_READ_BYTES,
      };
      this.states.set(threadId, state);
    }
    if (state.offset < size) {
      let length = size - state.offset;
      if (length > MAX_ROLLOUT_READ_BYTES) {
        state.offset = size - MAX_ROLLOUT_READ_BYTES;
        state.remainder = "";
        state.discardPrefix = true;
        length = MAX_ROLLOUT_READ_BYTES;
      }
      const buffer = Buffer.alloc(length);
      const descriptor = openSync(path, "r");
      try {
        readSync(descriptor, buffer, 0, length, state.offset);
      } finally {
        closeSync(descriptor);
      }
      let chunk = buffer.toString("utf8");
      if (state.discardPrefix) {
        const newline = chunk.indexOf("\n");
        chunk = newline < 0 ? "" : chunk.slice(newline + 1);
        state.discardPrefix = false;
      }
      const lines = `${state.remainder}${chunk}`.split("\n");
      state.remainder = lines.pop() ?? "";
      for (const line of lines) this.consumeLine(state, line);
      state.offset = size;
    }
    return {
      latestAssistantMessage: state.messages.get(currentTurnId) ?? null,
      latestToolMarker: state.markers.get(currentTurnId) ?? null,
      previousAssistantMessage:
        state.latestFinal?.turnId === currentTurnId ? null : state.latestFinal,
    };
  }

  consumeLine(state, line) {
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      return;
    }
    const payload = record?.type === "event_msg" ? record.payload : null;
    if (payload?.type !== "item_completed") return;
    const turnId = payload.turn_id;
    const message = rolloutMessage(payload.item, turnId);
    if (message) {
      state.messages.set(turnId, message);
      if (message.phase === "final_answer") state.latestFinal = message;
      while (state.messages.size > 8) state.messages.delete(state.messages.keys().next().value);
      return;
    }
    const marker = rolloutMarker(payload.item, turnId);
    if (marker) {
      state.markers.set(turnId, marker);
      while (state.markers.size > 8) state.markers.delete(state.markers.keys().next().value);
    }
  }
}

function pollFromThreadRead(result) {
  const turns = result.turns ?? [];
  let latestAssistantMessage = null;
  let latestToolMarker = null;
  for (const turn of turns) {
    const items = turn.items ?? [];
    if (!latestAssistantMessage) {
      const message = items.filter((item) => item.type === "agentMessage").at(-1);
      if (message) latestAssistantMessage = { ...message, turnId: turn.id };
    }
    if (!latestToolMarker) {
      const marker = items
        .filter((item) =>
          ["commandExecution", "fileChange", "mcpToolCall", "imageView", "subAgentActivity"].includes(
            item.type,
          ),
        )
        .at(-1);
      if (marker) latestToolMarker = { ...marker, turnId: turn.id };
    }
    if (latestAssistantMessage && latestToolMarker) break;
  }
  return {
    thread: {
      id: result.thread?.id,
      hostId: result.thread?.hostId,
      status: result.thread?.status,
    },
    latestTurn: turns[0] ?? null,
    latestAssistantMessage,
    latestToolMarker,
  };
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
      capabilities: {
        desktop_app_tools: true,
        local_rollout_progress: true,
        task_monitoring: true,
        progress_snapshots: true,
      },
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
    this.rolloutPaths = new Map();
    this.rolloutReader = new RolloutProgressReader();
    this.monitored = new Map();
    this.cachedTasks = null;
    this.cacheTime = 0;
    this.stopping = false;
  }

  async callerThreadId(excludedThreadId = null) {
    const threads = await this.catalog.recentLocalThreads(20);
    for (const thread of threads) {
      const id = thread.id ?? thread.sessionId;
      if (id && thread.path) this.rolloutPaths.set(id, thread.path);
    }
    const selected =
      threads.find((item) => (item.id ?? item.sessionId) !== excludedThreadId) ?? threads[0];
    const id = selected?.id ?? selected?.sessionId;
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

  async taskSnapshot(threadId, hostId = null, cursor = null, previousProgress = null) {
    const caller = await this.callerThreadId(threadId);
    const target = { threadId };
    if (hostId) target.hostId = hostId;
    if (cursor) target.afterCursor = cursor;
    let poll;
    try {
      const result = await this.appTools.call(
        "wait_threads",
        { targets: [target], timeoutMs: 0 },
        caller,
      );
      poll =
        (result.polls ?? []).find((item) => item.thread?.id === threadId) ?? result.polls?.[0];
    } catch (error) {
      if (!String(error?.message).includes("calling thread")) throw error;
      const argumentsValue = {
        threadId,
        turnLimit: 3,
        includeOutputs: true,
        maxOutputCharsPerItem: 1200,
      };
      if (hostId) argumentsValue.hostId = hostId;
      poll = pollFromThreadRead(await this.appTools.call("read_thread", argumentsValue, caller));
    }
    const rollout = this.rolloutReader.snapshot(
      threadId,
      this.rolloutPaths.get(threadId),
      poll?.latestTurn?.id,
    );
    if (poll) {
      poll.latestAssistantMessage ??= rollout.latestAssistantMessage;
      poll.latestToolMarker ??= rollout.latestToolMarker;
      poll.previousAssistantMessage ??= rollout.previousAssistantMessage;
    }
    if (!poll) return { ...(await this.taskStatus(threadId, hostId)), progress: previousProgress, cursor };
    const status = normalizedStatus(poll.thread?.status);
    return {
      status,
      host_id: hostId ?? poll.thread?.hostId ?? null,
      turn_id: poll.latestTurn?.id ?? null,
      progress: buildProgressSnapshot(poll, status) ?? previousProgress,
      cursor: poll.cursor ?? cursor,
    };
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
      const result = await this.taskSnapshot(String(payload.thread_id), payload.host_id);
      this.monitored.set(String(payload.thread_id), {
        hostId: result.host_id,
        status: result.status,
        cursor: result.cursor,
        progress: result.progress,
      });
      return { status: result.status, turn_id: result.turn_id, progress: result.progress };
    }
    if (job.kind === "get_status") {
      const result = await this.taskSnapshot(String(payload.thread_id), payload.host_id);
      return { status: result.status, turn_id: result.turn_id, progress: result.progress };
    }
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
    for (const [threadId, state] of this.monitored) {
      const snapshot = await this.taskSnapshot(
        threadId,
        state.hostId,
        state.cursor,
        state.progress,
      );
      const previousStatus = state.status;
      const previousRevision = state.progress?.revision;
      state.hostId = snapshot.host_id;
      state.status = snapshot.status;
      state.cursor = snapshot.cursor;
      state.progress = snapshot.progress;
      if (
        snapshot.status === previousStatus &&
        snapshot.progress?.revision === previousRevision
      ) {
        continue;
      }
      await this.cloud.postEvent({
        type: "thread_progress",
        thread_id: threadId,
        status: snapshot.status,
        turn_id: snapshot.turn_id,
        progress: snapshot.progress,
      });
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

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
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
}

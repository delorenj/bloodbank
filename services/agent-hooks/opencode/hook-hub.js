import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

// OpenCode auto-loads .js/.ts files. One native adapter, no behavioral hooks.

// OpenCode (>=1.18) schema-checks injected parts: each needs its own ascending
// "prt_" id plus the owning sessionID/messageID, or the whole prompt is dropped.
const BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
let lastMs = 0;
let counter = 0;
const partID = () => {
  const ms = Date.now();
  if (ms !== lastMs) { lastMs = ms; counter = 0; }
  const time = (BigInt(ms) * 0x1000n + BigInt(++counter)) & 0xffffffffffffn;
  const rand = [...randomBytes(14)].map((b) => BASE62[b % 62]).join("");
  return `prt_${time.toString(16).padStart(12, "0")}${rand}`;
};

export const BloodbankHookHub = async ({ directory }) => {
  const command = process.env.BB_HOOK_COMMAND || join(homedir(), ".agents/hooks/bb-hook");
  const toolInputs = new Map();
  const completedCalls = new Set();
  const sessions = new Map();
  const turns = new Map();
  const emit = (native, payload) => new Promise((resolve) => {
    const slow = native === "chat.message" || native === "session.created";
    const args = ["--cli", "opencode", "--native", native];
    if (slow) args.push("--deadline", "15");
    const child = spawn(command, args, {
      cwd: directory, env: process.env, stdio: ["pipe", "pipe", "ignore"],
    });
    let output = "";
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(output);
    };
    const timer = setTimeout(() => { child.kill("SIGKILL"); finish(); }, slow ? 16000 : 4000);
    child.stdout.on("data", (chunk) => { if (output.length < 65536) output += chunk; });
    child.on("error", finish);
    child.on("close", finish);
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ cwd: directory, hook_event_name: native, ...payload }));
  });
  const ensureSession = (sessionID, source) => {
    if (!sessionID) return;
    let session = sessions.get(sessionID);
    if (!session) {
      session = { context: "" };
      sessions.set(sessionID, session);
      session.startup = emit("session.created", { session_id: sessionID, source }).then((raw) => {
        if (sessions.get(sessionID) === session) session.context = additionalContext(raw);
      });
    }
    return session;
  };
  const additionalContext = (raw) => {
    if (!raw.trim()) return "";
    try {
      const value = JSON.parse(raw);
      return value.hookSpecificOutput?.additionalContext || value.additionalContext || "";
    } catch { return raw.trim(); }
  };
  return {
    "chat.message": async (input, output) => {
      const session = ensureSession(input.sessionID, "resume");
      const firstMessage = session && !session.consumed;
      if (firstMessage) session.consumed = true;
      await session?.startup;
      const turn = input.messageID || output.message?.id;
      if (turn && sessions.get(input.sessionID) === session) turns.set(input.sessionID, turn);
      const prompt = output.parts.filter((p) => p.type === "text").map((p) => p.text).join("\n");
      const raw = await emit("chat.message", {
        session_id: input.sessionID, turn_id: turn, model: input.model, prompt,
      });
      const text = [
        firstMessage && sessions.get(input.sessionID) === session ? session.context : "",
        additionalContext(raw),
      ].filter(Boolean).join("\n\n");
      if (firstMessage) session.context = "";
      if (text) {
        output.parts.push({
          id: partID(),
          sessionID: output.message?.sessionID || input.sessionID,
          messageID: output.message?.id || input.messageID,
          type: "text", text, synthetic: true,
        });
      }
    },
    "tool.execute.before": async (input, output) => {
      const payload = {
        session_id: input.sessionID, turn_id: turns.get(input.sessionID),
        tool_name: input.tool, tool_call_id: input.callID, tool_input: output.args,
      };
      toolInputs.set(`${input.sessionID}:${input.callID}`, payload);
      await emit("tool.execute.before", payload);
    },
    "tool.execute.after": async (input, output) => {
      const key = `${input.sessionID}:${input.callID}`;
      if (completedCalls.has(key)) return;
      completedCalls.add(key);
      const previous = toolInputs.get(key) || {};
      toolInputs.delete(key);
      await emit("tool.execute.after", {
        ...previous, session_id: input.sessionID, tool_name: input.tool,
        tool_call_id: input.callID, tool_output: output.output,
        is_error: Boolean(output.metadata?.error),
      });
    },
    event: async ({ event }) => {
      const properties = event.properties || {};
      const sessionID = properties.sessionID || properties.info?.id;
      if (event.type === "session.created") {
        await ensureSession(sessionID, "startup")?.startup;
      } else if (event.type === "session.deleted") {
        sessions.delete(sessionID);
        turns.delete(sessionID);
        await emit(event.type, { session_id: sessionID, reason: "deleted" });
      } else if (["session.idle", "session.error", "permission.asked"].includes(event.type)) {
        await emit(event.type, {
          ...properties, session_id: sessionID, turn_id: turns.get(sessionID),
          error: properties.error, tool_name: properties.permission,
        });
      } else if (event.type === "message.part.updated") {
        const part = properties.part;
        // OpenCode's after hook is success-only. Its error part is the native
        // terminal signal for a failed/blocked tool; close the same call once.
        if (part?.type !== "tool" || part.state?.status !== "error") return;
        const key = `${part.sessionID}:${part.callID}`;
        const pending = toolInputs.get(key);
        if (!pending) return;
        toolInputs.delete(key);
        completedCalls.add(key);
        await emit("tool.execute.after", { ...pending, is_error: true, error: part.state.error });
      }
    },
  };
};

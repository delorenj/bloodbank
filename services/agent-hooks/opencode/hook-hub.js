import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

// OpenCode auto-loads .js/.ts files. One native adapter, no behavioral hooks.
export const BloodbankHookHub = async ({ directory }) => {
  const command = process.env.BB_HOOK_COMMAND || join(homedir(), ".agents/hooks/bb-hook");
  const toolInputs = new Map();
  const completedCalls = new Set();
  const sessions = new Set();
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
  const ensureSession = async (sessionID, source) => {
    if (!sessionID || sessions.has(sessionID)) return;
    sessions.add(sessionID);
    await emit("session.created", { session_id: sessionID, source });
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
      await ensureSession(input.sessionID, "resume");
      const turn = input.messageID || output.message?.id;
      if (turn) turns.set(input.sessionID, turn);
      const prompt = output.parts.filter((p) => p.type === "text").map((p) => p.text).join("\n");
      const raw = await emit("chat.message", {
        session_id: input.sessionID, turn_id: turn, model: input.model, prompt,
      });
      const text = additionalContext(raw);
      if (text) output.parts.push({ type: "text", text, synthetic: true });
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
        await ensureSession(sessionID, "startup");
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

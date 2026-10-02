import { writeFile } from "node:fs/promises";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Quota = { remainingPercent: number; resetAt: number };

const USAGE_URL = "https://opencode.ai/zen/go/v1/usage";

// The API reports used percent per window; remaining is its complement.
function normalizeWindow(value: unknown): Quota | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const window = value as { percent?: unknown; resetsAt?: unknown };
  if (typeof window.percent !== "number" || !Number.isFinite(window.percent)) return undefined;
  if (typeof window.resetsAt !== "string") return undefined;
  const resetMillis = Date.parse(window.resetsAt);
  if (!Number.isFinite(resetMillis)) return undefined;
  const remainingPercent = Math.round(
    Math.max(0, Math.min(100, 100 - window.percent)),
  );
  return { remainingPercent, resetAt: Math.floor(resetMillis / 1000) };
}

export default function captureOpencodeQuota(pi: ExtensionAPI) {
  pi.on("agent_end", async (_event, ctx) => {
    if (ctx.model?.provider !== "opencode-go") return;
    const outputPath = process.env.PI_OPENCODE_QUOTA_FILE;
    if (!outputPath) return;

    try {
      const apiKey = await ctx.modelRegistry.getApiKeyForProvider("opencode-go");
      if (!apiKey) throw new Error("OpenCode Go credentials unavailable");

      const response = await fetch(USAGE_URL, {
        headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
        signal: ctx.signal,
      });
      if (!response.ok) throw new Error("usage request rejected");
      const payload = (await response.json()) as { usage?: Record<string, unknown> };
      const usage = payload?.usage ?? {};

      await writeFile(
        outputPath,
        JSON.stringify(
          {
            capturedAt: new Date().toISOString(),
            fiveHour: normalizeWindow(usage.rolling),
            weekly: normalizeWindow(usage.weekly),
            monthly: normalizeWindow(usage.monthly),
          },
          null,
          2,
        ),
        { encoding: "utf8", mode: 0o600 },
      );
    } catch {
      await writeFile(
        outputPath,
        JSON.stringify({ capturedAt: new Date().toISOString(), error: "quota fetch failed" }),
        { encoding: "utf8", mode: 0o600 },
      );
    }
  });
}

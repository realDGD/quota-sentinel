import { writeFile } from "node:fs/promises";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fetchAccountUsage } from "/Users/__USER__/.pi/agent/npm/node_modules/pi-antigravity/src/usage/usage.ts";

type Quota = { remainingPercent: number; resetAt: number };

function normalizeQuota(
  value: { remainingFraction?: number; resetTime?: string } | undefined,
): Quota | undefined {
  if (value?.remainingFraction === undefined || !value.resetTime) return undefined;
  const resetMillis = Date.parse(value.resetTime);
  if (!Number.isFinite(resetMillis)) return undefined;
  const remainingPercent = Math.round(
    Math.max(0, Math.min(1, value.remainingFraction)) * 100,
  );
  return { remainingPercent, resetAt: Math.floor(resetMillis / 1000) };
}

export default function captureAntigravityQuota(pi: ExtensionAPI) {
  pi.on("agent_end", async (_event, ctx) => {
    if (ctx.model?.provider !== "antigravity") return;
    const outputPath = process.env.PI_ANTIGRAVITY_QUOTA_FILE;
    if (!outputPath) return;

    try {
      const apiKey = await ctx.modelRegistry.getApiKeyForProvider("antigravity");
      if (!apiKey) throw new Error("Antigravity credentials unavailable");

      const usage = await fetchAccountUsage(apiKey);
      const group = usage.groups.find((item) => /gemini/i.test(item.displayName));
      const fiveHourBucket = group?.buckets.find(
        (item) => item.window === "5h" || /5h|five.hour/i.test(item.bucketId),
      );
      const weeklyBucket = group?.buckets.find(
        (item) => item.window === "weekly" || /weekly/i.test(item.bucketId),
      );
      const modelFallback = usage.models.find((item) =>
        /gemini-3\.7-flash/i.test(item.modelId),
      );

      await writeFile(
        outputPath,
        JSON.stringify(
          {
            capturedAt: new Date(usage.fetchedAt).toISOString(),
            fiveHour:
              normalizeQuota(fiveHourBucket) ?? normalizeQuota(modelFallback),
            weekly: normalizeQuota(weeklyBucket),
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

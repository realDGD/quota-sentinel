import { writeFile } from "node:fs/promises";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function captureCodexQuota(pi: ExtensionAPI) {
  pi.on("after_provider_response", async (event, ctx) => {
    if (ctx.model?.provider !== "openai-codex") return;

    const outputPath = process.env.PI_CODEX_QUOTA_FILE;
    if (!outputPath) return;

    const headers = Object.fromEntries(
      Object.entries(event.headers).filter(([name]) =>
        /^x-codex-(primary|secondary)-(used-percent|window-minutes|reset-at|reset-after-seconds)$/.test(
          name,
        ),
      ),
    );

    await writeFile(
      outputPath,
      JSON.stringify(
        {
          capturedAt: new Date().toISOString(),
          status: event.status,
          headers,
        },
        null,
        2,
      ),
      { encoding: "utf8", mode: 0o600 },
    );
  });
}

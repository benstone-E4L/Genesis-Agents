import { logger, task } from "@trigger.dev/sdk";

/**
 * Minimal placeholder task so the trigger.dev CLI (`dev`/`deploy`) has at
 * least one file under `src/trigger/` matching the glob configured in
 * `trigger.config.ts`. Replace or extend once real Genesis Agents jobs are
 * defined here.
 */
export const helloWorldTask = task({
  id: "hello-world",
  run: async (payload: { name?: string } = {}) => {
    logger.log("hello-world task running", { payload });
    return { message: `Hello, ${payload.name ?? "world"}!` };
  },
});

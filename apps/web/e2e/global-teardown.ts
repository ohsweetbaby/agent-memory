import { execFileSync } from "node:child_process";

import { PG_CONTAINER, REDIS_CONTAINER } from "./env";

/** 测试结束后移除独立测试容器，避免长期占用 55432/56379 端口。 */
export default function globalTeardown(): void {
  for (const name of [PG_CONTAINER, REDIS_CONTAINER]) {
    try {
      execFileSync("docker", ["rm", "-f", name], {
        stdio: "ignore",
        timeout: 60_000,
      });
    } catch {
      // 容器不存在或 Docker 已退出，无需让测试失败。
    }
  }
}

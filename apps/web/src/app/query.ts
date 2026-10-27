import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "../lib/api";

/** 4xx 不重试（无意义且会放大登录失败），5xx 最多重试一次。 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          return false;
        }
        return failureCount < 1;
      },
      refetchOnWindowFocus: false,
      staleTime: 15_000,
    },
  },
});

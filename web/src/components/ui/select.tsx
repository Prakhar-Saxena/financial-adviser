import type { SelectHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function Select({ className, ...p }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-9 rounded-md border border-line bg-surface px-2 text-sm text-ink " +
          "focus-visible:outline-2 focus-visible:outline-series-1",
        className,
      )}
      {...p}
    />
  );
}

// Copyright (C) 2026 Prakhar Saxena
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function Badge({ className, ...p }: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border border-line bg-surface-2 px-2 py-0.5 text-xs text-ink-2",
        className,
      )}
      {...p}
    />
  );
}

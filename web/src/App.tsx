import { useQuery } from "@tanstack/react-query";
import { ChartPie, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { cn, monthLabel } from "@/lib/utils";
import { Orders } from "@/pages/Orders";
import { Overview } from "@/pages/Overview";
import { Reconciliation } from "@/pages/Reconciliation";
import { Review } from "@/pages/Review";
import { Rules } from "@/pages/Rules";
import { Transactions } from "@/pages/Transactions";

function useTheme() {
  const [theme, setTheme] = useState<string>(() => {
    try { return localStorage.getItem("theme") ?? ""; } catch { return ""; }
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("theme", theme); } catch { /* private mode */ }
  }, [theme]);
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  return { dark, toggle: () => setTheme(dark ? "light" : "dark") };
}

export function App() {
  const { data: meta } = useQuery({ queryKey: ["meta"], queryFn: api.meta });
  const [month, setMonth] = useState<string>("");
  const { dark, toggle } = useTheme();
  useEffect(() => {
    if (meta && !month && meta.months.length) setMonth(meta.months[meta.months.length - 1]);
  }, [meta, month]);
  const link = ({ isActive }: { isActive: boolean }) =>
    cn("rounded-md px-3 py-1.5 text-sm", isActive ? "bg-surface-2 text-ink font-medium" : "text-ink-2 hover:text-ink");
  return (
    <div className="mx-auto max-w-7xl px-4 py-5 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <h1 className="mr-4 flex items-center gap-2 text-base font-semibold">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg text-white shadow-sm"
                style={{ background: "linear-gradient(135deg, var(--cat-1), var(--cat-7))" }}>
            <ChartPie className="h-4 w-4" />
          </span>
          Expenses
        </h1>
        <nav className="flex gap-1">
          <NavLink to="/" end className={link}>Overview</NavLink>
          <NavLink to="/transactions" className={link}>Transactions</NavLink>
          <NavLink to="/orders" className={link}>Amazon &amp; Costco</NavLink>
          <NavLink to="/review" className={link}>Review</NavLink>
          <NavLink to="/reconciliation" className={link}>Reconciliation</NavLink>
          <NavLink to="/rules" className={link}>Rules</NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-2">
          {meta && (
            <Select value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Month">
              {[...meta.months].reverse().map((m) => <option key={m} value={m}>{monthLabel(m)}</option>)}
            </Select>
          )}
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="Toggle dark mode">
            {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>
        </div>
      </header>
      {!meta || !month ? (
        <p className="text-sm text-muted">{meta && !meta.months.length ? "No data yet. Run fin sync, then fin process." : "Loading…"}</p>
      ) : (
        <Routes>
          <Route path="/" element={<Overview month={month} meta={meta} />} />
          <Route path="/transactions" element={<Transactions month={month} meta={meta} />} />
          <Route path="/orders" element={<Orders month={month} meta={meta} />} />
          <Route path="/review" element={<Review meta={meta} />} />
          <Route path="/reconciliation" element={<Reconciliation meta={meta} />} />
          <Route path="/rules" element={<Rules meta={meta} />} />
        </Routes>
      )}
    </div>
  );
}

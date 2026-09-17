"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { RuntimeGate } from "./RuntimeGate";
import { EmulatorRecovery } from "./EmulatorRecovery";
import { useAccountSelection } from "@/lib/AccountSelection";

const HISTORY = new Set(["/runs/", "/stats/", "/errors/", "/ledger/", "/account/"]);

export function AccountShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { accounts, selected, loading, error, choose } = useAccountSelection();
  const independent = pathname.startsWith("/fleet/") || pathname === "/guide/" || pathname === "/archives/";
  const history = HISTORY.has(pathname);
  const remote = selected?.running && selected.dashboard_url && typeof window !== "undefined"
    && new URL(selected.dashboard_url).origin !== window.location.origin;

  return <div className="flex min-w-0 flex-1 flex-col">
    <header className="flex flex-wrap items-center gap-3 border-b bg-card px-4 py-3" aria-label="Game account">
      <label htmlFor="game-account" className="text-sm font-semibold">Game account</label>
      <select id="game-account" value={selected?.key ?? ""} disabled={loading}
        onChange={event => choose(event.target.value)} className="min-w-44 max-w-full rounded-md border bg-background px-3 py-2 text-sm">
        <option value="">Choose an account</option>
        {accounts.filter(account => account.running).map(account => <option key={account.key} value={account.key}>{account.account_id} · {account.instance} · Running</option>)}
        {accounts.some(account => !account.running) && <optgroup label="Archives">
          {accounts.filter(account => !account.running).map(account => <option key={account.key} value={account.key}>{account.kind === "unattributed" ? `Unattributed history${account.instance ? ` · ${account.instance}` : ""}` : `${account.account_id} · ${account.instance}`}</option>)}
        </optgroup>}
      </select>
      {selected && <span className="text-xs text-muted-foreground">{selected.running ? "Running account" : selected.kind === "unattributed" ? "History without verified account identity" : "Registered account · no running bot"}</span>}
      <Link href="/fleet/?reroll=1" className="ml-auto text-sm text-primary underline">Reroll account</Link>
      <Link href="/archives/" className="text-sm text-primary underline">Archives</Link>
    </header>
    {error && <p role="alert" className="border-b px-4 py-2 text-sm text-danger">Could not refresh account list: {error}</p>}
    {independent ? <main className="min-w-0 flex-1 p-4">{children}</main>
      : loading ? <main className="p-6 text-sm">Loading accounts…</main>
      : !selected ? <main className="m-4 flex max-w-2xl flex-col gap-3 rounded-lg border p-6">
          <h1 className="text-lg font-semibold">No account selected</h1>
          <p className="text-sm text-muted-foreground">Choose a verified account above to view its data. Existing shared history remains in Unattributed archive. To create a new account, start a reroll.</p>
          <Link href="/fleet/?reroll=1" className="w-fit rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground">Start a reroll</Link>
          <EmulatorRecovery />
        </main>
      : remote ? <main className="m-4 flex max-w-2xl flex-col gap-3 rounded-lg border p-6">
          <h1 className="text-lg font-semibold">{selected.account_id} is running on {selected.instance}</h1>
          <p className="text-sm text-muted-foreground">Open this worker’s dashboard to see its Live data and controls. This dashboard serves a different bot process.</p>
          <a href={selected.dashboard_url!} className="w-fit rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground">Open worker dashboard</a>
        </main>
      : !selected.running && !history ? <main className="m-4 flex max-w-2xl flex-col gap-3 rounded-lg border p-6">
          <h1 className="text-lg font-semibold">No live bot for this account</h1>
          <p className="text-sm text-muted-foreground">This account is available in Archives. Its saved Runs, Stats, Errors, Ledger, and account evidence can be viewed, while live controls require a verified running worker.</p>
          <div className="flex gap-4 text-sm text-primary underline"><Link href="/runs/">View runs</Link><Link href="/stats/">View stats</Link></div>
          <EmulatorRecovery />
        </main>
      : selected.running ? <RuntimeGate><main key={selected.key} className="min-w-0 flex-1 p-4">{children}</main></RuntimeGate>
      : <main key={selected.key} className="min-w-0 flex-1 p-4">{children}</main>}
  </div>;
}

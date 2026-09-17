"use client";

import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { useAccountSelection } from "@/lib/AccountSelection";

export default function ArchivesPage() {
  const { accounts, choose } = useAccountSelection();
  const archived = accounts.filter(account => !account.running);
  return <div className="mx-auto flex max-w-5xl flex-col gap-4">
    <PageHeader title="Archives" meta={`${archived.length} saved ${archived.length === 1 ? "source" : "sources"}`} />
    <p className="max-w-3xl text-sm text-muted-foreground">History stays with the worker that recorded it. Older shared history has no verified account identity and appears separately as Unattributed.</p>
    {archived.length === 0 ? <SectionCard title="No archived accounts">
      <p className="text-sm">Create a reroll account from Fleet to start a new worker. Its history will appear here when the worker is no longer running.</p>
      <Link href="/fleet/reroll/" className="mt-3 inline-block text-sm text-primary underline">Start a reroll</Link>
    </SectionCard> : archived.map(account => <SectionCard key={account.key} title={account.kind === "unattributed" ? `Unattributed history${account.instance ? ` · ${account.instance}` : ""}` : `Tower account ${account.account_id}`}>
      <p className="mb-3 text-sm text-muted-foreground">{account.kind === "unattributed" ? "Account ownership was not recorded for this database" : `${account.instance} · registered worker · bot not running`}</p>
      <div className="flex gap-4 text-sm text-primary underline">
        <Link href="/runs/" onClick={() => choose(account.key)}>Runs</Link>
        <Link href="/stats/" onClick={() => choose(account.key)}>Stats</Link>
        <Link href="/account/" onClick={() => choose(account.key)}>Account evidence</Link>
      </div>
    </SectionCard>)}
  </div>;
}

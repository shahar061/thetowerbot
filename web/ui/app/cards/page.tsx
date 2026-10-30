"use client";
import { useAccountSelection } from "@/lib/AccountSelection";
import { AccountExportReport } from "@/components/game-export/GameExportReports";
import { CardsAccountClient } from "@/components/cards/CardsAccountClient";
export default function CardsPage() {
  const { selected, loading } = useAccountSelection();
  if (loading) return <p>Loading account…</p>;
  if (!selected) return <p>Select an account to view Cards.</p>;
  return (
    <main className="space-y-5"><AccountExportReport context={{ scope: selected.key, accountId: selected.account_id }} section="cards" />
    <CardsAccountClient
      key={selected.key}
      context={{
        scope: selected.key,
        accountId: selected.account_id,
        worker: selected.instance,
      }}
    /></main>
  );
}

"use client";

import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { LabListEntry, LabsReference, LabsRow } from "@/lib/labs";

export type LabListViewProps = {
  labs: BuildRouteDocument["baseline"]["labs"];
  reference: LabsReference | null;
  observed: LabsRow | null;
};

export function LabListView({ labs, reference, observed }: LabListViewProps): React.JSX.Element {
  const block = labs.blocks?.[0];
  const entries: LabListEntry[] = block?.type === "lab_list" ? block.entries : [];
  const name = (id: string): string => reference?.labs.find(lab => lab.id === id)?.name ?? id;
  const plan = observed?.plan ?? null;
  return <div className="min-w-0 space-y-4">
    <p role="note" className="text-xs text-muted-foreground">Ranked list: editing arrives with the planner. Slot pins keep a lab in its slot; every other slot takes the highest-ranked lab it can run, and runs a short, cheap filler while saving.</p>
    <table className="w-full min-w-0 table-fixed text-sm">
      <thead><tr><th className="w-10 text-left">#</th><th className="text-left">Lab</th><th className="w-14 text-left">To</th><th className="w-12 text-left">Tier</th><th className="w-16 text-left">Pin</th></tr></thead>
      <tbody>{entries.map((entry, index) => <tr key={entry.id}>
        <td>{index + 1}</td><td className="truncate">{name(entry.lab_id)}</td><td>{entry.to_level}</td>
        <td>{entry.tier}</td><td>{entry.pin_slot ? `Slot ${entry.pin_slot}` : ""}</td></tr>)}</tbody>
    </table>
    {plan && <section aria-label="Current picks" className="space-y-2">
      {plan.slots.map(slot => <div key={slot.slot} className="min-w-0 rounded-lg border border-border p-2 text-xs">
        <div className="font-semibold">Lab {slot.slot}</div>
        {slot.next ? <div>{slot.next.name} L{slot.next.level ?? "?"} · {slot.role === "filler" ? "filler" : "target"}
          {slot.saving_for && <span> · saving for {slot.saving_for.name} L{slot.saving_for.level ?? "?"}</span>}
          {slot.note && <span> · {slot.note}</span>}</div> : <div className="text-muted-foreground">{slot.why.at(-1) ?? "No pick"}</div>}
      </div>)}
      {plan.saving && <ul aria-label="Saving" className="list-disc pl-5 text-xs">{plan.saving.why.map(line => <li key={line}>{line}</li>)}</ul>}
    </section>}
  </div>;
}

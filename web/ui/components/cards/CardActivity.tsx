"use client";
import Link from "next/link";
import { SectionCard } from "@/components/ui/section-card";
import { Button } from "@/components/ui/button";
import type { CardOperation } from "@/lib/cards";
export function CardActivity({
  operations,
  disabled = false,
  onCancel,
  onInspectEvidence,
  inspectedOperation,
}: {
  operations: CardOperation[];
  disabled?: boolean;
  onCancel: (id: string) => void;
  onInspectEvidence?: (id: string) => void;
  inspectedOperation?: CardOperation | null;
}) {
  return (
    <SectionCard
      title="Cards activity"
      action={
        <Link href="/ledger/" className="text-sm underline">
          Open ledger
        </Link>
      }
    >
      {operations.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No recorded Cards operations.
        </p>
      ) : (
        <ul className="space-y-3">
          {operations.map((operation) => (
            <li
              key={operation.operation_id}
              className="rounded-md border p-3 text-sm"
            >
              <div className="flex flex-wrap items-center gap-2">
                <strong>
                  {operation.command.kind} · {operation.status}
                </strong>
                <span className="text-muted-foreground">
                  {new Date(operation.created_at * 1000).toLocaleString()}
                </span>
                {[
                  "queued",
                  "preflight",
                  "dispatched",
                  "verifying",
                  "reconciliation_required",
                ].includes(operation.status) ? (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={disabled}
                    onClick={() => onCancel(operation.operation_id)}
                  >
                    Cancel operation
                  </Button>
                ) : null}
              </div>
              {operation.reason ? (
                <p>{operation.reason}</p>
              ) : ["queued", "preflight", "dispatched", "verifying"].includes(
                  operation.status,
                ) ? (
                <p>Awaiting worker processing</p>
              ) : null}
              {onInspectEvidence ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => onInspectEvidence(operation.operation_id)}
                >
                  View recorded evidence
                </Button>
              ) : null}
              {["buy", "slot"].includes(operation.command.kind) ? (
                <p>
                  Gem spend:{" "}
                  {operation.spent_gems ??
                    (["dispatched", "verifying", "reconciliation_required"].includes(operation.status)
                      ? "Unknown / not recorded — pending reconciliation"
                      : "Unknown / not recorded")}
                </p>
              ) : (
                <p>No purchase debit</p>
              )}
              {operation.rewards.map((reward) => (
                <p key={reward.position}>
                  {reward.card_id} ×{reward.quantity}
                  {reward.level_after != null
                    ? ` · Level ${reward.level_after}`
                    : ""}
                </p>
              ))}
              {operation.snapshot_after?.capacity != null ? (
                <p>Slots after: {operation.snapshot_after.capacity}</p>
              ) : null}
              {operation.command.kind === "apply" ||
              operation.command.kind === "clear" ? (
                <p>
                  Equipment after:{" "}
                  {operation.snapshot_after?.equipment_complete &&
                  operation.snapshot_after.equipped
                    ? operation.snapshot_after.equipped.join(", ") ||
                      "None (verified clear)"
                    : "Not verified"}
                </p>
              ) : null}
              <span className="break-all text-xs text-muted-foreground">
                {operation.operation_id}
              </span>
            </li>
          ))}
        </ul>
      )}
      {inspectedOperation ? (
        <section
          aria-label="Recorded operation evidence"
          className="space-y-2 rounded-md border p-3 text-sm"
        >
          <h3 className="font-semibold">
            Evidence for {inspectedOperation.operation_id}
          </h3>
          <p>
            Immutable recorded observations; these do not imply current account
            authority.
          </p>
          {(
            [
              ["Before", inspectedOperation.snapshot_before],
              ["After", inspectedOperation.snapshot_after],
            ] as const
          ).map(([label, snapshot]) => (
            <div key={label}>
              <h4>{label}</h4>
              {snapshot ? (
                <>
                  <p>
                    Observed:{" "}
                    <time
                      dateTime={new Date(
                        snapshot.observed_at * 1000,
                      ).toISOString()}
                    >
                      {new Date(snapshot.observed_at * 1000).toLocaleString()}
                    </time>
                  </p>
                  <p className="break-all">
                    Frame reference: {snapshot.frame_digest ?? "Unavailable"}
                  </p>
                  {snapshot.equipment_evidence ? (
                    <p className="break-all">
                      Equipment evidence:{" "}
                      {snapshot.equipment_evidence.evidence_ref}
                    </p>
                  ) : null}
                  {snapshot.items?.map((item) => (
                    <p className="break-all" key={item.card_id}>
                      {item.card_id}:{" "}
                      {item.evidence_ref ?? "No evidence reference"}
                    </p>
                  ))}
                </>
              ) : (
                <p>No recorded snapshot.</p>
              )}
            </div>
          ))}
          <p className="text-xs text-muted-foreground">
            Evidence references identify frames and tiles. No persisted image
            URL is available.
          </p>
        </section>
      ) : null}
    </SectionCard>
  );
}

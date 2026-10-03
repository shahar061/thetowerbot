"use client";
import { useState } from "react";
import Link from "next/link";
import { PageHeader } from "@/components/PageHeader";
import { SectionCard } from "@/components/ui/section-card";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { NumberField } from "@/components/ui/number-field";
import { CardCollection } from "./CardCollection";
import { CardPlanEditor } from "./CardPlanEditor";
import { CardLoadoutEditor } from "./CardLoadoutEditor";
import { CardActivity } from "./CardActivity";
import {
  safeInteger,
  cardDraftError,
  type CardCatalog,
  type CardClientContext,
  type CardCommandIntent,
  type CardPreview,
  type CardProgram,
  type CardsProjection,
} from "@/lib/cards";
import type { CardPolicy } from "@/lib/types";

export interface CardsWorkspaceProps {
  context: CardClientContext;
  data: CardsProjection;
  catalog: CardCatalog;
  saved: CardProgram | null;
  draft: CardProgram | null;
  policy: CardPolicy | null;
  pending: boolean;
  dirty?: boolean;
  preview?: CardPreview | null;
  error?: string | null;
  retryAvailable?: boolean;
  editDisabledReason?: string | null;
  applyDisabledReason?: string | null;
  reloadLabel?: string;
  inspectedOperation?: import("@/lib/cards").CardOperation | null;
  onInspectEvidence?: (id: string) => void;
  onChange: (program: CardProgram) => void;
  onPolicyChange: (policy: CardPolicy) => void;
  onSave: () => void;
  onRevert: () => void;
  automationEnabled?: boolean;
  onAutomation?: (enabled: boolean) => void;
  onCommand: (intent: CardCommandIntent) => void;
  onStartCycle: (cap: number) => void;
  onRetry?: () => void;
  onDiscardRetry?: () => void;
}
export function CardsWorkspace({
  automationEnabled,
  onAutomation,
  context,
  data,
  catalog,
  saved,
  draft,
  policy,
  pending,
  dirty,
  preview,
  error,
  retryAvailable,
  editDisabledReason,
  applyDisabledReason,
  reloadLabel = "Revert / reload",
  inspectedOperation,
  onInspectEvidence,
  onChange,
  onPolicyChange,
  onSave,
  onRevert,
  onCommand,
  onStartCycle,
  onRetry,
  onDiscardRetry,
}: CardsWorkspaceProps) {
  const [confirmClear, setConfirmClear] = useState(false);
  const [confirmCycle, setConfirmCycle] = useState(false);
  const [cycleCap, setCycleCap] = useState(0);
  const [paidGoal, setPaidGoal] = useState("");
  const readOnly =
    data.read_only_reason ||
    (!data.preconditions ? "Current account identity unavailable" : null);
  const editReason =
    editDisabledReason ||
    (data.config_owner === "fleet"
      ? "This account uses a fleet-owned Cards program."
      : data.config_owner !== "local"
        ? "Configuration owner unavailable"
        : null) ||
    readOnly;
  const locked = pending || !!retryAvailable;
  const editDisabled = locked || !!editReason;
  const capabilityReason = (
    key: "inventory" | "buy_one" | "buy_ten" | "buy_slot" | "assign",
    paid = false,
  ): string | null =>
    data.capabilities[key]
      ? readOnly ||
        (paid && !data.active_budget
          ? "Start an explicit budget cycle first"
          : null)
      : (data.capabilities.reasons[key] ??
        "Unsupported by current verified layout");
  const action = (
    label: string,
    intent: CardCommandIntent,
    reason: string | null,
  ): React.JSX.Element => (
    <div className="space-y-1">
      <Button
        variant="outline"
        disabled={locked || !!reason}
        onClick={() => onCommand(intent)}
      >
        {label}
      </Button>
      {reason ? (
        <p className="max-w-60 text-xs text-muted-foreground">{reason}</p>
      ) : null}
    </div>
  );
  const draftError = draft ? cardDraftError(draft, policy) : null;
  const hasChanges = dirty ?? JSON.stringify(saved) !== JSON.stringify(draft);
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <PageHeader
        title="Cards"
        meta={[context.accountId ?? "Unknown account", context.worker]
          .filter(Boolean)
          .join(" · ")}
      />
      {readOnly ? (
        <p role="status" className="rounded-md border p-3 text-sm">
          Read only: {readOnly}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="rounded-md border border-danger p-3 text-sm">
          {error}
        </p>
      ) : null}
      {retryAvailable ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
          <p className="text-sm">
            Request outcome unknown. Retry sends the exact original account,
            key, and preconditions.
          </p>
          <Button disabled={pending} onClick={onRetry}>
            Retry original request
          </Button>
          <Button variant="outline" disabled={pending} onClick={onDiscardRetry}>
            Dismiss retry
          </Button>
        </div>
      ) : null}
      <SectionCard title="Execution & budget">
        <p className="text-sm">
          Worker decision: {data.decision.kind} ·{" "}
          {data.decision.reason ?? "No pending reason"}
        </p>
        {onAutomation && automationEnabled !== undefined ? (
          <Switch
            label="Automatic Cards purchases"
            checked={automationEnabled}
            disabled={locked || !!readOnly}
            onCheckedChange={onAutomation}
          />
        ) : null}
        <p className="text-sm">
          Automatic purchases: {data.policy?.enabled ? "Enabled" : "Disabled"} ·
          Saving a plan does not enable automation.{" "}
          <Link href="/strategy/#shopping" className="underline">
            Shopping controls
          </Link>
        </p>
        {data.policy ? (
          <p className="text-xs text-muted-foreground">
            Effective reserve: {data.policy.gem_floor} gems · Effective visit
            limit: {data.policy.max_per_visit}. Parent shopping and fleet rules
            may tighten these settings.
          </p>
        ) : null}
        {data.active_budget ? (
          <p className="text-sm">
            Current cycle {data.active_budget.cycle_id}: cap{" "}
            {data.active_budget.cap.toLocaleString()} gems · spent{" "}
            {data.active_budget.spent.toLocaleString()} · pending{" "}
            {data.active_budget.pending.toLocaleString()}. Plan edits cannot
            enlarge this cycle.
          </p>
        ) : (
          <p className="text-sm">
            No active budget cycle. Refreshing observations never starts one.
          </p>
        )}
        {policy ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm">
              Batch preference{" "}
              <select
                aria-label="Batch preference"
                value={policy.batch}
                disabled={editDisabled}
                className="ml-2 rounded-md border bg-background p-2"
                onChange={(event) =>
                  onPolicyChange({ ...policy, batch: event.target.value === "x10" ? "x10" : "x1" })
                }
              >
                <option value="x1">x1</option>
                <option value="x10">x10</option>
              </select>
              <span className="block text-xs text-muted-foreground">
                Stored shopping preference. Automatic goal buying always uses
                x1; unsupported x10 is never downgraded.
              </span>
            </label>
            <NumberField
              step={1}
              label="Gem reserve"
              value={policy.gem_floor}
              min={0}
              max={Number.MAX_SAFE_INTEGER}
              disabled={editDisabled}
              onCommit={(value) => {
                if (safeInteger(value))
                  onPolicyChange({ ...policy, gem_floor: value });
              }}
              note="Existing shopping.cards reserve; effective fleet reserve may be higher."
            />
            <NumberField
              step={1}
              label="Max cards per visit"
              value={policy.max_per_visit}
              min={1}
              max={50}
              disabled={editDisabled}
              onCommit={(value) => {
                if (safeInteger(value, 1, 50))
                  onPolicyChange({ ...policy, max_per_visit: value });
              }}
            />
          </div>
        ) : null}
        <div className="flex flex-wrap items-end gap-3">
          <NumberField
            step={1}
            label="New cycle gem cap"
            value={cycleCap}
            min={0}
            max={Number.MAX_SAFE_INTEGER}
            disabled={locked || !!readOnly}
            onCommit={(value) => {
              if (safeInteger(value)) setCycleCap(value);
            }}
          />
          <Button
            variant="outline"
            disabled={locked || !!readOnly}
            onClick={() => setConfirmCycle(true)}
          >
            Start budget cycle
          </Button>
        </div>
        {confirmCycle ? (
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm">
              Start a new finite allowance of {cycleCap} gems? This does not
              enable or arm shopping.
            </p>
            <Button
              disabled={locked || !!readOnly}
              onClick={() => {
                setConfirmCycle(false);
                onStartCycle(cycleCap);
              }}
            >
              Confirm new cycle
            </Button>
            <Button variant="ghost" onClick={() => setConfirmCycle(false)}>
              Cancel
            </Button>
          </div>
        ) : null}
      </SectionCard>
      <SectionCard title="Manual actions">
        <p className="text-sm text-muted-foreground">
          Actions queue for the verified worker. Queued is not confirmed. All
          spending uses the existing reserve, journal, and budget gates.
        </p>
        <label className="text-sm">
          Purchase goal{" "}
          <select
            aria-label="Manual purchase goal"
            className="ml-2 rounded-md border bg-background p-2"
            value={paidGoal}
            disabled={locked || !!readOnly}
            onChange={(event) => setPaidGoal(event.target.value)}
          >
            <option value="">Current route goal / untargeted standalone</option>
            {data.program?.goals.map((goal) => (
              <option key={goal.id} value={goal.id}>
                {goal.kind}: {goal.id}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-wrap gap-3">
          {action(
            "Refresh observations",
            { kind: "refresh" },
            capabilityReason("inventory"),
          )}
          {action(
            "Buy x1",
            {
              kind: "buy",
              quantity: 1,
              ...(paidGoal ? { goal_id: paidGoal } : {}),
            },
            capabilityReason("buy_one", true) ||
              (paidGoal &&
              data.program?.goals.find((goal) => goal.id === paidGoal)?.kind !==
                "acquire"
                ? "Choose an acquire goal"
                : null),
          )}
          {action(
            "Buy x10",
            {
              kind: "buy",
              quantity: 10,
              ...(paidGoal ? { goal_id: paidGoal } : {}),
            },
            capabilityReason("buy_ten", true) ||
              (paidGoal &&
              data.program?.goals.find((goal) => goal.id === paidGoal)?.kind !==
                "acquire"
                ? "Choose an acquire goal"
                : null),
          )}
          {action(
            "Buy slot",
            { kind: "slot", ...(paidGoal ? { goal_id: paidGoal } : {}) },
            capabilityReason("buy_slot", true) ||
              (paidGoal &&
              data.program?.goals.find((goal) => goal.id === paidGoal)?.kind !==
                "slots"
                ? "Choose a slot goal"
                : null),
          )}
          {action(
            "Apply saved loadout",
            {
              kind: "apply",
              loadout_id: data.program?.selected_loadout_id ?? undefined,
            },
            capabilityReason("assign") ||
              applyDisabledReason ||
              (!data.program?.selected_loadout_id
                ? "Select and save a loadout first"
                : hasChanges
                  ? "Save or revert the draft before applying"
                  : null),
          )}
          <div>
            <Button
              variant="outline"
              disabled={locked || !!capabilityReason("assign")}
              onClick={() => setConfirmClear(true)}
            >
              Clear equipment
            </Button>
            {capabilityReason("assign") ? (
              <p className="max-w-60 text-xs text-muted-foreground">
                {capabilityReason("assign")}
              </p>
            ) : null}
          </div>
        </div>
        {confirmClear ? (
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm">
              Explicitly remove all equipped cards between runs?
            </p>
            <Button
              disabled={locked || !!capabilityReason("assign")}
              onClick={() => {
                setConfirmClear(false);
                onCommand({ kind: "clear" });
              }}
            >
              Confirm clear equipment
            </Button>
            <Button variant="ghost" onClick={() => setConfirmClear(false)}>
              Keep equipment
            </Button>
          </div>
        ) : null}
      </SectionCard>
      <CardCollection
        targetedIds={
          draft?.goals.flatMap((goal) =>
            goal.kind === "acquire"
              ? goal.targets.map((target) => target.card_id)
              : [],
          ) ?? []
        }
        observedAt={data.snapshot?.observed_at}
        items={data.snapshot?.items ?? []}
        catalog={catalog}
        collectionComplete={data.snapshot?.collection_complete ?? false}
        equipmentComplete={data.snapshot?.equipment_complete ?? false}
        capacity={data.snapshot?.capacity}
        equipped={data.snapshot?.equipped}
        fresh={data.fresh}
      />
      {editReason ? (
        <div className="rounded-md border p-3 text-sm">
          <p>{editReason}</p>
          {data.config_owner === "fleet" ? (
            <Link href="/fleet/reroll/strategies/" className="underline">
              Edit in Fleet / Strategy Library
            </Link>
          ) : null}
        </div>
      ) : null}
      {draftError ? (
        <p role="alert" className="text-sm text-danger">
          {draftError}
        </p>
      ) : null}
      {draft ? (
        <>
          <div className="sticky top-0 z-10 flex items-center gap-2 rounded-md border bg-background p-3">
            <Button
              disabled={editDisabled || !hasChanges || !!draftError}
              onClick={onSave}
            >
              Save Cards
            </Button>
            <Button variant="outline" disabled={locked} onClick={onRevert}>
              {reloadLabel}
            </Button>
            <span className="text-xs text-muted-foreground">
              {hasChanges ? "Unsaved changes" : "Saved configuration"}
            </span>
          </div>
          <CardPlanEditor
            program={draft}
            catalog={catalog}
            preview={preview}
            disabled={editDisabled}
            onChange={onChange}
          />
          <CardLoadoutEditor
            program={draft}
            catalog={catalog}
            preview={preview}
            disabled={editDisabled}
            onChange={onChange}
          />
        </>
      ) : null}
      <CardActivity
        operations={data.recent_operations}
        inspectedOperation={inspectedOperation}
        onInspectEvidence={onInspectEvidence}
        disabled={locked || !!readOnly}
        onCancel={(id) =>
          onCommand({ kind: "cancel", target_operation_id: id })
        }
      />
    </div>
  );
}

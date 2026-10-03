"use client";
import { SectionCard } from "@/components/ui/section-card";
import { Button } from "@/components/ui/button";
import { NumberField } from "@/components/ui/number-field";
import { Switch } from "@/components/ui/switch";
import {
  freshCommandKey,
  safeInteger,
  type CardCatalog,
  type CardGoal,
  type CardPreview,
  type CardProgram,
} from "@/lib/cards";

export function CardPlanEditor({
  program,
  catalog,
  preview,
  disabled = false,
  onChange,
}: {
  program: CardProgram;
  catalog: CardCatalog;
  preview?: CardPreview | null;
  disabled?: boolean;
  onChange: (program: CardProgram) => void;
}) {
  const patch = (index: number, goal: CardGoal): void =>
    onChange({
      ...program,
      goals: program.goals.map((value, i) => (i === index ? goal : value)),
    });
  const move = (index: number, delta: number): void => {
    const goals = [...program.goals];
    [goals[index], goals[index + delta]] = [goals[index + delta], goals[index]];
    onChange({ ...program, goals });
  };
  return (
    <SectionCard title="Plan goals">
      <p className="text-sm text-muted-foreground">
        Card purchases are random. Targets are stopping conditions, not cards
        you can buy directly. Automatic goal buying uses x1 and checks progress
        after each confirmed purchase.
      </p>
      <NumberField
        step={1}
        label="Plan gem cap"
        value={program.gem_cap}
        min={0}
        max={Number.MAX_SAFE_INTEGER}
        disabled={disabled}
        onCommit={(value) => {
          if (safeInteger(value)) onChange({ ...program, gem_cap: value });
        }}
      />
      {program.goals.map((goal, index) => {
        const result = preview?.goals.find((item) => item.id === goal.id);
        return (
          <div className="space-y-3 rounded-md border p-3" key={goal.id}>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">
                {index + 1}.{" "}
                {goal.kind === "acquire" ? "Acquire cards" : "Unlock slots"}
              </h3>
              <span className="text-xs text-muted-foreground">
                {result?.met === true
                  ? "Met"
                  : result?.met === false
                    ? "Not met"
                    : "Unknown — refresh needed"}
              </span>
              <Button
                variant="outline"
                size="sm"
                aria-label={`Move goal ${index + 1} up`}
                disabled={disabled || index === 0}
                onClick={() => move(index, -1)}
              >
                ↑
              </Button>
              <Button
                variant="outline"
                size="sm"
                aria-label={`Move goal ${index + 1} down`}
                disabled={disabled || index === program.goals.length - 1}
                onClick={() => move(index, 1)}
              >
                ↓
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={disabled}
                onClick={() =>
                  onChange({
                    ...program,
                    goals: program.goals.filter((_, i) => i !== index),
                  })
                }
              >
                Remove goal
              </Button>
            </div>
            {goal.kind === "slots" ? (
              <>
                <NumberField
                  step={1}
                  label={`Goal ${index + 1} slot capacity`}
                  min={1}
                  max={catalog.max_gem_slots}
                  value={goal.capacity}
                  disabled={disabled}
                  onCommit={(value) => {
                    if (safeInteger(value, 1, catalog.max_gem_slots))
                      patch(index, { ...goal, capacity: value });
                  }}
                />
                <label className="flex items-center gap-2 text-sm">
                  <Switch
                    label="Only when another usable card exists"
                    checked={goal.when_usable_card}
                    disabled={disabled}
                    onCheckedChange={(value) =>
                      patch(index, { ...goal, when_usable_card: value })
                    }
                  />
                  <span>Only when another usable card exists</span>
                </label>
              </>
            ) : (
              <>
                {goal.targets.map((target, targetIndex) => (
                  <div
                    className="flex flex-wrap items-center gap-2"
                    key={target.card_id}
                  >
                    <span className="text-sm">
                      {catalog.cards.find(
                        (card) => card.card_id === target.card_id,
                      )?.name ?? target.card_id}
                    </span>
                    <select
                      aria-label={`Target level for ${target.card_id}`}
                      className="rounded-md border bg-background p-2 text-sm"
                      value={target.min_level ?? ""}
                      disabled={disabled}
                      onChange={(event) =>
                        patch(index, {
                          ...goal,
                          targets: goal.targets.map((item, i) =>
                            i === targetIndex
                              ? {
                                  ...item,
                                  min_level:
                                    event.target.value === ""
                                      ? null
                                      : Number(event.target.value),
                                }
                              : item,
                          ),
                        })
                      }
                    >
                      <option value="">Unlock</option>
                      {Array.from(
                        {
                          length:
                            catalog.cards.find(
                              (card) => card.card_id === target.card_id,
                            )?.max_level ?? 0,
                        },
                        (_, i) => (
                          <option key={i + 1} value={i + 1}>
                            Level {i + 1}
                          </option>
                        ),
                      )}
                    </select>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={disabled || goal.targets.length === 1}
                      onClick={() =>
                        patch(index, {
                          ...goal,
                          targets: goal.targets.filter(
                            (_, i) => i !== targetIndex,
                          ),
                        })
                      }
                    >
                      Remove target
                    </Button>
                  </div>
                ))}
                <select
                  aria-label={`Add target to goal ${index + 1}`}
                  className="rounded-md border bg-background p-2 text-sm"
                  value=""
                  disabled={disabled || goal.targets.length >= 20}
                  onChange={(event) => {
                    if (event.target.value)
                      patch(index, {
                        ...goal,
                        targets: [
                          ...goal.targets,
                          { card_id: event.target.value, min_level: null },
                        ],
                      });
                  }}
                >
                  <option value="">Add target…</option>
                  {catalog.cards
                    .filter(
                      (card) =>
                        !goal.targets.some(
                          (target) => target.card_id === card.card_id,
                        ),
                    )
                    .map((card) => (
                      <option key={card.card_id} value={card.card_id}>
                        {card.name}
                      </option>
                    ))}
                </select>
              </>
            )}
          </div>
        );
      })}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={
            disabled || program.goals.length >= 20 || !catalog.cards.length
          }
          onClick={() =>
            onChange({
              ...program,
              goals: [
                ...program.goals,
                {
                  id: freshCommandKey(),
                  kind: "acquire",
                  targets: [
                    { card_id: catalog.cards[0].card_id, min_level: null },
                  ],
                },
              ],
            })
          }
        >
          Add card goal
        </Button>
        <Button
          variant="outline"
          disabled={disabled || program.goals.length >= 20}
          onClick={() =>
            onChange({
              ...program,
              goals: [
                ...program.goals,
                {
                  id: freshCommandKey(),
                  kind: "slots",
                  capacity: 1,
                  when_usable_card: true,
                },
              ],
            })
          }
        >
          Add slot goal
        </Button>
      </div>
    </SectionCard>
  );
}

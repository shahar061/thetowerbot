"use client";
import { SectionCard } from "@/components/ui/section-card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  freshCommandKey,
  type CardCatalog,
  type CardLoadout,
  type CardPreview,
  type CardProgram,
} from "@/lib/cards";
export function CardLoadoutEditor({
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
  const patch = (loadout: CardLoadout): void =>
    onChange({
      ...program,
      loadouts: program.loadouts.map((item) =>
        item.id === loadout.id ? loadout : item,
      ),
    });
  const name = (id: string): string =>
    catalog.cards.find((card) => card.card_id === id)?.name ?? id;
  return (
    <SectionCard title="Named loadouts">
      <p className="text-sm text-muted-foreground">
        Bot-managed configurations, not in-game presets. Priority order fills
        available slots from owned cards, with lower entries as fallbacks.
        Equipment is applied only between runs. An empty loadout never clears
        equipment.
      </p>
      <label className="text-sm">
        Selected loadout{" "}
        <select
          aria-label="Selected loadout"
          className="ml-2 rounded-md border bg-background p-2"
          disabled={disabled}
          value={program.selected_loadout_id ?? ""}
          onChange={(event) =>
            onChange({
              ...program,
              selected_loadout_id: event.target.value || null,
            })
          }
        >
          <option value="">None</option>
          {program.loadouts.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
      {program.loadouts.map((loadout) => {
        const result = preview?.loadouts.find((item) => item.id === loadout.id);
        const resolution = result?.resolution;
        return (
          <div key={loadout.id} className="space-y-3 rounded-md border p-3">
            <Input
              aria-label={`Loadout name ${loadout.id}`}
              maxLength={60}
              disabled={disabled}
              value={loadout.name}
              onChange={(event) =>
                patch({ ...loadout, name: event.target.value })
              }
            />
            <ol className="space-y-2">
              {loadout.priority.map((id, index) => (
                <li key={id} className="flex items-center gap-2 text-sm">
                  <span className="mr-auto">
                    {index + 1}. {name(id)}
                  </span>
                  {([-1, 1] as const).map((delta) => (
                    <Button
                      key={delta}
                      size="sm"
                      variant="outline"
                      aria-label={`Move ${name(id)} ${delta === -1 ? "up" : "down"}`}
                      disabled={
                        disabled ||
                        index + delta < 0 ||
                        index + delta >= loadout.priority.length
                      }
                      onClick={() => {
                        const priority = [...loadout.priority];
                        [priority[index], priority[index + delta]] = [
                          priority[index + delta],
                          priority[index],
                        ];
                        patch({ ...loadout, priority });
                      }}
                    >
                      {delta === -1 ? "↑" : "↓"}
                    </Button>
                  ))}
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={disabled}
                    aria-label={`Remove ${name(id)} from loadout`}
                    onClick={() =>
                      patch({
                        ...loadout,
                        priority: loadout.priority.filter(
                          (value) => value !== id,
                        ),
                      })
                    }
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ol>
            <select
              aria-label={`Add card to ${loadout.name}`}
              className="rounded-md border bg-background p-2 text-sm"
              disabled={disabled}
              value=""
              onChange={(event) => {
                if (event.target.value)
                  patch({
                    ...loadout,
                    priority: [...loadout.priority, event.target.value],
                  });
              }}
            >
              <option value="">Add priority card…</option>
              {catalog.cards
                .filter((card) => !loadout.priority.includes(card.card_id))
                .map((card) => (
                  <option key={card.card_id} value={card.card_id}>
                    {card.name}
                  </option>
                ))}
            </select>
            <p className="text-sm">
              Server preview:{" "}
              {resolution?.desired?.map(name).join(", ") ||
                resolution?.reason ||
                "Awaiting preview"}
            </p>
            <p className="text-sm">
              Observed capacity: {result?.capacity ?? "Unknown"}
            </p>
            {result?.capacity_limited === true ? (
              <p className="text-sm">
                Insufficient slots for every eligible priority card. Deferred by
                capacity: {result.capacity_excluded?.map(name).join(", ")}
              </p>
            ) : result?.capacity_limited === false ? (
              <p className="text-xs text-muted-foreground">
                All observed eligible priority cards fit.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Capacity coverage unknown; refresh incomplete observations.
              </p>
            )}
            <p className="text-sm">
              Observed equipment:{" "}
              {result?.observed_equipped?.map(name).join(", ") ?? "Unknown"}
              {result?.observed_equipped?.length === 0 ? "None" : ""}
            </p>
            {result?.additions != null && result.removals != null ? (
              <p className="text-sm">
                Equipment changes — Add:{" "}
                {result.additions.map(name).join(", ") || "None"} · Remove:{" "}
                {result.removals.map(name).join(", ") || "None"}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Equipment changes unknown until both the desired set and
                observed equipment are verified.
              </p>
            )}
            {resolution?.missing.length ? (
              <p className="text-xs text-muted-foreground">
                Skipped: {resolution.missing.map(name).join(", ")}
              </p>
            ) : null}
            <Button
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() =>
                onChange({
                  ...program,
                  loadouts: program.loadouts.filter(
                    (item) => item.id !== loadout.id,
                  ),
                  selected_loadout_id:
                    program.selected_loadout_id === loadout.id
                      ? null
                      : program.selected_loadout_id,
                })
              }
            >
              Remove loadout
            </Button>
          </div>
        );
      })}
      <Button
        variant="outline"
        disabled={disabled || program.loadouts.length >= 20}
        onClick={() =>
          onChange({
            ...program,
            loadouts: [
              ...program.loadouts,
              {
                id: freshCommandKey(),
                name: `Loadout ${program.loadouts.length + 1}`,
                priority: [],
              },
            ],
          })
        }
      >
        Add loadout
      </Button>
    </SectionCard>
  );
}

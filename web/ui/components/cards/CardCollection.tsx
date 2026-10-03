"use client";
import { useState } from "react";
import { SectionCard } from "@/components/ui/section-card";
import { Input } from "@/components/ui/input";
import type { CardCatalog, CardItem } from "@/lib/cards";

export function CardCollection({
  items,
  collectionComplete,
  catalog,
  capacity = null,
  equipped = null,
  equipmentComplete = false,
  fresh = false,
  observedAt,
  targetedIds = [],
}: {
  items: CardItem[];
  collectionComplete: boolean;
  catalog?: CardCatalog;
  capacity?: number | null;
  equipped?: string[] | null;
  equipmentComplete?: boolean;
  fresh?: boolean;
  observedAt?: number | null;
  targetedIds?: readonly string[];
}) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const targets = new Set(targetedIds);
  const known = new Map(items.map((item) => [item.card_id, item]));
  const rows =
    catalog?.cards.map((card) => ({ card, item: known.get(card.card_id) })) ??
    items.map((item) => ({
      card: { card_id: item.card_id, name: item.card_id },
      item,
    }));
  const visible = rows.filter(
    ({ card, item }) =>
      card.name.toLowerCase().includes(search.toLowerCase()) &&
      (filter === "all" ||
        (filter === "targeted"
          ? targets.has(card.card_id)
          : filter === "equipped"
            ? item?.equipped === true
            : (item?.ownership ?? "unknown") === filter)),
  );
  return (
    <SectionCard title="Collection">
      <p className="text-sm">
        <span>
          {collectionComplete
            ? "Collection observed"
            : "Collection not fully observed"}
        </span>{" "}
        · {fresh ? "Current evidence" : "Historical or unknown evidence"}
      </p>
      <p className="text-sm text-muted-foreground">
        Slots: {capacity ?? "Unknown"} · Equipped:{" "}
        {equipmentComplete && equipped
          ? equipped.length
          : "Unknown / incomplete"}
      </p>
      <p className="text-xs text-muted-foreground">
        Collection observation:{" "}
        {observedAt != null ? (
          <time dateTime={new Date(observedAt * 1000).toISOString()}>
            {new Date(observedAt * 1000).toLocaleString()}
          </time>
        ) : (
          "Unknown"
        )}
      </p>
      <div className="flex flex-wrap gap-2">
        <Input
          aria-label="Search cards"
          placeholder="Search cards"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="Collection filter"
          className="rounded-md border bg-background p-2"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        >
          {[
            "all",
            "owned",
            "unowned",
            "unknown",
            "locked",
            "unavailable",
            "equipped",
            "targeted",
          ].map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {visible.map(({ card, item }) => (
          <article className="rounded-md border p-3 text-sm" key={card.card_id}>
            <h3 className="font-semibold">{card.name}</h3>
            <p>
              {item?.ownership ?? "unknown"}
              {item?.equipped === true ? " · Equipped" : ""}
            </p>
            <p className="text-muted-foreground">
              Level {item?.level ?? "?"} · Copies {item?.copies ?? "?"} /{" "}
              {item?.copies_needed ?? "?"}
              {item?.maxed === true ? " · Maxed" : ""}
            </p>
            <p className="text-xs text-muted-foreground">
              Tile observed:{" "}
              {item?.observed_at != null ? (
                <time
                  dateTime={new Date(item.observed_at * 1000).toISOString()}
                >
                  {new Date(item.observed_at * 1000).toLocaleString()}
                </time>
              ) : (
                "Unknown"
              )}
            </p>
            {item?.field_evidence && Object.keys(item.field_evidence).length ? (
              <details className="text-xs text-muted-foreground">
                <summary>Field observation times</summary>
                {Object.entries(item.field_evidence).map(
                  ([field, evidence]) => (
                    <p key={field}>
                      {field}:{" "}
                      <time
                        dateTime={new Date(
                          evidence.observed_at * 1000,
                        ).toISOString()}
                      >
                        {new Date(evidence.observed_at * 1000).toLocaleString()}
                      </time>
                    </p>
                  ),
                )}
              </details>
            ) : null}
          </article>
        ))}
      </div>
      {visible.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No matching observed cards.
        </p>
      ) : null}
    </SectionCard>
  );
}

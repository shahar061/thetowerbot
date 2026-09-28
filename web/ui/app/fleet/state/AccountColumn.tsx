"use client";

import { memo, useState } from "react";
import { cn } from "@/lib/utils";
import {
  STATE_CATEGORIES, type FleetStateAccount, type FleetStateBestWave, type FleetStateCards,
  type FleetStateDecision, type FleetStateLabs, type FleetStateNextBuy, type FleetStateWorkshop,
  type StateCategory,
} from "@/lib/fleetState";
import { decisionFor } from "@/lib/rerollState";
import { CategoryLedger } from "./CategoryLedger";
import { RunUpgradesChart } from "./RunUpgradesChart";
import { sectionOpen } from "./selection";
import { agoText, amount, CAT_COLOR, CAT_LABEL, DASH, priceText, span, whole } from "./stateFormat";
import { Ago, Countdown } from "./Tickers";

export interface AccountColumnProps {
  account: FleetStateAccount;
  accent: string;
  /** Date.now() when this account last changed; also "now" for relative times. */
  changedAt: number;
  shownCount: number;
  open: Record<string, boolean>;
  onToggleSection: (key: string, open: boolean) => void;
}

function Section({ title, value, color, open, onToggle, children }: {
  title: string; value?: string; color: string; open: boolean;
  onToggle: (open: boolean) => void; children: React.ReactNode;
}): React.JSX.Element {
  return (
    <details className="fs-sec" open={open} style={{ "--sc": color } as React.CSSProperties}
      onToggle={event => { const now = event.currentTarget.open; if (now !== open) onToggle(now); }}>
      <summary className="fs-sh">{title}{value !== undefined && <span className="fs-sh-v">{value}</span>}</summary>
      <div className="fs-sb">{children}</div>
    </details>);
}

const Unavailable = (): React.JSX.Element => <p className="fs-none">Unavailable</p>;

function BestWave({ best }: { best: FleetStateBestWave | null }): React.JSX.Element {
  return (
    <div className="fs-kv fs-rec"><small>Best wave</small>
      <b>{best === null ? DASH : whole(best.wave)}{best !== null && <span className="fs-tier">T{best.tier}</span>}</b>
    </div>);
}

function BattleHud({ account, nowMs }: { account: FleetStateAccount; nowMs: number }): React.JSX.Element {
  const battle = account.battle;
  if (battle !== null) {
    const pct = battle.wave !== null && battle.best_wave ? Math.min(100, (battle.wave / battle.best_wave) * 100) : null;
    const best = account.best_wave ?? null;
    const record = battle.wave !== null && best !== null && battle.wave > best.wave;
    return (
      <div className="fs-hud on">
        <div className="fs-wave"><small>Wave{record && <span className="fs-new">New best</span>}</small>
          <b>{whole(battle.wave)}</b>{battle.tier !== null && <span className="fs-tier">T{battle.tier}</span>}</div>
        <div className="fs-kvs">
          <div className="fs-kv"><small>Cash</small><b className="fs-cash">{battle.cash === null ? DASH : `$${amount(battle.cash)}`}</b></div>
          <div className="fs-kv"><small>Elapsed</small><b>{span(battle.elapsed_s)}</b></div>
          <BestWave best={account.best_wave ?? null} />
        </div>
        <div className="fs-best">
          {pct === null ? <small>No best wave on this tier yet</small> : <>
            <div className="fs-pb"><i style={{ width: `${pct}%` }} /></div>
            <small>vs best T{battle.tier} wave {whole(battle.best_wave)} · {pct.toFixed(0)}%</small></>}
        </div>
      </div>);
  }
  const last = account.runs?.[0];
  return (
    <div className="fs-hud">
      <div className="fs-wave"><small>{account.bot.screen === "GAME_OVER" ? "Run ended" : "No battle"}</small><b className="fs-dim">{DASH}</b></div>
      <div className="fs-kvs">
        <BestWave best={account.best_wave ?? null} />
        <div className="fs-kv"><small>Last run</small>
          <b className="fs-small">{last ? `T${last.tier ?? "?"} · W${whole(last.wave)} · ${amount(last.coins)} · ${agoText(last.ended_at, nowMs)}` : "No runs yet"}</b></div>
      </div>
    </div>);
}

function QueueRow({ kind, color, name, detail, cost }: {
  kind: string; color: string; name: string; detail?: string; cost?: number | null;
}): React.JSX.Element {
  return (
    <div className="fs-q" style={{ "--c": color } as React.CSSProperties}>
      <span className="qk">{kind}</span>
      <span className="qn">{name}{detail ? <small> · {detail}</small> : null}</span>
      <span className="n">{cost === undefined ? "" : priceText(cost)}</span>
    </div>);
}

function NextBuy({ buy }: { buy: FleetStateNextBuy | null }): React.JSX.Element {
  if (buy === null) return <QueueRow kind="Workshop" color="var(--cat-utility)" name="No plan yet" />;
  const verdict = decisionFor(buy.state);
  const color = buy.category ? CAT_COLOR[buy.category] : "var(--cat-utility)";
  const pct = buy.price !== null && buy.price > 0 && buy.wallet !== null
    ? Math.min(100, (buy.wallet / buy.price) * 100) : null;
  return (
    <div className="fs-q fs-nb" style={{ "--c": color } as React.CSSProperties} title={buy.reason || verdict.hint}>
      <span className="qk">Workshop</span>
      <span className="qn">{buy.name ?? "Nothing to buy"}<small> · <span className="fs-verdict" data-tone={verdict.tone}>{verdict.label}</span></small></span>
      <span className={cn("n", buy.price === null ? "fs-dim" : "fs-coin")}>{buy.name === null ? "" : priceText(buy.price)}</span>
      {pct !== null && (
        <div className="qp">
          <div className="fs-pb" role="meter" aria-label={`${buy.name ?? "Next buy"} coins saved`}
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}><i style={{ width: `${pct}%` }} /></div>
          <small className="fs-mono">{amount(buy.wallet)} / {amount(buy.price)} · {pct.toFixed(0)}%</small>
        </div>)}
    </div>);
}

function BuyQueue({ decision, nextBuy, labs, cards }: {
  decision: FleetStateDecision | null; nextBuy: FleetStateNextBuy | null;
  labs: FleetStateLabs | null; cards: FleetStateCards | null;
}): React.JSX.Element {
  return (
    <div className="fs-queue">
      <div className="fs-qh">Bot buy queue</div>
      <NextBuy buy={nextBuy} />
      <QueueRow kind="Autopilot" color={decision?.category ? CAT_COLOR[decision.category] : "var(--primary)"}
        name={decision ? (decision.name ?? decision.phase) : "No decision yet"}
        detail={decision?.reason || undefined} cost={decision ? decision.cost : undefined} />
      {labs?.next && <QueueRow kind="Labs" color="var(--cat-labs)" name={labs.next.name} cost={labs.next.cost} />}
      {cards && cards.slots.capacity !== null && (
        <QueueRow kind="Cards" color="var(--cat-cards)" name={`Slot ${cards.slots.capacity + 1}`} cost={cards.slots.next_slot_gems} />)}
    </div>);
}

function WorkshopBody({ workshop, decision, nowMs }: {
  workshop: FleetStateWorkshop; decision: FleetStateDecision | null; nowMs: number;
}): React.JSX.Element {
  const [tab, setTab] = useState<StateCategory>(decision?.category ?? "attack");
  const category = workshop.categories[tab];
  return (<>
    <div className="fs-split" aria-hidden="true">
      {STATE_CATEGORIES.map(c => <i key={c} style={{ flex: workshop.totals[c], background: CAT_COLOR[c] }} />)}
    </div>
    <div className="fs-tabs" role="tablist" aria-label="Workshop category">
      {STATE_CATEGORIES.map(c => (
        <button key={c} type="button" role="tab" aria-selected={c === tab}
          className={cn("fs-tab", decision?.category === c && "hn")}
          style={{ "--c": CAT_COLOR[c] } as React.CSSProperties} onClick={() => setTab(c)}>
          <span>{CAT_LABEL[c]}</span><b>{whole(workshop.totals[c])}</b>
          <small>{workshop.categories[c].unlocked}/{workshop.categories[c].total} unlocked</small>
        </button>))}
    </div>
    <CategoryLedger label={`${CAT_LABEL[tab]} workshop`} head="Skill" color={CAT_COLOR[tab]} nowMs={nowMs}
      nextId={decision?.category === tab ? decision.upgrade_id : null} unlock={category.next_unlock}
      rows={category.skills.map(skill => ({
        id: skill.id, name: skill.name, level: skill.level, invested: skill.invested,
        spent: skill.bot_spent, next: skill.next_cost, maxed: skill.status === "maxed", locked: skill.locked,
      }))}
      recent={workshop.recent.map((item, index) => ({
        key: `${item.ts}-${index}`, ts: item.ts, name: item.name,
        detail: item.level === null ? null : `→ ${item.level}`, price: item.price,
        color: item.category ? CAT_COLOR[item.category] : undefined,
      }))} />
  </>);
}

function LabsBody({ labs, nowMs }: { labs: FleetStateLabs; nowMs: number }): React.JSX.Element {
  const free = labs.slots === null ? null : Math.max(0, labs.slots - labs.running.length);
  return (<>
    <ul className="fs-timers">
      {labs.running.map(job => (
        <li key={job.id}><div className="tr"><span>{job.name} <small>→ Lv {job.to_level ?? "?"}</small></span><Countdown until={job.completes_at} /></div></li>))}
      {free !== null && free > 0 && <li className="free">{free} slot{free === 1 ? "" : "s"} free{labs.next ? ` · next ${labs.next.name}` : ""}</li>}
      {labs.slots === null && <li className="fs-dim">Slots not counted yet</li>}
    </ul>
    <CategoryLedger label="Labs" head="Lab" color="var(--cat-labs)" nowMs={nowMs} investedHead={null}
      nextId={labs.next?.id ?? null}
      rows={labs.levels.map(row => ({ id: row.id, name: row.name, level: row.level, next: row.next_cost }))}
      recent={labs.recent.map((item, index) => ({ key: `${item.ts}-${index}`, ts: item.ts, name: item.name, price: item.price }))} />
  </>);
}

function CardsBody({ cards, nowMs }: { cards: FleetStateCards; nowMs: number }): React.JSX.Element {
  return (<>
    <p className="fs-note">Gems invested <b>{amount(cards.gems_invested)}</b> · next slot {priceText(cards.slots.next_slot_gems)}</p>
    <CategoryLedger label="Cards" head="Card" color="var(--cat-cards)" nowMs={nowMs} investedHead={null}
      nextHead="Copies" nextIsPrice={false} nextId={null}
      rows={cards.items.map(item => ({ id: item.name, name: item.name, level: item.level, next: item.copies }))}
      recent={cards.recent.map((item, index) => ({ key: `${item.ts}-${index}`, ts: item.ts, name: item.name, price: item.gems }))} />
  </>);
}

function AccountColumnView({ account, accent, changedAt, shownCount, open, onToggleSection }: AccountColumnProps): React.JSX.Element {
  const live = account.online && account.bot.live;
  const screen = account.bot.screen ?? (account.online ? "UNKNOWN" : "OFFLINE");
  const toggle = (section: string) => (value: boolean) => onToggleSection(`${account.id}:${section}`, value);
  const isOpen = (section: string) => sectionOpen(open, account.id, section, shownCount);
  const workshop = account.workshop, cards = account.cards, labs = account.labs;
  // `?? null`: a backend one release behind (next dev against an older bot) omits these.
  const strategy = account.strategy ?? null;
  const total = workshop ? workshop.totals.attack + workshop.totals.defense + workshop.totals.utility : null;
  const maxWave = Math.max(1, ...(account.runs ?? []).map(run => run.wave ?? 0));
  return (
    <article className="fs-acct" style={{ "--acc": accent } as React.CSSProperties}
      aria-label={account.name === account.id ? account.id : `${account.id} ${account.name}`}>
      <header className="fs-ah">
        <div className="fs-ah-row">
          <span className="fs-emu">{account.id.toUpperCase()}</span>
          {account.name !== account.id && <h2>{account.name}</h2>}
          {live && <span className="fs-live"><i aria-hidden="true" />LIVE</span>}
          {!account.online && <span className="fs-stale">{account.stale_seconds === null ? "offline" : `stale · ${span(account.stale_seconds)}`}</span>}
          <span className="fs-ser">{account.serial ?? DASH}</span>
        </div>
        {account.error && <p className="fs-error" role="status">{account.error}</p>}
        <dl className="fs-bot">
          <div><dt>Screen</dt><dd><span className="fs-scr" data-screen={screen}>{screen}</span></dd></div>
          <div><dt>Now</dt><dd>{account.bot.now ?? DASH}</dd></div>
          <div><dt>Strategy</dt><dd>{!strategy ? <span className="fs-dim">Not assigned</span> : <>
            <span className="fs-strat">{strategy.name}</span> <small className="fs-mono">v{strategy.version}</small></>}
            {account.next_buy?.goal && <small> · {account.next_buy.goal}</small>}</dd></div>
          <div><dt>Scan</dt><dd className="fs-mono fs-dim">{account.scan === null ? DASH : `#${whole(account.scan)}`} · <Ago since={changedAt} /></dd></div>
        </dl>
      </header>
      <div className="fs-top3">
        <BattleHud account={account} nowMs={changedAt} />
        <div className="fs-bal">
          <div className="fs-kv"><small>Coins</small><b className="fs-coin">{amount(account.balances?.coins)}</b></div>
          <div className="fs-kv"><small>Gems</small><b className="fs-gem">{amount(account.balances?.gems)}</b></div>
          <div className="fs-kv"><small>Stones</small><b>{DASH}</b><small className="fs-hint">not tracked yet</small></div>
        </div>
        <BuyQueue decision={account.decision} nextBuy={account.next_buy ?? null} labs={labs} cards={cards} />
      </div>
      <div className={cn("fs-flow", shownCount <= 2 && "wide")}>
        <Section title="Workshop" value={total === null ? undefined : `${whole(total)} lv`} color="var(--cat-utility)"
          open={isOpen("workshop")} onToggle={toggle("workshop")}>
          {workshop ? <WorkshopBody workshop={workshop} decision={account.decision} nowMs={changedAt} /> : <Unavailable />}
        </Section>
        <Section title="Cards" value={cards ? `${cards.slots.equipped ?? "?"}/${cards.slots.capacity ?? "?"} slots` : undefined}
          color="var(--cat-cards)" open={isOpen("cards")} onToggle={toggle("cards")}>
          {cards ? <CardsBody cards={cards} nowMs={changedAt} /> : <Unavailable />}
        </Section>
        <Section title="Labs" value={labs ? `${labs.running.length}/${labs.slots ?? "?"} running` : undefined}
          color="var(--cat-labs)" open={isOpen("labs")} onToggle={toggle("labs")}>
          {labs ? <LabsBody labs={labs} nowMs={changedAt} /> : <Unavailable />}
        </Section>
        <section className="fs-sec" style={{ "--sc": "var(--live)" } as React.CSSProperties}>
          <div className="fs-sh">In-run upgrades</div>
          <div className="fs-sb"><RunUpgradesChart data={account.run_upgrades} /></div>
        </section>
        <section className="fs-sec" style={{ "--sc": "var(--primary)" } as React.CSSProperties}>
          <div className="fs-sh">Last 5 runs</div>
          <div className="fs-sb">
            {account.runs === null ? <Unavailable /> : account.runs.length === 0 ? <p className="fs-none">No runs yet</p> : (
              <div className="fs-tw" role="region" aria-label={`${account.id} last runs`} tabIndex={0}>
                <table className="fs-runs">
                  <thead><tr><th scope="col">Tier</th><th scope="col" className="n">Wave</th><th scope="col" className="n">Coins</th><th scope="col" className="n">Time</th><th scope="col" className="n">Ended</th></tr></thead>
                  <tbody>
                    {account.runs.map((run, index) => (
                      <tr key={`${run.ended_at}-${index}`}>
                        <td className="tier">T{run.tier ?? "?"}</td>
                        <td className="n"><span className="wb" style={{ "--w": `${((run.wave ?? 0) / maxWave) * 100}%` } as React.CSSProperties}>{whole(run.wave)}</span></td>
                        <td className="n">{amount(run.coins)}</td>
                        <td className="n">{span(run.duration_s)}</td>
                        <td className="n fs-dim">{run.abandoned ? "abandoned" : agoText(run.ended_at, changedAt)}</td>
                      </tr>))}
                  </tbody>
                </table>
              </div>)}
          </div>
        </section>
      </div>
    </article>);
}

/** One emulator's column. Memoized: it re-renders when its account object
 * changes, which useFleetState only allows on a new scan. */
export const AccountColumn = memo(AccountColumnView);

"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  fetchStrategyRevision,
  saveStrategyRevision,
} from "@/lib/api";
import {
  CardActionSession,
  cardDraftError,
  cardContextKey,
  cardRequest,
  cardWrite,
  emptyCardProgram,
  fetchCardCatalog,
  fetchCards,
  fetchCardOperation,
  freshCommandKey,
  previewCardProgram,
  startCardCycle,
  submitCardCommand,
  type CardCatalog,
  type CardClientContext,
  type CardCommandIntent,
  type CardCommandRequest,
  type CardPreconditions,
  type CardPreview,
  type CardProgram,
  type CardsProjection,
} from "@/lib/cards";
import type { CardPolicy, Strategy, StrategyList } from "@/lib/types";
import { CardsWorkspace } from "./CardsWorkspace";

type PendingAction =
  | { kind: "automation"; body: CardPreconditions & { enabled: boolean } }
  | { kind: "command"; body: CardCommandRequest }
  | {
      kind: "cycle";
      body: CardPreconditions & { cycle_id: string; cap: number };
    };
/** Reusable account controller. Fleet may render this with its explicit worker context. */
export function CardsAccountClient({
  context,
}: {
  context: CardClientContext;
}) {
  const { scope, accountId, worker, baseUrl } = context;
  const client = useMemo<CardClientContext>(
    () => ({ scope, accountId, worker, baseUrl }),
    [scope, accountId, worker, baseUrl],
  );
  const key = cardContextKey(client);
  const [data, setData] = useState<CardsProjection | null>(null);
  const [savedAuthority, setSavedAuthority] = useState<CardsProjection | null>(
    null,
  );
  const [inspectedOperation, setInspectedOperation] = useState<
    import("@/lib/cards").CardOperation | null
  >(null);
  const [catalog, setCatalog] = useState<CardCatalog | null>(null);
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [draft, setDraft] = useState<CardProgram | null>(null);
  const [saved, setSaved] = useState<CardProgram | null>(null);
  const [policy, setPolicy] = useState<CardPolicy | null>(null);
  const [automationEnabled, setAutomationEnabled] = useState<
    boolean | undefined
  >(undefined);
  const [previewFor, setPreviewFor] = useState<string | null>(null);
  const [preview, setPreview] = useState<CardPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [retry, setRetry] = useState(false);
  const session = useRef(new CardActionSession<PendingAction>());
  const lifecycle = useRef<{ alive: boolean; abort: AbortController }>({
    alive: false,
    abort: new AbortController(),
  });
  const mutationBusy = useRef(false);
  const readEpoch = useRef(0);
  const dirty =
    JSON.stringify(draft) !== JSON.stringify(saved) ||
    JSON.stringify(policy) !==
      JSON.stringify(strategy?.shopping.cards ?? data?.policy ?? null);
  const authorityKey = (value: CardsProjection | null): string =>
    JSON.stringify(
      value && [
        value.account_id,
        value.config_owner,
        value.program_revision,
        value.preconditions,
        value.program,
      ],
    );
  const remoteDrift =
    !savedAuthority || authorityKey(data) !== authorityKey(savedAuthority);
  const previewKey = JSON.stringify([
    draft,
    data?.program_revision,
    data?.config_owner,
    data?.preconditions,
    data?.snapshot?.revision,
    data?.snapshot?.observed_at,
    data?.fresh,
  ]);
  const reconcileReason = remoteDrift
    ? "Configuration changed remotely or has not been reconciled. Your draft is preserved; reload the latest plan before saving or applying."
    : null;
  useEffect(() => {
    const actionSession = session.current;
    const life = { alive: true, abort: new AbortController() };
    lifecycle.current = life;
    actionSession.switchAccount(key);
    mutationBusy.current = false;
    setData(null);
    setSavedAuthority(null);
    setInspectedOperation(null);
    setCatalog(null);
    setStrategy(null);
    setEtag(null);
    setDraft(null);
    setSaved(null);
    setPolicy(null);
    setPreview(null);
    setPending(false);
    setRetry(false);
    setError(null);
    setAutomationEnabled(undefined);
    const load = async (): Promise<void> => {
      try {
        const [next, items] = await Promise.all([
          fetchCards(client, life.abort.signal),
          fetchCardCatalog(client, life.abort.signal),
        ]);
        if (!life.alive) return;
        setData(next);
        setCatalog(items);
        setPreview(null);
        setDraft(next.program);
        setSaved(next.program);
        setPolicy(next.policy);
        if (next.config_owner === "local" && !next.read_only_reason) {
          const names = (
            await cardRequest<StrategyList>(client, "/api/strategies", {
              signal: life.abort.signal,
            })
          ).data;
          const loaded = await fetchStrategyRevision(
            client,
            names.active,
            life.abort.signal,
          );
          if (!life.alive) return;
          setStrategy(loaded.data);
          setEtag(loaded.etag);
          setPolicy(loaded.data.shopping.cards);
          setAutomationEnabled(loaded.data.shopping.cards.enabled);
          const program = loaded.data.cards ?? emptyCardProgram();
          setSaved(program);
          setDraft(program);
          setSavedAuthority(
            JSON.stringify(loaded.data.cards ?? null) ===
              JSON.stringify(next.program)
              ? next
              : null,
          );
        } else {
          setSavedAuthority(next);
        }
      } catch (failure) {
        if (life.alive) setError((failure as Error).message);
      }
    };
    void load();
    const timer = window.setInterval(() => {
      const epoch = ++readEpoch.current;
      void fetchCards(client, life.abort.signal)
        .then((next) => {
          if (life.alive && epoch === readEpoch.current) setData(next);
        })
        .catch(() => {});
    }, 5000);
    return () => {
      life.alive = false;
      life.abort.abort();
      window.clearInterval(timer);
      actionSession.switchAccount("closed");
    };
  }, [client, key]);
  useEffect(() => {
    if (!draft) return;
    const abort = new AbortController();
    let alive = true;
    setPreview(null);
    const timer = window.setTimeout(() => {
      void previewCardProgram(client, draft, abort.signal)
        .then((value) => {
          if (alive) {
            setPreview(value);
            setPreviewFor(previewKey);
          }
        })
        .catch((failure) => {
          if (alive) setError(`Preview: ${(failure as Error).message}`);
        });
    }, 250);
    return () => {
      alive = false;
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [client, draft, previewKey]);
  const refresh = async (): Promise<CardsProjection | null> => {
    const life = lifecycle.current;
    const epoch = ++readEpoch.current;
    const next = await fetchCards(client, life.abort.signal);
    if (life.alive && epoch === readEpoch.current) {
      setData(next);
      return next;
    }
    return null;
  };
  const execute = async (
    attempt: NonNullable<ReturnType<CardActionSession<PendingAction>["begin"]>>,
  ): Promise<void> => {
    const life = lifecycle.current;
    setPending(true);
    setError(null);
    setRetry(false);
    try {
      if (attempt.body.kind === "command")
        await submitCardCommand(client, attempt.body.body, life.abort.signal);
      else if (attempt.body.kind === "cycle")
        await startCardCycle(client, attempt.body.body, life.abort.signal);
      else
        await cardWrite(
          client,
          "/api/cards/automation",
          attempt.body.body,
          life.abort.signal,
        );
      if (!life.alive || !session.current.isCurrent(attempt)) return;
      if (attempt.body.kind === "automation")
        setAutomationEnabled(attempt.body.body.enabled);
      session.current.complete(attempt);
      setRetry(false);
      await refresh();
    } catch (failure) {
      if (!life.alive || !session.current.isCurrent(attempt)) return;
      if (failure instanceof ApiError && failure.status < 500) {
        session.current.complete(attempt);
        setRetry(false);
      } else {
        session.current.failed(attempt);
        setRetry(true);
      }
      setError((failure as Error).message);
    } finally {
      if (life.alive) setPending(false);
    }
  };
  const submit = (intent: CardCommandIntent): void => {
    if (!data?.preconditions || mutationBusy.current) return;
    if (
      intent.kind === "apply" &&
      (remoteDrift ||
        previewFor !== previewKey ||
        dirty ||
        !savedAuthority?.preconditions ||
        intent.loadout_id !== savedAuthority.program?.selected_loadout_id)
    )
      return;
    const body: CardCommandRequest = {
      ...(intent.kind === "apply"
        ? savedAuthority!.preconditions!
        : data.preconditions),
      ...intent,
      idempotency_key: freshCommandKey(),
      quantity: intent.quantity ?? 1,
      ...(["buy", "slot"].includes(intent.kind)
        ? { budget_cycle_id: data.active_budget?.cycle_id ?? "" }
        : {}),
    };
    const attempt = session.current.begin(key, { kind: "command", body });
    if (attempt) void execute(attempt);
  };
  const save = async (): Promise<void> => {
    if (
      mutationBusy.current ||
      pending ||
      retry ||
      !strategy ||
      !draft ||
      !policy ||
      !etag ||
      remoteDrift ||
      !data?.preconditions ||
      data.config_owner !== "local"
    )
      return;
    const validationError = cardDraftError(draft, policy);
    if (validationError) {
      setError(validationError);
      return;
    }
    mutationBusy.current = true;
    setPending(true);
    setError(null);
    const life = lifecycle.current;
    try {
      const next = {
        ...strategy,
        cards: draft,
        shopping: {
          ...strategy.shopping,
          cards: { ...policy, enabled: strategy.shopping.cards.enabled },
        },
      };
      const result = await saveStrategyRevision(
        client,
        strategy.name,
        next,
        etag,
        life.abort.signal,
        data.preconditions,
      );
      if (!life.alive) return;
      setStrategy(result.data);
      setEtag(result.etag);
      setSaved(result.data.cards ?? emptyCardProgram());
      setDraft(result.data.cards ?? emptyCardProgram());
      setPolicy(result.data.shopping.cards);
      const current = await refresh();
      if (life.alive)
        setSavedAuthority(
          current?.config_owner === "local" &&
            JSON.stringify(current.program) ===
              JSON.stringify(result.data.cards ?? null)
            ? current
            : null,
        );
    } catch (failure) {
      if (life.alive)
        setError(
          failure instanceof ApiError && failure.status === 409
            ? "The strategy changed since this draft was loaded. Your draft is preserved. Revert / reload to load the current strategy before editing again."
            : (failure as Error).message,
        );
    } finally {
      if (life.alive) {
        mutationBusy.current = false;
        setPending(false);
      }
    }
  };
  const revert = async (): Promise<void> => {
    if (mutationBusy.current || pending || retry) return;
    mutationBusy.current = true;
    setPending(true);
    setError(null);
    const life = lifecycle.current;
    try {
      const next = await fetchCards(client, life.abort.signal);
      if (!life.alive) return;
      if (next.config_owner === "local" && !next.read_only_reason) {
        const names = (
          await cardRequest<StrategyList>(client, "/api/strategies", {
            signal: life.abort.signal,
          })
        ).data;
        const loaded = await fetchStrategyRevision(
          client,
          names.active,
          life.abort.signal,
        );
        if (!life.alive) return;
        setStrategy(loaded.data);
        setEtag(loaded.etag);
        setDraft(loaded.data.cards ?? emptyCardProgram());
        setSaved(loaded.data.cards ?? emptyCardProgram());
        setPolicy(loaded.data.shopping.cards);
        setAutomationEnabled(loaded.data.shopping.cards.enabled);
        setSavedAuthority(
          JSON.stringify(loaded.data.cards ?? null) ===
            JSON.stringify(next.program)
            ? next
            : null,
        );
        setData(next);
      } else {
        setStrategy(null);
        setEtag(null);
        setDraft(next.program);
        setSaved(next.program);
        setPolicy(next.policy);
        setSavedAuthority(next);
        setData(next);
        setAutomationEnabled(undefined);
      }
    } catch (failure) {
      if (life.alive) setError((failure as Error).message);
    } finally {
      if (life.alive) {
        mutationBusy.current = false;
        setPending(false);
      }
    }
  };
  if (!data || !catalog)
    return (
      <p role={error ? "alert" : "status"} className="p-4 text-sm">
        {error ?? "Loading Cards…"}
      </p>
    );
  return (
    <CardsWorkspace
      key={key}
      context={client}
      data={data}
      catalog={catalog}
      saved={saved}
      draft={draft}
      policy={policy}
      preview={previewFor === previewKey ? preview : null}
      dirty={dirty}
      applyDisabledReason={
        reconcileReason ||
        (previewFor !== previewKey ? "Waiting for saved plan preview" : null)
      }
      reloadLabel={remoteDrift ? "Reload latest plan" : "Revert draft"}
      inspectedOperation={inspectedOperation}
      onInspectEvidence={(id) => {
        const life = lifecycle.current;
        void fetchCardOperation(client, id, life.abort.signal)
          .then((operation) => {
            if (life.alive) setInspectedOperation(operation);
          })
          .catch((failure) => {
            if (life.alive) setError((failure as Error).message);
          });
      }}
      pending={pending}
      error={error}
      retryAvailable={retry}
      editDisabledReason={
        reconcileReason ||
        (data.config_owner === "local" && (!etag || !strategy)
          ? "Waiting for a revision-checked strategy"
          : null)
      }
      automationEnabled={automationEnabled}
      onAutomation={(enabled) => {
        if (!data.preconditions || mutationBusy.current) return;
        const attempt = session.current.begin(key, {
          kind: "automation",
          body: { ...data.preconditions, enabled },
        });
        if (attempt) void execute(attempt);
      }}
      onChange={setDraft}
      onPolicyChange={setPolicy}
      onSave={() => void save()}
      onRevert={() => void revert()}
      onCommand={submit}
      onStartCycle={(cap) => {
        if (!data.preconditions || mutationBusy.current) return;
        const attempt = session.current.begin(key, {
          kind: "cycle",
          body: { ...data.preconditions, cycle_id: freshCommandKey(), cap },
        });
        if (attempt) void execute(attempt);
      }}
      onRetry={() => {
        const attempt = session.current.retry(key);
        if (attempt) void execute(attempt);
      }}
      onDiscardRetry={() => {
        session.current.switchAccount(key);
        setRetry(false);
        setError(
          "Retry dismissed. Check recorded activity before starting another action.",
        );
        void refresh();
      }}
    />
  );
}

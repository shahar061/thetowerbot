"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { fetchAccounts, type AccountChoice } from "./api";
import { setAccountScope } from "./accountScope";

type Selection = {
  accounts: AccountChoice[];
  selected: AccountChoice | null;
  loading: boolean;
  error: string | null;
  choose: (key: string) => void;
};

const Context = createContext<Selection | null>(null);
const STORAGE_KEY = "tower-selected-account";

export function AccountSelectionProvider({ children }: { children: React.ReactNode }) {
  const [accounts, setAccounts] = useState<AccountChoice[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const catalog = await fetchAccounts();
        if (!alive) return;
        const remembered = window.localStorage.getItem(STORAGE_KEY);
        const next = remembered === "none" ? null
          : catalog.accounts.some(account => account.key === remembered)
            ? remembered : catalog.active;
        setAccountScope(next);
        setSelectedKey(next);
        setAccounts(catalog.accounts);
        setError(null);
      } catch (failure) {
        if (alive) setError((failure as Error).message);
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 10_000);
    return () => { alive = false; window.clearInterval(timer); setAccountScope(null); };
  }, []);

  const choose = (key: string) => {
    const next = accounts.some(account => account.key === key) ? key : null;
    setAccountScope(next);
    setSelectedKey(next);
    window.localStorage.setItem(STORAGE_KEY, next ?? "none");
  };
  const selected = accounts.find(account => account.key === selectedKey) ?? null;
  return <Context.Provider value={{ accounts, selected, loading, error, choose }}>
    {children}
  </Context.Provider>;
}

export function useAccountSelection(): Selection {
  const value = useContext(Context);
  if (!value) throw new Error("AccountSelectionProvider is missing");
  return value;
}

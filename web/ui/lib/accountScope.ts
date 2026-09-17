let selectedScope: string | null = null;

export function setAccountScope(scope: string | null): void {
  selectedScope = scope;
}

export function accountScope(): string | null {
  return selectedScope;
}

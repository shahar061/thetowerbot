import type { RerollMember } from "@/lib/fleet";

export function memberIdentity(member: Pick<RerollMember, "name" | "account_key" | "account_id" | "lease_id">): string {
  return JSON.stringify([member.name, member.account_key, member.account_id, member.lease_id]);
}

export function timeLabel(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${total % 60}s`;
}

import { TelegramSettings } from "@/components/TelegramSettings";
import { FleetTimingSettings } from "./FleetTimingSettings";
import { RecoverySettingsPanel } from "../RecoverySettingsPanel";
import { PageHeader } from "@/components/PageHeader";

export default function FleetSettingsPage(): React.JSX.Element {
  return <div className="flex max-w-3xl flex-col gap-5">
    <PageHeader title="Fleet settings" meta="Notifications, timing and recovery" />
    <TelegramSettings mode="fleet" embedded />
    <FleetTimingSettings />
    <RecoverySettingsPanel />
  </div>;
}

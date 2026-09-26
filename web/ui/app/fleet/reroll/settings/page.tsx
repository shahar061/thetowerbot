import { TelegramSettings } from "@/components/TelegramSettings";
import { FleetTimingSettings } from "./FleetTimingSettings";

export default function FleetSettingsPage(): React.JSX.Element {
  return <div className="flex max-w-3xl flex-col gap-5">
    <TelegramSettings mode="fleet" />
    <FleetTimingSettings />
  </div>;
}

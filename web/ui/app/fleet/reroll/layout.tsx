import { RerollWorkspaceProvider } from "./RerollWorkspace";
import { FleetLabsProvider } from "./FleetLabsContext";

export default function RerollLayout({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <RerollWorkspaceProvider><FleetLabsProvider>{children}</FleetLabsProvider></RerollWorkspaceProvider>;
}

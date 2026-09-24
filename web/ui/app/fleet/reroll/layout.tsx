import { RerollWorkspaceProvider } from "./RerollWorkspace";

export default function RerollLayout({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <RerollWorkspaceProvider>{children}</RerollWorkspaceProvider>;
}

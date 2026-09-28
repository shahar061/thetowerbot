import "./fleet-state.css";

// The page's stylesheet is imported here rather than by page.tsx, so the page
// stays importable by vitest, which does not run the PostCSS pipeline.
export default function FleetStateLayout({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <>{children}</>;
}

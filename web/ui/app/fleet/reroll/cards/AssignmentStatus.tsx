export function AssignmentStatus({ published, applied }: { published: number; applied: number | null }): React.JSX.Element {
  return <span>{published === applied ? "Active on worker" : "Pending on worker"}</span>;
}

import { Link } from "react-router-dom";
import { EmptyState } from "../components/States";

export function NotFoundPage() {
  return (
    <EmptyState title="Page not found">
      <Link to="/">Return to the overview</Link>
    </EmptyState>
  );
}

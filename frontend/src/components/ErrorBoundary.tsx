import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode; resetKey?: string };
type State = { failed: boolean };

/** Contains a rendering or chunk-load failure to the page area so the navigation shell stays usable. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Deliberately not sent anywhere: no user data leaves the browser from here.
    console.error("Page failed to render", error.message, info.componentStack?.split("\n")[1]?.trim());
  }

  componentDidUpdate(prev: Props) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="panel border-(--color-danger)/50 p-6">
        <p className="m-0 font-semibold text-(--color-danger)">This page could not be displayed</p>
        <p className="mt-2 text-sm text-(--color-muted)">
          A part of the application failed to load. Your data is unaffected. Reload the page, or open another section.
        </p>
        <button type="button" className="btn mt-4" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    );
  }
}

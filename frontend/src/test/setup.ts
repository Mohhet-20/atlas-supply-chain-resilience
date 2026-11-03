import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// Routes are lazy-loaded; under a parallel run a chunk import can exceed the 1s default, which made async queries flaky.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => cleanup());

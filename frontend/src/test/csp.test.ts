import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { CSP_DIRECTIVES, SECURITY_HEADERS, contentSecurityPolicy } from "../../csp";

const nginx = readFileSync(resolve(__dirname, "../../nginx.conf"), "utf8");

describe("content security policy", () => {
  it("is strict: no inline or eval script, no inline style, no plugins, no framing", () => {
    const csp = contentSecurityPolicy();
    expect(csp).toContain("script-src 'self';");
    expect(csp).toContain("style-src 'self';");
    expect(csp).not.toMatch(/unsafe-inline|unsafe-eval|\*/);
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
  });

  it("adds only the given API origin to connect-src", () => {
    expect(contentSecurityPolicy(["http://127.0.0.1:8765"])).toMatch(/connect-src 'self' http:\/\/127\.0\.0\.1:8765$/);
  });

  it("nginx serves the same policy and headers in every location that serves the app", () => {
    const blocks = ["location /assets/", "location / "].map((start) => {
      const from = nginx.indexOf(start);
      expect(from, start).toBeGreaterThan(-1);
      return nginx.slice(from, nginx.indexOf("\n  }", from));
    });
    for (const block of blocks) {
      expect(block).toContain(`add_header Content-Security-Policy "${contentSecurityPolicy()}" always;`);
      for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
        expect(block).toContain(`add_header ${name} "${value}" always;`);
      }
    }
    expect(CSP_DIRECTIVES.length).toBeGreaterThan(5);
  });
});

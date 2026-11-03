import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "node_modules"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended, reactHooks.configs.flat["recommended-latest"] ?? reactHooks.configs.recommended],
    languageOptions: { ecmaVersion: 2022, globals: { ...globals.browser } },
    rules: {
      "no-console": ["error", { allow: ["warn", "error"] }],
    },
  },
);

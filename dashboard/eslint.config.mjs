// Flat ESLint config for Next 16. `next lint` was removed in Next 16, so the
// project runs the ESLint CLI directly (`npm run lint` -> `eslint .`). The rule
// sets come from eslint-config-next, whose subpath exports are flat-config arrays:
// `core-web-vitals` carries the React, React-Hooks, jsx-a11y and Next rules, and
// `typescript` layers the typescript-eslint rules on top.
import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "coverage/**"] },
  ...coreWebVitals,
  ...typescript,
];

export default config;

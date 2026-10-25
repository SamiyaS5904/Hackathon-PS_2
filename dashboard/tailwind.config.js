/** Tailwind theme: dark premium surfaces + the validated chart palette (dataviz reference palette, dark steps). */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        page: "#0d0d0d",        // page plane
        surface: "#1a1a19",     // cards and chart surface
        raised: "#232322",      // hover / inputs
        hairline: "#2c2c2a",    // gridlines, borders
        baseline: "#383835",    // axis / stronger border
        ink: "#f0efec",         // primary text
        ink2: "#c3c2b7",        // secondary text
        muted: "#898781",       // labels, axis text
        accent: "#3987e5",      // brand accent (= series slot 1)
        s1: "#3987e5",          // series: AI agent
        s2: "#d95926",          // series: free rules (fallback)
        s3: "#199e70",          // series: gatekeeper skip
        good: "#0ca30c",
        warning: "#fab219",
        serious: "#ec835a",
        critical: "#d03b3b",
      },
      fontFamily: {
        sans: ["system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
    },
  },
  plugins: [],
};

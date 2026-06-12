/**
 * Tailwind config for the self-hosted build (replaces the Play CDN + its inline
 * config). Regenerate the CSS after changing templates:
 *   npx tailwindcss@3 -c tailwind.config.js \
 *     -i watcher/web/static/tailwind-input.css \
 *     -o watcher/web/static/tailwind.css --minify
 */
module.exports = {
  content: ["./watcher/web/templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      colors: { brand: { 200: "#c7d2fe", 300: "#a5b4fc", 400: "#818cf8", 500: "#6366f1", 600: "#4f46e5" } },
    },
  },
};

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,ts}"],
  theme: {
    extend: {
      colors: {
        primary: { DEFAULT: "#2563EB", hover: "#1D4ED8", soft: "#EFF4FF" },
        onprimary: "#FFFFFF",
        background: "#FFFFFF",
        surface: "#F8FAFC",
        muted: "#F1F3F5",
        foreground: { DEFAULT: "#0F172A", secondary: "#64748B" },
        border: "#E4E7EB",
        success: "#059669",
        warning: "#D97706",
        destructive: "#DC2626",
      },
      fontFamily: {
        sans: ["Inter", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "sans-serif"],
      },
      fontSize: {
        xs: "12px", sm: "13px", base: "14px", lg: "16px", xl: "20px", "2xl": "24px",
      },
      borderRadius: { md: "6px", lg: "8px" },
      boxShadow: {
        card: "0 1px 2px rgba(15,23,42,.06)",
        overlay: "0 4px 16px rgba(15,23,42,.08)",
      },
      transitionDuration: { fast: "150ms", base: "200ms" },
    },
  },
  plugins: [],
}

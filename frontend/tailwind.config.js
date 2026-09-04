/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef4ff",
          100: "#dce8ff",
          200: "#b8d0ff",
          300: "#8ab0ff",
          400: "#5c8bff",
          500: "#3667f5",
          600: "#254dd1",
          700: "#1c3aa8",
          800: "#182f80",
          900: "#152964",
        },
      },
    },
  },
  plugins: [],
};

import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trading Framework | Research Console",
  description: "Local-first trading research and paper-trading dashboard.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  var theme = localStorage.getItem('theme');
                  var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                  var initial = theme || (prefersDark ? 'dark' : 'light');
                  document.documentElement.classList.toggle('dark', initial === 'dark');
                } catch (e) {}
              })();
            `,
          }}
        />
      </head>
      <body className="min-h-screen bg-[var(--canvas)] transition-colors duration-300 ease-in-out">
        {children}
      </body>
    </html>
  );
}
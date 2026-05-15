import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Post-cart stress plots",
  description: "PDF plots for post-cart-stress-open runs",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header
          style={{
            display: "flex",
            alignItems: "center",
            gap: "1.25rem",
            padding: "0.5rem 0.75rem",
            background: "#0f172a",
            borderBottom: "1px solid #1e293b",
          }}
        >
          <span
            style={{
              fontSize: "0.72rem",
              fontWeight: 800,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: "#94a3b8",
              marginRight: "0.25rem",
            }}
          >
            Plot viewer
          </span>
          <nav style={{ display: "flex", gap: "0.35rem" }}>
            <Link
              href="/"
              style={{
                color: "#e2e8f0",
                fontSize: "0.88rem",
                fontWeight: 600,
                textDecoration: "none",
                padding: "0.35rem 0.65rem",
                borderRadius: 6,
              }}
            >
              Plots
            </Link>
            <Link
              href="/table"
              style={{
                color: "#e2e8f0",
                fontSize: "0.88rem",
                fontWeight: 600,
                textDecoration: "none",
                padding: "0.35rem 0.65rem",
                borderRadius: 6,
              }}
            >
              Table
            </Link>
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}

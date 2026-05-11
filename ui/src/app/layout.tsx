import "@/styles/globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "IntelliAI 2.0 — Migration Control Plane",
  description:
    "IBM MQ migration control plane. intelliAI2DotO team, Wells Fargo Hackathon 2026.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body>{children}</body>
    </html>
  );
}

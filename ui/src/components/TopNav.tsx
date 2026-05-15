"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/migrations", label: "Migrations" },
  { href: "/viz", label: "Topology Viz" },
];

export default function TopNav() {
  const pathname = usePathname();

  return (
    <nav className="border-b border-border-subtle bg-bg-elevated">
      <div className="mx-auto flex max-w-6xl items-center gap-1 px-6 py-2">
        <div className="mr-4 flex items-baseline gap-2">
          <span className="text-sm font-semibold tracking-tight">
            IntelliAI 2.0
          </span>
          <span className="text-[10px] uppercase tracking-wider text-fg-subtle">
            BCL
          </span>
        </div>
        {LINKS.map((l) => {
          // Active when path starts with href (root only matches root).
          const active =
            l.href === "/"
              ? pathname === "/"
              : pathname === l.href || pathname.startsWith(l.href + "/");
          return (
            <Link
              key={l.href}
              href={l.href}
              className={
                "rounded-md px-3 py-1.5 text-sm transition-colors " +
                (active
                  ? "bg-bg-subtle text-fg"
                  : "text-fg-muted hover:bg-bg-subtle hover:text-fg")
              }
            >
              {l.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}

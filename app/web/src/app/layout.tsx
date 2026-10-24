import { NuqsAdapter } from "nuqs/adapters/next/app";
import { Toaster } from "sonner";
import { AuthGate } from "@/app/components/AuthGate";
import "./globals.css";
import "./views.css";
import "./minecraft.css";
import "./experience.css";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="zh-CN"
      suppressHydrationWarning
    >
      <body
        className="aidison-body"
        suppressHydrationWarning
      >
        <NuqsAdapter>
          <AuthGate>{children}</AuthGate>
        </NuqsAdapter>
        <Toaster />
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import type { ReactNode } from "react";

import { WorkspaceProvider } from "@/components/gateway/workspace-provider";
import { BetaChatWidget } from "@/components/beta-chat-widget";
import { LanguageProvider } from "@/components/language-provider";
import { LanguageToggle } from "@/components/language-toggle";

import "./globals.css";
import "./workspace-ui.css";
import "./brand-theme.css";

export const metadata: Metadata = {
  title: "Taskway",
  description: "Một cổng điều phối thống nhất để định tuyến, dự phòng và quan sát mọi yêu cầu AI.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="vi">
      <body>
        <LanguageProvider>
          <WorkspaceProvider>{children}<BetaChatWidget /></WorkspaceProvider>
          <LanguageToggle floating />
        </LanguageProvider>
      </body>
    </html>
  );
}

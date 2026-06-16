import type { Metadata } from "next";
import { Inter, Lora, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { I18nProvider } from "@/lib/i18n";
import { ThemeProvider, themeInitScript } from "@/lib/theme";

const inter = Inter({
  subsets: ["latin", "cyrillic"],
  variable: "--font-inter",
  display: "swap",
});

// Lora — редакционная антиква с кириллицей: задаёт «человеческий», не-шаблонный
// характер заголовкам.
const lora = Lora({
  subsets: ["latin", "cyrillic"],
  variable: "--font-lora",
  display: "swap",
});

// JetBrains Mono — для меток источников (ст. 14 · adilet.zan.kz) и метрик:
// моноширинный «след документа», подчёркивающий доказательную природу продукта.
const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin", "cyrillic"],
  variable: "--font-mono-jb",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Zerde — правовой анализ НПА РК",
  description: "Проверка законопроектов против adilet.zan.kz: каждое утверждение со ссылкой на источник.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="ru"
      className={`${inter.variable} ${lora.variable} ${jetbrainsMono.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <ThemeProvider>
          <I18nProvider>
            <Navbar />
            <main>{children}</main>
            <Footer />
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}

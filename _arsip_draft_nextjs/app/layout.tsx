import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DCC Disaster Center | Sumatera Barat",
  description: "Pemantauan peringatan BMKG dan paparan aset jaringan distribusi 20 kV di Sumatera Barat.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="id">
      <body className="antialiased">{children}</body>
    </html>
  );
}

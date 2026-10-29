import type { Metadata } from "next"
import "./globals.css"

export const metadata: Metadata = {
  title: "AQ6370E 光谱监控台",
  description: "AQ6370E 多通道动态寻峰与 SQLite 数据监控 Web App",
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>
}

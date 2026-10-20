import type { Metadata } from "next";
import "@xyflow/react/dist/style.css";
import "./globals.css";
import "./quant-workflow.css";
import "./factor-lab.css";
import "./ai-workspace.css";
import Providers from "./providers";

export const metadata: Metadata = {
  title: "量构 QuantWeave · 低代码A股量化研究",
  description: "独立运行的低代码A股策略搭建、因子实验与回测审计平台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body><Providers>{children}</Providers></body></html>;
}

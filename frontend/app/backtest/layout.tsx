import type { Metadata } from "next";
import "./backtest-site.css";

export const metadata: Metadata = {
  title: "量构 QuantWeave · 低代码A股量化研究",
  description: "面向懂K线、不会编程用户的低代码A股策略搭建、因子实验与回测审计平台",
};

export default function BacktestLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}

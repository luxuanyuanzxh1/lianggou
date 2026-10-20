import BacktestSite from "./backtest-site";

export default async function BacktestPage({
  searchParams,
}: {
  searchParams?: Promise<{ view?: string | string[] }>;
}) {
  const params = searchParams ? await searchParams : {};
  const view = Array.isArray(params.view) ? params.view[0] : params.view;
  return <BacktestSite initialView={view} />;
}

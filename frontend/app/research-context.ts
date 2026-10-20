export type ResearchContext = {
  surface: "studio" | "factors" | "guide" | "overview" | "runs" | "data" | "ai";
  title: string;
  summary: string;
  details?: string[];
  suggestions?: string[];
};

// Shared types mirroring the backend responses.

export interface IndexInfo {
  key: string;
  name: string;
  lotSize: number;
}

export interface Greeks {
  delta: number | null;
  theta: number | null;
  gamma: number | null;
  vega: number | null;
}

export interface OptionLeg {
  securityId: number | null;
  ltp: number | null;
  prevClose: number | null;
  oi: number | null;
  prevOi: number | null;
  volume: number | null;
  prevVolume: number | null;
  avgPrice: number | null;
  iv: number | null;
  bid: number | null;
  bidQty: number | null;
  ask: number | null;
  askQty: number | null;
  greeks: Greeks;
}

export interface OptionRow {
  strike: number;
  ce: OptionLeg;
  pe: OptionLeg;
}

export interface ChainSnapshot {
  index: string;
  expiry: string;
  underlyingLtp: number;
  rows: OptionRow[];
  fetchedAt: number;
  ageSeconds: number;
  stale: boolean;
  error: string | null;
}

export interface OrderPreview {
  index: string;
  optionType: string;
  side: string;
  quantity: number;
  lots?: number;
  lotSize?: number;
  units?: number;
  entryPrice: number;
  targetPrice: number;
  expectedProfitPerUnit: number;
  safetyStopPrice: number;
  safetyStopNote: string;
  noStopLoss: boolean;
  exchangeSegment: string;
  productType: string;
}

export interface OrderPreviewResponse {
  ok: boolean;
  preview: OrderPreview;
  dhanPayload: Record<string, unknown>;
}

export interface Funds {
  availabelBalance?: number;
  sodLimit?: number;
  collateralAmount?: number;
  utilizedAmount?: number;
  withdrawableBalance?: number;
  [k: string]: unknown;
}

export interface Position {
  tradingSymbol?: string;
  securityId?: string;
  positionType?: string;
  productType?: string;
  exchangeSegment?: string;
  netQty?: number;
  buyAvg?: number;
  sellAvg?: number;
  ltp?: number;
  realizedProfit?: number;
  unrealizedProfit?: number;
  drvExpiryDate?: string;
  drvOptionType?: string;
  drvStrikePrice?: number;
  [k: string]: unknown;
}

export interface SuperOrderLeg {
  orderId: string;
  legName: string;
  transactionType: string;
  price?: number;
  orderStatus?: string;
  triggeredQuantity?: number;
  trailingJump?: number;
}

export interface SuperOrder {
  orderId: string;
  correlationId?: string;
  orderStatus: string;
  transactionType: string;
  exchangeSegment?: string;
  productType?: string;
  orderType?: string;
  tradingSymbol?: string;
  securityId?: string;
  quantity?: number;
  price?: number;
  ltp?: number;
  filledQty?: number;
  averageTradedPrice?: number;
  createTime?: string;
  omsErrorDescription?: string;
  legDetails?: SuperOrderLeg[];
  [k: string]: unknown;
}

export interface StatusResponse {
  dryRun: boolean;
  fastOrders: boolean;
  profileOk: boolean;
  profileError: unknown;
  activeSegment?: string;
  dataPlan?: string;
  ip: { primaryIP?: string; secondaryIP?: string; [k: string]: unknown };
  ipError: unknown;
}

export interface EodSummary {
  generatedAt: number;
  totalAttempts: number;
  dryRunAttempts: number;
  liveAttempts: number;
  liveSuccess: number;
  liveFailed: number;
  entries: Array<Record<string, unknown>>;
}

// ---- Intelligence layer ----

export interface GuidanceScenario {
  spotMovePct: number;
  spotLevel: number;
  optionMovePct: number;
}

export interface HistoryStudy {
  index: string;
  optionType: string;
  strikeOffset: string;
  winRate: number | null;
  avgIntradayGainPct: number | null;
  avgIntradayDrawdownPct: number | null;
  avgCloseVsOpenPct: number | null;
  sampleDays: number;
  ivAvg: number | null;
  spotMoveAvgPct: number | null;
  ageSeconds: number;
  error: string | null;
}

export interface Guidance {
  index: string;
  optionType: string;
  verdict: "BUY" | "SELL" | "WAIT";
  confidence: number;
  bullScore: number;
  probabilityUp: number;
  reasons: string[];
  spot: number | null;
  atmStrike: number | null;
  rsi: number | null;
  macd: { macd: number; signal: number; hist: number } | null;
  trend: string | null;
  realizedVolPct: number | null;
  iv: number | null;
  suggestedTargetPct: number;
  suggestedSafetySlPct: number;
  impliedDailyMovePct: number | null;
  supportResistance: SupportResistance | null;
  history: HistoryStudy | null;
  scenarios: GuidanceScenario[];
  disclaimer: string;
}

export interface Sentiment {
  label: "BULLISH" | "BEARISH" | "NEUTRAL";
  score: number;
  confidence: number;
  matched: string[];
  impact: "low" | "medium" | "high";
}

export interface NewsItem {
  title: string;
  link: string;
  source: string;
  published: string | null;
  ts?: number | null;
  sentiment: Sentiment;
}

export interface NewsSummary {
  stance: "BULLISH" | "BEARISH" | "NEUTRAL";
  netScore: number;
  bullish: number;
  bearish: number;
  neutral: number;
  total: number;
}

export interface NewsResponse {
  items: NewsItem[];
  count: number;
  summary: NewsSummary;
  ageSeconds: number | null;
  error: string | null;
}

// ---- Seasonality ----

export interface SameDateSample {
  year: number;
  date: string;
  open: number;
  close: number;
  changePct: number | null;
}

export interface WeekdayStats {
  weekday: string;
  samples: number;
  avgPct?: number;
  winRate?: number;
  bestPct?: number;
  worstPct?: number;
  lastPct?: number;
}

export interface Seasonality {
  index: string;
  today: string;
  weekday: string;
  sameDate: {
    samples: SameDateSample[];
    avgPct: number | null;
    upDays: number;
    total: number;
  };
  weekdayStats: WeekdayStats;
  barsAnalyzed: number;
  ageSeconds: number;
  error: string | null;
}

// ---- Per-strike recommendations ----

export interface StrikeRecommendation {
  strike: number;
  side: "CE" | "PE";
  action: "BUY" | "WAIT" | "AVOID";
  profitProbability: number;
  confidence: number;
  ltp?: number;
  iv?: number | null;
  delta?: number | null;
  theta?: number | null;
  thetaRisk?: "low" | "medium" | "high";
  suggestedTargetPct?: number;
  suggestedStopPct?: number;
  score?: number;
  reasons: string[];
}

export interface RecommendationResponse {
  index?: string;
  expiry?: string;
  spot?: number;
  daysToExpiry?: number | null;
  marketBias?: number;
  stance?: "BULLISH" | "BEARISH" | "NEUTRAL";
  rsi?: number | null;
  macd?: { macd: number; signal: number; hist: number } | null;
  trend?: string | null;
  contextReasons?: string[];
  recommendations: StrikeRecommendation[];
  disclaimer?: string;
  error?: string;
}

export interface SupportResistance {
  support: number[];
  resistance: number[];
  pivot: Record<string, number> | null;
}

export interface CacheIndexState {
  candles: number;
  ageSeconds: number;
  fresh: boolean;
  error: string | null;
}
export interface CacheState {
  ok: boolean;
  warmed: boolean;
  indices: Record<string, CacheIndexState>;
}

export interface IpStatus {
  registered: unknown;
  registeredError: unknown;
  detectedIp: string | null;
  match: boolean | null;
  // Direct verdict from Dhan's /ip/getIP response:
  dhanSeenIp?: string | null;
  ipMatchStatus?: string | null;
  ordersAllowed?: boolean | null;
}

export interface AppSettings {
  ok: boolean;
  dryRun: boolean;
  fastOrders: boolean;
  host: string;
  port: number;
  logDir: string;
  logRetentionDays: number;
  logLevel: string;
}

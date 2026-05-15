import type { Market, MarketMatchMap, SuggestedMatch } from "../types";

const MIN_SCORE = 0.5;
const MAX_CANDIDATES_PER_TOKEN = 250;

const STOP_WORDS = new Set([
  "a",
  "an",
  "and",
  "are",
  "be",
  "by",
  "de",
  "del",
  "el",
  "en",
  "for",
  "ganara",
  "ganar",
  "gana",
  "la",
  "las",
  "lo",
  "los",
  "of",
  "on",
  "or",
  "para",
  "que",
  "quien",
  "the",
  "to",
  "will",
  "win",
  "winner",
  "with",
]);

interface IndexedMarket {
  market: Market;
  key: string;
  tokens: Set<string>;
  tokenList: string[];
  endTime: number | null;
}

interface CandidatePair {
  poly: IndexedMarket;
  kalshi: IndexedMarket;
  score: number;
  sharedTerms: string[];
}

export function getMarketKey(market: Market): string {
  return `${market.source}:${market.id}`;
}

export function findSuggestedMatches(
  polymarkets: Market[],
  kalshiMarkets: Market[],
): MarketMatchMap {
  const polyIndexed = polymarkets.map(indexMarket);
  const kalshiIndexed = kalshiMarkets.map(indexMarket);
  const kalshiByToken = buildTokenIndex(kalshiIndexed);
  const kalshiByKey = new Map(kalshiIndexed.map((market) => [market.key, market]));
  const pairs: CandidatePair[] = [];

  for (const poly of polyIndexed) {
    const candidateKeys = new Set<string>();

    for (const token of poly.tokens) {
      const candidates = kalshiByToken.get(token);
      if (!candidates || candidates.length > MAX_CANDIDATES_PER_TOKEN) continue;
      candidates.forEach((candidate) => candidateKeys.add(candidate.key));
    }

    for (const key of candidateKeys) {
      const kalshi = kalshiByKey.get(key);
      if (!kalshi) continue;

      const { score, sharedTerms } = scorePair(poly, kalshi);
      if (score >= MIN_SCORE) {
        pairs.push({ poly, kalshi, score, sharedTerms });
      }
    }
  }

  pairs.sort((a, b) => b.score - a.score);
  return assignVisualGroups(pairs);
}

function indexMarket(market: Market): IndexedMarket {
  const tokenList = tokenize(`${market.title} ${market.category}`);

  return {
    market,
    key: getMarketKey(market),
    tokens: new Set(tokenList),
    tokenList,
    endTime: parseEndTime(market.end_date),
  };
}

function buildTokenIndex(markets: IndexedMarket[]): Map<string, IndexedMarket[]> {
  const index = new Map<string, IndexedMarket[]>();

  for (const market of markets) {
    for (const token of market.tokens) {
      const current = index.get(token) ?? [];
      current.push(market);
      index.set(token, current);
    }
  }

  return index;
}

function scorePair(poly: IndexedMarket, kalshi: IndexedMarket) {
  const sharedTerms = [...poly.tokens].filter((token) => kalshi.tokens.has(token));
  const unionSize = new Set([...poly.tokens, ...kalshi.tokens]).size;
  const tokenScore = unionSize > 0 ? sharedTerms.length / unionSize : 0;
  const containmentScore =
    Math.min(poly.tokens.size, kalshi.tokens.size) > 0
      ? sharedTerms.length / Math.min(poly.tokens.size, kalshi.tokens.size)
      : 0;
  const categoryScore =
    poly.market.category &&
    kalshi.market.category &&
    normalizeText(poly.market.category) === normalizeText(kalshi.market.category)
      ? 0.1
      : 0;
  const dateScore = closeDateBonus(poly.endTime, kalshi.endTime);
  const score = Math.min(1, tokenScore * 0.55 + containmentScore * 0.35 + categoryScore + dateScore);

  return {
    score,
    sharedTerms,
  };
}

function assignVisualGroups(pairs: CandidatePair[]): MarketMatchMap {
  const map: MarketMatchMap = {};
  const usedKalshi = new Set<string>();
  const polyGroups = new Map<string, { groupId: string; colorIndex: number }>();
  let groupCount = 0;

  for (const pair of pairs) {
    if (usedKalshi.has(pair.kalshi.key)) continue;

    let group = polyGroups.get(pair.poly.key);
    if (!group) {
      group = {
        groupId: `suggested-${groupCount + 1}`,
        colorIndex: groupCount % 8,
      };
      polyGroups.set(pair.poly.key, group);
      groupCount += 1;
    }

    const existingPolyMatch = map[pair.poly.key];
    if (!existingPolyMatch || pair.score > existingPolyMatch.score) {
      map[pair.poly.key] = buildMatch(
        group.groupId,
        group.colorIndex,
        pair.score,
        pair.kalshi.market,
        pair.sharedTerms,
      );
    }

    map[pair.kalshi.key] = buildMatch(
      group.groupId,
      group.colorIndex,
      pair.score,
      pair.poly.market,
      pair.sharedTerms,
    );

    usedKalshi.add(pair.kalshi.key);
  }

  return map;
}

function buildMatch(
  groupId: string,
  colorIndex: number,
  score: number,
  peer: Market,
  sharedTerms: string[],
): SuggestedMatch {
  return {
    groupId,
    colorIndex,
    score,
    peerTitle: peer.title,
    peerSource: peer.source,
    sharedTerms: sharedTerms.slice(0, 5),
  };
}

function tokenize(value: string): string[] {
  return normalizeText(value)
    .split(" ")
    .map(normalizeSynonym)
    .filter((token) => token.length > 2 && !STOP_WORDS.has(token));
}

function normalizeText(value: string): string {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function normalizeSynonym(token: string): string {
  const synonyms: Record<string, string> = {
    america: "usa",
    estadounidense: "usa",
    eeuu: "usa",
    states: "usa",
    "u.s": "usa",
    "u.s.a": "usa",
    united: "usa",
  };

  return synonyms[token] ?? token;
}

function parseEndTime(value: string): number | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

function closeDateBonus(polyEndTime: number | null, kalshiEndTime: number | null): number {
  if (!polyEndTime || !kalshiEndTime) return 0;

  const daysApart = Math.abs(polyEndTime - kalshiEndTime) / 86_400_000;
  if (daysApart <= 1) return 0.1;
  if (daysApart <= 7) return 0.05;
  return 0;
}

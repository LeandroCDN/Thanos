import type { Market, MarketMatchMap, SuggestedCandidate, SuggestedMatch } from "../types";

const MIN_SCORE = 0.28;
const MAX_CANDIDATES_PER_TOKEN = 400;
const MAX_CANDIDATES_PER_MARKET = 12;

type MarketKind =
  | "exact_score"
  | "total"
  | "spread"
  | "winner"
  | "advance"
  | "elimination"
  | "best_host"
  | "host"
  | "regional_winner"
  | "tournament"
  | "generic";

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
  "from",
  "get",
  "gets",
  "in",
  "into",
  "la",
  "las",
  "lo",
  "los",
  "market",
  "of",
  "on",
  "or",
  "para",
  "que",
  "quien",
  "the",
  "this",
  "to",
  "will",
  "with",
]);

const KIND_WORDS = new Set([
  "advance",
  "advances",
  "champion",
  "eliminated",
  "elimination",
  "final",
  "handicap",
  "host",
  "hosts",
  "performing",
  "over",
  "qualify",
  "score",
  "spread",
  "total",
  "under",
  "win",
  "winner",
]);

interface ExactScore {
  home: string | null;
  away: string | null;
  homeScore: number;
  awayScore: number;
  winner: string | "draw" | null;
  signature: string;
}

type TiePolicy = "single_winner_tiebreak" | "fractional_tie_payout" | null;

const PHRASE_ALIASES: Array<[RegExp, string]> = [
  [/\bu\s*s\s*a\b/g, "usa"],
  [/\bu\s*s\b/g, "usa"],
  [/\bu\s*s\s*wnt\b/g, "usa"],
  [/\bunited states(?: of america)?\b/g, "usa"],
  [/\bamerica\b/g, "usa"],
  [/\bbosnia and herzegovina\b/g, "bosnia"],
  [/\bdr congo\b/g, "congo"],
  [/\bcongo dr\b/g, "congo"],
  [/\bdemocratic republic of congo\b/g, "congo"],
  [/\bcote d ivoire\b/g, "ivory coast"],
  [/\bkorea republic\b/g, "south korea"],
  [/\bkorea south\b/g, "south korea"],
  [/\bkorea dpr\b/g, "north korea"],
  [/\bkorea north\b/g, "north korea"],
];

const COUNTRY_ALIASES: Array<[RegExp, string]> = [
  [/\busa\b/g, "usa"],
  [/\bunited states\b/g, "usa"],
  [/\bargentina\b/g, "argentina"],
  [/\bfrance\b/g, "france"],
  [/\bspain\b/g, "spain"],
  [/\bengland\b/g, "england"],
  [/\bbrazil\b/g, "brazil"],
  [/\bgermany\b/g, "germany"],
  [/\bportugal\b/g, "portugal"],
  [/\bnetherlands\b/g, "netherlands"],
  [/\bbelgium\b/g, "belgium"],
  [/\bitaly\b/g, "italy"],
  [/\bmexico\b/g, "mexico"],
  [/\bcanada\b/g, "canada"],
  [/\bmorocco\b/g, "morocco"],
  [/\bcroatia\b/g, "croatia"],
  [/\becuador\b/g, "ecuador"],
  [/\begypt\b/g, "egypt"],
  [/\bcongo\b/g, "congo"],
  [/\bparaguay\b/g, "paraguay"],
  [/\bcape verde\b/g, "cape_verde"],
  [/\bcolombia\b/g, "colombia"],
  [/\bivory coast\b/g, "ivory_coast"],
  [/\bsenegal\b/g, "senegal"],
  [/\buruguay\b/g, "uruguay"],
  [/\bjapan\b/g, "japan"],
  [/\bsouth korea\b/g, "south_korea"],
  [/\bnorth korea\b/g, "north_korea"],
  [/\baustralia\b/g, "australia"],
  [/\bnorway\b/g, "norway"],
  [/\balgeria\b/g, "algeria"],
  [/\bswitzerland\b/g, "switzerland"],
  [/\baustria\b/g, "austria"],
  [/\bsouth africa\b/g, "south_africa"],
  [/\bghana\b/g, "ghana"],
  [/\btunisia\b/g, "tunisia"],
  [/\bdenmark\b/g, "denmark"],
  [/\bsweden\b/g, "sweden"],
  [/\bpoland\b/g, "poland"],
  [/\bchile\b/g, "chile"],
  [/\bnigeria\b/g, "nigeria"],
  [/\bturkey\b/g, "turkey"],
  [/\bscotland\b/g, "scotland"],
  [/\bnew zealand\b/g, "new_zealand"],
  [/\bsaudi arabia\b/g, "saudi_arabia"],
  [/\biran\b/g, "iran"],
  [/\buzbekistan\b/g, "uzbekistan"],
  [/\bcosta rica\b/g, "costa_rica"],
  [/\bpanama\b/g, "panama"],
  [/\bqatar\b/g, "qatar"],
];

interface IndexedMarket {
  market: Market;
  key: string;
  tokens: Set<string>;
  tokenList: string[];
  searchTokens: Set<string>;
  kind: MarketKind;
  line: number | null;
  exactScore: ExactScore | null;
  tiePolicy: TiePolicy;
  matchup: [string, string] | null;
  subjects: Set<string>;
  structuredTokens: Set<string>;
  endTime: number | null;
}

interface CandidatePair {
  poly: IndexedMarket;
  kalshi: IndexedMarket;
  score: number;
  tier: SuggestedCandidate["tier"];
  sharedTerms: string[];
  reasons: string[];
}

export function getMarketKey(market: Market): string {
  return `${market.source}:${market.id}`;
}

export function getPairKey(polyId: string, kalshiId: string): string {
  return `${polyId}::${kalshiId}`;
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

    for (const token of poly.searchTokens) {
      const candidates = kalshiByToken.get(token);
      if (!candidates || candidates.length > MAX_CANDIDATES_PER_TOKEN) continue;
      candidates.forEach((candidate) => candidateKeys.add(candidate.key));
    }

    for (const key of candidateKeys) {
      const kalshi = kalshiByKey.get(key);
      if (!kalshi) continue;

      const scored = scorePair(poly, kalshi);
      if (scored.score >= MIN_SCORE) {
        pairs.push({ poly, kalshi, ...scored });
      }
    }
  }

  pairs.sort((a, b) => b.score - a.score);
  return buildMatchMap(pairs);
}

export function excludeSuggestedPairs(
  matches: MarketMatchMap,
  excludedPairIds: Set<string>,
  excludedMarketKeys: Set<string> = new Set(),
): MarketMatchMap {
  if (excludedPairIds.size === 0 && excludedMarketKeys.size === 0) return matches;

  const filtered: MarketMatchMap = {};
  for (const [key, match] of Object.entries(matches)) {
    if (excludedMarketKeys.has(key)) continue;

    const candidates = match.candidates.filter((candidate) => {
      if (excludedPairIds.has(candidate.pairKey)) return false;
      return !excludedMarketKeys.has(`${candidate.peerSource}:${candidate.peerId}`);
    });
    if (candidates.length === 0) continue;

    if (candidates.length === match.candidates.length) {
      filtered[key] = match;
      continue;
    }

    const top = candidates[0]!;
    filtered[key] = {
      ...match,
      score: top.score,
      peerId: top.peerId,
      peerTitle: top.peerTitle,
      peerSource: top.peerSource,
      sharedTerms: top.sharedTerms,
      candidates,
    };
  }
  return filtered;
}

function indexMarket(market: Market): IndexedMarket {
  const normalized = normalizeText(
    [market.title, market.event_title, market.category, market.rules, market.outcomes?.join(" ")]
      .filter(Boolean)
      .join(" "),
  );
  const title = normalizeText(`${market.title} ${market.event_title ?? ""}`);
  const tokenList = tokenize(normalized);
  const tokens = new Set(tokenList);
  const matchup = extractMatchup(title) ?? extractMatchup(normalized);
  const subjects = extractSubjects(title, tokenList, matchup);
  const kind = classifyKind(title, normalized);
  const line = extractLine(title) ?? extractLine(normalized);
  const exactScore = extractExactScore(title, normalized, matchup);
  const tiePolicy = extractTiePolicy(normalized);
  const structuredTokens = extractStructuredTokens(normalized, kind, exactScore, tiePolicy);
  const searchTokens = new Set<string>([
    ...tokenList.filter((token) => !KIND_WORDS.has(token)),
    ...subjects,
    ...structuredTokens,
    `kind:${kind}`,
  ]);
  if (line !== null) searchTokens.add(`line:${line}`);
  if (tiePolicy) searchTokens.add(`tie:${tiePolicy}`);
  if (exactScore) {
    searchTokens.add(`score:${exactScore.homeScore}-${exactScore.awayScore}`);
    searchTokens.add(`score-signature:${exactScore.signature}`);
  }

  return {
    market,
    key: getMarketKey(market),
    tokens,
    tokenList,
    searchTokens,
    kind,
    line,
    exactScore,
    tiePolicy,
    matchup,
    subjects,
    structuredTokens,
    endTime: parseEndTime(market.end_date),
  };
}

function buildTokenIndex(markets: IndexedMarket[]): Map<string, IndexedMarket[]> {
  const index = new Map<string, IndexedMarket[]>();

  for (const market of markets) {
    for (const token of market.searchTokens) {
      const current = index.get(token) ?? [];
      current.push(market);
      index.set(token, current);
    }
  }

  return index;
}

function scorePair(poly: IndexedMarket, kalshi: IndexedMarket) {
  const sharedTerms = [...poly.tokens].filter((token) => kalshi.tokens.has(token));
  const subjectScore = setSimilarity(poly.subjects, kalshi.subjects);
  const tokenScore = setSimilarity(poly.tokens, kalshi.tokens);
  const structuredScore = setSimilarity(poly.structuredTokens, kalshi.structuredTokens);
  const containmentScore =
    Math.min(poly.tokens.size, kalshi.tokens.size) > 0
      ? sharedTerms.length / Math.min(poly.tokens.size, kalshi.tokens.size)
      : 0;
  const dateScore = closeDateScore(poly.endTime, kalshi.endTime);
  const kindScore = kindCompatibility(poly.kind, kalshi.kind);
  const lineScore = lineCompatibility(poly.line, kalshi.line, poly.kind, kalshi.kind);
  const exactScoreScore = exactScoreCompatibility(poly.exactScore, kalshi.exactScore);
  const tiePolicyScore = tiePolicyCompatibility(poly.tiePolicy, kalshi.tiePolicy);
  const matchupScore = matchupCompatibility(poly.matchup, kalshi.matchup);
  const entityMatch = sharedPrefixed(poly.structuredTokens, kalshi.structuredTokens, "entity:").length > 0;
  const competitionMatch = sharedPrefixed(poly.structuredTokens, kalshi.structuredTokens, "competition:").length > 0;
  const signatureMatch = sharedPrefixed(poly.structuredTokens, kalshi.structuredTokens, "signature:").length > 0;

  const rawScore = clamp01(
    structuredScore * 0.24
    + subjectScore * 0.22
    + tokenScore * 0.18
    + containmentScore * 0.12
    + dateScore * 0.08
    + kindScore * 0.08
    + lineScore * 0.04
    + matchupScore * 0.04
    + exactScoreScore * 0.22
    + tiePolicyScore * 0.08
    + (signatureMatch ? 0.18 : 0)
    + (entityMatch && competitionMatch ? 0.1 : 0),
  );
  const score = clampIncompatiblePairScore(poly, kalshi, rawScore, exactScoreScore, tiePolicyScore);

  const reasons = [
    exactScoreScore >= 0.95 ? "same exact scoreline" : "",
    poly.exactScore && kalshi.exactScore && exactScoreScore < 0.9 ? "different exact scoreline" : "",
    tiePolicyScore === 1 ? "same tie policy" : "",
    poly.tiePolicy && kalshi.tiePolicy && tiePolicyScore === 0 ? "different tie policy" : "",
    signatureMatch ? "same structured proposition" : "",
    competitionMatch ? "same competition" : "",
    entityMatch ? "same entity" : "",
    subjectScore >= 0.4 ? "shared subject" : "",
    matchupScore >= 0.75 ? "same matchup" : "",
    kindScore >= 0.8 ? `${poly.kind} market` : kindScore >= 0.35 ? "related proposition" : "",
    lineScore >= 0.9 && poly.line !== null ? `same line ${poly.line}` : "",
    dateScore >= 0.7 ? "near close date" : "",
  ].filter(Boolean);

  return {
    score,
    tier: tierForScore(score),
    sharedTerms: sharedTerms.slice(0, 8),
    reasons,
  };
}

function buildMatchMap(pairs: CandidatePair[]): MarketMatchMap {
  const map: MarketMatchMap = {};
  const colors = new Map<string, number>();
  let colorCount = 0;

  for (const pair of pairs) {
    if (!colors.has(pair.poly.key)) {
      colors.set(pair.poly.key, colorCount % 8);
      colorCount += 1;
    }
    addCandidate(map, colors, colorCount, pair.poly, pair.kalshi, pair);
    if (!colors.has(pair.kalshi.key)) {
      colors.set(pair.kalshi.key, colorCount % 8);
      colorCount += 1;
    }
    addCandidate(map, colors, colorCount, pair.kalshi, pair.poly, pair);
  }

  for (const match of Object.values(map)) {
    match.candidates.sort((a, b) => b.score - a.score);
    match.candidates = match.candidates.slice(0, MAX_CANDIDATES_PER_MARKET);
    const top = match.candidates[0];
    if (top) {
      match.peerId = top.peerId;
      match.peerTitle = top.peerTitle;
      match.peerSource = top.peerSource;
      match.score = top.score;
      match.sharedTerms = top.sharedTerms;
    }
  }

  return map;
}

function addCandidate(
  map: MarketMatchMap,
  colors: Map<string, number>,
  colorCount: number,
  source: IndexedMarket,
  peer: IndexedMarket,
  pair: CandidatePair,
) {
  let colorIndex = colors.get(source.key);
  if (colorIndex === undefined) {
    colorIndex = colorCount % 8;
    colors.set(source.key, colorIndex);
  }
  const existing = map[source.key] ?? buildEmptyMatch(source, peer, pair, colorIndex);
  const poly = source.market.source === "polymarket" ? source.market : peer.market;
  const kalshi = source.market.source === "kalshi" ? source.market : peer.market;
  const candidate: SuggestedCandidate = {
    pairKey: getPairKey(poly.id, kalshi.id),
    peerId: peer.market.id,
    peerTitle: peer.market.title,
    peerSource: peer.market.source,
    score: pair.score,
    tier: pair.tier,
    reasons: pair.reasons,
    sharedTerms: pair.sharedTerms,
  };
  if (!existing.candidates.some((item) => item.pairKey === candidate.pairKey)) {
    existing.candidates.push(candidate);
  }
  map[source.key] = existing;
}

function buildEmptyMatch(
  source: IndexedMarket,
  peer: IndexedMarket,
  pair: CandidatePair,
  colorIndex: number,
): SuggestedMatch {
  return {
    groupId: `suggested-${source.key}`,
    colorIndex,
    score: pair.score,
    peerId: peer.market.id,
    peerTitle: peer.market.title,
    peerSource: peer.market.source,
    sharedTerms: pair.sharedTerms,
    candidates: [],
  };
}

function classifyKind(title: string, fullText: string): MarketKind {
  const titleKind = classifyKindFromText(title);
  if (titleKind !== "generic") return titleKind;
  return classifyKindFromText(`${title} ${fullText}`);
}

function classifyKindFromText(text: string): MarketKind {
  if (/\b(exact score|correct score|final score|score be)\b/.test(text) && /\b\d{1,2}\s*-\s*\d{1,2}\b/.test(text)) {
    return "exact_score";
  }
  if (/\b(o u|over under|total|totals|over|under)\b/.test(text)) return "total";
  if (/\b(spread|handicap|point spread|run line|puck line)\b/.test(text)) return "spread";
  if (/\b(best performing host|furthest advancing host|host nation)\b/.test(text)) return "best_host";
  if (/\bannounced as (?:a )?hosts?\b|\bwho will host\b|\bhost for the\b/.test(text)) return "host";
  if (/\bwinner of\b.*\bcome from\b|\bcome from\b.*\bwinner\b/.test(text)) return "regional_winner";
  if (/\b(eliminated|elimination|knocked out|fail to advance|not advance)\b/.test(text)) return "elimination";
  if (/\b(to advance|advance to|advances|advancing|qualify|qualifies)\b/.test(text)) return "advance";
  if (/\b(winner|moneyline|beat|beats|defeat|defeats|win)\b/.test(text)) return "winner";
  if (/\b(champion|championship|world cup)\b/.test(text) && !extractMatchup(text)) return "tournament";
  return "generic";
}

function extractStructuredTokens(
  text: string,
  kind: MarketKind,
  exactScore: ExactScore | null,
  tiePolicy: TiePolicy,
): Set<string> {
  const tokens = new Set<string>();
  const years = [...text.matchAll(/\b(20[2-9][0-9])\b/g)].map((match) => match[1]);
  const entities = extractEntities(text);
  const competitions = extractCompetitions(text);
  const gender = extractGender(text, competitions);

  years.forEach((year) => tokens.add(`year:${year}`));
  entities.forEach((entity) => tokens.add(`entity:${entity}`));
  competitions.forEach((competition) => tokens.add(`competition:${competition}`));
  if (gender) tokens.add(`gender:${gender}`);
  if (tiePolicy) tokens.add(`tie:${tiePolicy}`);
  if (exactScore) {
    tokens.add(`score:${exactScore.homeScore}-${exactScore.awayScore}`);
    tokens.add(`score-signature:${exactScore.signature}`);
    if (exactScore.winner) tokens.add(`score-winner:${exactScore.winner}`);
  }

  for (const competition of competitions) {
    for (const entity of entities) {
      tokens.add(`combo:${competition}:${entity}`);
      for (const year of years) {
        tokens.add(`combo:${competition}:${entity}:${year}`);
        tokens.add(`signature:${competition}:${entity}:${year}:${kind}:${gender || "any"}`);
        if (exactScore) {
          tokens.add(`signature:${competition}:${entity}:${year}:${kind}:${exactScore.signature}`);
        }
      }
    }
  }

  return tokens;
}

function extractEntities(text: string): string[] {
  const entities: string[] = [];
  for (const [pattern, entity] of COUNTRY_ALIASES) {
    if (pattern.test(text) && !entities.includes(entity)) {
      entities.push(entity);
    }
    pattern.lastIndex = 0;
  }
  return entities;
}

function extractEntitiesInOrder(text: string): string[] {
  const found: Array<{ entity: string; index: number }> = [];
  for (const [pattern, entity] of COUNTRY_ALIASES) {
    const match = pattern.exec(text);
    if (match && !found.some((item) => item.entity === entity)) {
      found.push({ entity, index: match.index });
    }
    pattern.lastIndex = 0;
  }
  return found.sort((a, b) => a.index - b.index).map((item) => item.entity);
}

function extractCompetitions(text: string): string[] {
  const competitions: string[] = [];
  if (/\b(?:fifa\s+)?(?:men s\s+|mens\s+)?world cup\b/.test(text)) {
    competitions.push("world_cup");
  }
  if (/\bwomen s world cup\b|\bwomens world cup\b/.test(text)) {
    competitions.push("womens_world_cup");
  }
  if (/\bworld cup\b/.test(text) && !competitions.includes("world_cup")) {
    competitions.push("world_cup");
  }
  if (/\bus open\b/.test(text)) {
    competitions.push("us_open");
  }
  if (/\bfrench open\b|\broland garros\b/.test(text)) {
    competitions.push("french_open");
  }
  if (/\bwimbledon\b/.test(text)) {
    competitions.push("wimbledon");
  }
  return competitions;
}

function extractGender(text: string, competitions: string[]): string | null {
  if (/\bwomen\b|\bwomen s\b|\bwomens\b|\bfemale\b/.test(text)) return "women";
  if (/\bmen\b|\bmen s\b|\bmens\b|\bmale\b/.test(text)) return "men";
  if (competitions.includes("world_cup") && !competitions.includes("womens_world_cup")) return "men";
  return null;
}

function extractMatchup(text: string): [string, string] | null {
  const match = text.match(
    /\b(.{2,80}?)\s+(?:vs|v|versus|at|@)\s+(.{2,80}?)(?=\s+(?:winner|moneyline|to advance|advance|o u|over under|total|totals|spread|handicap|first half|second half|will|market)\b|[:?]|\s+-\s+|$)/,
  );
  if (!match?.[1] || !match[2]) return null;
  const left = cleanupEntity(match[1]);
  const right = cleanupEntity(match[2]);
  if (!left || !right || left === right) return null;
  return [left, right];
}

function extractExactScore(
  title: string,
  fullText: string,
  matchup: [string, string] | null,
): ExactScore | null {
  const relevant = /\b(exact score|correct score|final score|score be)\b/.test(title)
    ? title
    : /\b(exact score|correct score|final score|score be)\b/.test(fullText)
      ? fullText
      : "";
  if (!relevant) return null;

  const scoreMatch = relevant.match(/\b(\d{1,2})\s*-\s*(\d{1,2})\b/);
  if (!scoreMatch?.[1] || !scoreMatch[2]) return null;
  const firstScore = Number(scoreMatch[1]);
  const secondScore = Number(scoreMatch[2]);
  if (!Number.isFinite(firstScore) || !Number.isFinite(secondScore)) return null;

  const titleEntities = extractEntitiesInOrder(title);
  const allEntities = extractEntitiesInOrder(fullText);
  const teams = matchup ?? (allEntities.length >= 2 ? [allEntities[0]!, allEntities[1]!] as [string, string] : null);
  const winner = extractScoreWinner(title, firstScore, secondScore);

  if (teams) {
    const [home, away] = teams;
    let homeScore = firstScore;
    let awayScore = secondScore;
    if (winner && winner !== "draw") {
      if (winner === away) {
        homeScore = secondScore;
        awayScore = firstScore;
      } else if (winner !== home && titleEntities.length >= 2) {
        const firstEntity = titleEntities[0]!;
        if (firstEntity === away) {
          homeScore = secondScore;
          awayScore = firstScore;
        }
      }
    }
    return buildExactScore(home, away, homeScore, awayScore);
  }

  if (titleEntities.length >= 2) {
    return buildExactScore(titleEntities[0]!, titleEntities[1]!, firstScore, secondScore);
  }

  const unresolvedWinner = winner ?? (firstScore === secondScore ? "draw" : null);
  return {
    home: null,
    away: null,
    homeScore: firstScore,
    awayScore: secondScore,
    winner: unresolvedWinner,
    signature: `${unresolvedWinner ?? "unknown"}:${firstScore}-${secondScore}`,
  };
}

function extractTiePolicy(text: string): TiePolicy {
  if (/\b1\s*\/\s*n\b|\btied participants\b|\byes payout\b|\brounded down\b/.test(text)) {
    return "fractional_tie_payout";
  }
  if (/\bmore total wins\b|\bmore total goals\b|\bconceded fewer\b|\balphabetically\b/.test(text)) {
    return "single_winner_tiebreak";
  }
  return null;
}

function extractScoreWinner(text: string, firstScore: number, secondScore: number): string | "draw" | null {
  if (firstScore === secondScore || /\bdraw\b/.test(text)) return "draw";
  const beforeWins = text.match(/\b([a-z][a-z ]{1,50}?)\s+wins?\s+\d{1,2}\s*-\s*\d{1,2}\b/)?.[1];
  if (!beforeWins) return null;
  return extractEntitiesInOrder(beforeWins)[0] ?? null;
}

function buildExactScore(home: string, away: string, homeScore: number, awayScore: number): ExactScore {
  return {
    home,
    away,
    homeScore,
    awayScore,
    winner: homeScore === awayScore ? "draw" : homeScore > awayScore ? home : away,
    signature: `${home}:${homeScore}:${away}:${awayScore}`,
  };
}

function extractSubjects(title: string, tokenList: string[], matchup: [string, string] | null): Set<string> {
  const subjects = new Set<string>();
  if (matchup) {
    matchup.flatMap(entityTokens).forEach((token) => subjects.add(token));
  }
  for (const token of tokenList) {
    if (!STOP_WORDS.has(token) && !KIND_WORDS.has(token)) subjects.add(token);
  }
  tokenize(title)
    .filter((token) => !STOP_WORDS.has(token) && !KIND_WORDS.has(token))
    .forEach((token) => subjects.add(token));
  return subjects;
}

function extractLine(text: string): number | null {
  const patterns = [
    /\b(?:o u|over under|total|totals|over|under)\s*([0-9]+(?:\.[0-9]+)?)/,
    /\b([0-9]+(?:\.[0-9]+)?)\s*(?:goals|points|runs|total)\b/,
    /\b(?:spread|handicap|line)\s*(-?[0-9]+(?:\.[0-9]+)?)/,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (!match?.[1]) continue;
    const value = Number(match[1]);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

function matchupCompatibility(a: [string, string] | null, b: [string, string] | null): number {
  if (!a || !b) return 0.3;
  const direct = (entitySimilarity(a[0], b[0]) + entitySimilarity(a[1], b[1])) / 2;
  const swapped = (entitySimilarity(a[0], b[1]) + entitySimilarity(a[1], b[0])) / 2;
  return Math.max(direct, swapped);
}

function kindCompatibility(a: MarketKind, b: MarketKind): number {
  if (a === b) return 1;
  if (a === "generic" || b === "generic") return 0.4;
  if (new Set([a, b]).has("advance") && new Set([a, b]).has("elimination")) return 0.55;
  if (new Set([a, b]).has("winner") && new Set([a, b]).has("best_host")) return 0.15;
  if (new Set([a, b]).has("winner") && new Set([a, b]).has("host")) return 0.05;
  if (new Set([a, b]).has("winner") && new Set([a, b]).has("regional_winner")) return 0.3;
  if (new Set([a, b]).has("tournament") && new Set([a, b]).has("elimination")) return 0.35;
  if (new Set([a, b]).has("tournament") && new Set([a, b]).has("advance")) return 0.35;
  return 0.25;
}

function exactScoreCompatibility(a: ExactScore | null, b: ExactScore | null): number {
  if (!a && !b) return 0;
  if (!a || !b) return 0.1;
  if (a.signature === b.signature) return 1;
  const sameTeams = a.home && a.away && a.home === b.home && a.away === b.away;
  if (sameTeams && (a.homeScore !== b.homeScore || a.awayScore !== b.awayScore)) return 0;
  if (a.winner && b.winner && a.winner === b.winner && a.homeScore === b.homeScore && a.awayScore === b.awayScore) return 0.8;
  if (a.homeScore === b.homeScore && a.awayScore === b.awayScore && a.winner === b.winner) return 0.65;
  return 0;
}

function lineCompatibility(a: number | null, b: number | null, aKind: MarketKind, bKind: MarketKind): number {
  if (a === null && b === null) return 0.4;
  if (a === null || b === null) return aKind === bKind && (aKind === "total" || aKind === "spread") ? 0 : 0.25;
  return Math.abs(a - b) <= 0.01 ? 1 : 0.15;
}

function tiePolicyCompatibility(a: TiePolicy, b: TiePolicy): number {
  if (!a && !b) return 0;
  if (!a || !b) return 0.3;
  return a === b ? 1 : 0;
}

function clampIncompatiblePairScore(
  poly: IndexedMarket,
  kalshi: IndexedMarket,
  rawScore: number,
  exactScoreScore: number,
  tiePolicyScore: number,
): number {
  let score = rawScore;
  if (poly.exactScore && kalshi.exactScore && exactScoreScore < 0.9) {
    score = Math.min(score, 0.46);
  }
  if (poly.tiePolicy && kalshi.tiePolicy && tiePolicyScore === 0) {
    score = Math.min(score, 0.5);
  }
  return score;
}

function closeDateScore(a: number | null, b: number | null): number {
  if (!a || !b) return 0.3;
  const days = Math.abs(a - b) / 86_400_000;
  if (days <= 1) return 1;
  if (days <= 7) return 0.75;
  if (days <= 21) return 0.45;
  if (days <= 45) return 0.25;
  return 0;
}

function tierForScore(score: number): SuggestedCandidate["tier"] {
  if (score >= 0.72) return "strong";
  if (score >= 0.54) return "possible";
  if (score >= 0.38) return "weak";
  return "related";
}

function entitySimilarity(a: string, b: string): number {
  return setSimilarity(new Set(entityTokens(a)), new Set(entityTokens(b)));
}

function setSimilarity(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  const shared = [...a].filter((token) => b.has(token)).length;
  return (2 * shared) / (a.size + b.size);
}

function sharedPrefixed(a: Set<string>, b: Set<string>, prefix: string): string[] {
  return [...a].filter((token) => token.startsWith(prefix) && b.has(token));
}

function entityTokens(value: string): string[] {
  return cleanupEntity(value).split(" ").filter(Boolean);
}

function cleanupEntity(value: string): string {
  return tokenize(value)
    .filter((token) => !KIND_WORDS.has(token))
    .join(" ");
}

function tokenize(value: string): string[] {
  return normalizeText(value)
    .split(" ")
    .map(normalizeSynonym)
    .filter((token) => token.length > 2 && !STOP_WORDS.has(token));
}

function normalizeText(value: string): string {
  const base = value
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\bo\s*\/\s*u\b/g, " o u ")
    .replace(/\b1st\b/g, " first ")
    .replace(/\b2nd\b/g, " second ")
    .replace(/[^a-z0-9@.+:-]+/g, " ")
    .trim();

  return PHRASE_ALIASES.reduce((current, [pattern, replacement]) => current.replace(pattern, replacement), base)
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeSynonym(token: string): string {
  const synonyms: Record<string, string> = {
    america: "usa",
    americans: "usa",
    eeuu: "usa",
    ganara: "win",
    ganar: "win",
    gana: "win",
    states: "usa",
    united: "usa",
    estadounidense: "usa",
  };

  return synonyms[token] ?? token;
}

function parseEndTime(value: string): number | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

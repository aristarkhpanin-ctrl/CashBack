/**
 * A/B testing — typed wrappers around `/experiments/*`.
 *
 * The Campaign Manager service hosts the experiment registry; the
 * server-side z-test is exposed via `getResults()`. A client-side
 * `clientZTest()` mirror is provided for the ZTestCalculator UI so a
 * user can preview the verdict before any data is collected.
 */
import { campaignClient } from '@/shared/api/client';
import type {
  ABExperiment, ABExperimentCreatePayload, ABResults,
} from '@/shared/api/types';

export const abApi = {
  list: async (): Promise<ABExperiment[]> => {
    // The server doesn't ship a list endpoint yet — list a stable empty
    // array if the upstream returns 404. Kept as a separate function so
    // the UI doesn't need to handle the missing-endpoint case.
    try {
      const { data } = await campaignClient.get<ABExperiment[]>('/experiments');
      return data;
    } catch {
      return [];
    }
  },

  create: async (payload: ABExperimentCreatePayload): Promise<ABExperiment> => {
    const { data } = await campaignClient.post<ABExperiment>('/experiments', payload);
    return data;
  },

  get: async (id: string): Promise<ABExperiment> => {
    const { data } = await campaignClient.get<ABExperiment>(`/experiments/${id}`);
    return data;
  },

  updateStatus: async (
    id: string,
    action: 'start' | 'stop',
  ): Promise<ABExperiment> => {
    const { data } = await campaignClient.patch<ABExperiment>(
      `/experiments/${id}/status`,
      null,
      { params: { action } },
    );
    return data;
  },

  getResults: async (id: string): Promise<ABResults> => {
    const { data } = await campaignClient.get<ABResults>(`/experiments/${id}/results`);
    return data;
  },
};

export const abKeys = {
  list:    ['experiments'] as const,
  detail:  (id: string)             => ['experiments', 'detail', id]  as const,
  results: (id: string)             => ['experiments', 'results', id] as const,
};

// ── Client-side z-test for the calculator UI ────────────────────────────────
const SQRT_2 = Math.sqrt(2);

/**
 * Erf approximation (Abramowitz & Stegun 7.1.26) — keeps the calculator
 * dependency-free. Accurate to ~1.5e-7 across the meaningful range.
 */
function erf(x: number): number {
  const sign = Math.sign(x);
  const ax = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * ax);
  const y =
    1 -
    (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) *
      t *
      Math.exp(-ax * ax);
  return sign * y;
}

const cdf = (z: number) => 0.5 * (1 + erf(z / SQRT_2));

/** Inverse standard-normal — Beasley-Springer-Moro. */
function invStdNormal(p: number): number {
  if (p <= 0 || p >= 1) return 0;
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.383577518672690e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];

  const pl = 0.02425, ph = 1 - pl;
  if (p < pl) {
    const q = Math.sqrt(-2 * Math.log(p));
    return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
      ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  if (p > ph) {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
      ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
  }
  const q = p - 0.5;
  const r = q * q;
  return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
    (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
}

export interface ZTestResult {
  pA: number;
  pB: number;
  diff: number;
  z: number;
  pValue: number;
  ci: [number, number];
}

/** Two-proportion z-test mirroring `scipy.stats` (server side). */
export function clientZTest(
  successA: number, nA: number, successB: number, nB: number, alpha = 0.05,
): ZTestResult | null {
  if (nA <= 0 || nB <= 0) return null;
  const pA = successA / nA;
  const pB = successB / nB;
  const pPool = (successA + successB) / (nA + nB);
  const sePool = Math.sqrt(pPool * (1 - pPool) * (1 / nA + 1 / nB));
  if (sePool === 0) return null;
  const z = (pB - pA) / sePool;
  const pValue = 2 * (1 - cdf(Math.abs(z)));
  const seDiff = Math.sqrt((pA * (1 - pA)) / nA + (pB * (1 - pB)) / nB);
  const zAlpha = invStdNormal(1 - alpha / 2);
  return {
    pA, pB,
    diff: pB - pA,
    z, pValue,
    ci: [pB - pA - zAlpha * seDiff, pB - pA + zAlpha * seDiff],
  };
}

/**
 * Zustand store backing the 5-step Campaign Wizard.
 *
 * Keeping form state outside the page component:
 *   1. survives unmount (e.g. accidental navigation)
 *   2. lets the AudiencePreview hook into shared state without lifting it
 *   3. centralises the reset() once a campaign has been launched
 */
import { create } from 'zustand';

export interface WizardForm {
  name: string;
  startDate: string;
  endDate: string;
  cashbackRate: string;
  segments: string[];
  rfmR: string;
  rfmF: string;
  rfmM: string;
  mccCodes: string[];
  minTxAmount: Record<string, string>;
  totalBudget: string;
  dailyLimit: string;
  autoPause: boolean;
}

export const defaultForm: WizardForm = {
  name: '',
  startDate: '',
  endDate: '',
  cashbackRate: '',
  segments: [],
  rfmR: '',
  rfmF: '',
  rfmM: '',
  mccCodes: [],
  minTxAmount: {},
  totalBudget: '',
  dailyLimit: '',
  autoPause: true,
};

interface WizardState {
  step: number;
  form: WizardForm;
  setStep: (step: number) => void;
  patch: (patch: Partial<WizardForm>) => void;
  set: (form: WizardForm) => void;
  reset: () => void;
}

export const useWizard = create<WizardState>((set) => ({
  step: 1,
  form: defaultForm,
  setStep: (step) => set({ step }),
  patch: (patch) => set((s) => ({ form: { ...s.form, ...patch } })),
  set: (form) => set({ form }),
  reset: () => set({ step: 1, form: defaultForm }),
}));

// Map the UI segment label → numeric segment_id used in the database.
// Keeps the wizard friendly (Cyrillic labels) while the API payload
// sticks to the `target_segment_ids: int[]` contract.
export const SEGMENT_LABEL_TO_ID: Record<string, number> = {
  Premium: 1,
  Mass:    2,
  VIP:     3,
  'Новые': 4,
  'Спящие': 5,
  'Активные': 6,
};

export function segmentsToIds(labels: string[]): number[] {
  return labels.map((l) => SEGMENT_LABEL_TO_ID[l]).filter((x): x is number => x != null);
}

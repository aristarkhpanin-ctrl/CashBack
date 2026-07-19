// @ts-nocheck
/* eslint-disable */
// Единый SVG-иконочный сет (фаза 27): kebab-имя → Lucide-компонент.
// Заменяет emoji прототипа. Имена MCC-иконок приходят с бэкенда (фаза 21).
import React from "react";
import {
  ShoppingCart, Pill, Fuel, Utensils, Store, Hotel, Bus, Laptop, Shirt,
  Clapperboard, Hammer, Sparkles, Megaphone, Users, Banknote, Target,
  CircleCheck, CreditCard, Wallet, Trophy, TrendingUp, TriangleAlert,
  ShieldCheck, ChartColumn, Zap, Hourglass, Rocket, Tag,
} from "lucide-react";

const REGISTRY = {
  // MCC-категории (имена из reference_data.py)
  "shopping-cart": ShoppingCart, "pill": Pill, "fuel": Fuel, "utensils": Utensils,
  "store": Store, "hotel": Hotel, "bus": Bus, "laptop": Laptop, "shirt": Shirt,
  "clapperboard": Clapperboard, "hammer": Hammer, "sparkles": Sparkles,
  // KPI / инсайты / интро
  "megaphone": Megaphone, "users": Users, "banknote": Banknote, "target": Target,
  "circle-check": CircleCheck, "credit-card": CreditCard, "wallet": Wallet,
  "trophy": Trophy, "trending-up": TrendingUp, "triangle-alert": TriangleAlert,
  "shield-check": ShieldCheck, "chart-column": ChartColumn, "zap": Zap,
  "hourglass": Hourglass, "rocket": Rocket,
};

export function Icon({ name, size = 18, color = "currentColor", strokeWidth = 1.7, style }) {
  const Cmp = REGISTRY[name] || Tag;
  return <Cmp size={size} color={color} strokeWidth={strokeWidth} style={style} />;
}

export default Icon;

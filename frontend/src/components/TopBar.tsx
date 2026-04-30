import { Bell, Search, ChevronDown } from 'lucide-react';

interface TopBarProps {
  title?: string;
}

export default function TopBar({ title = `Дашборд` }: TopBarProps) {
  return (
    <header
      data-cmp="TopBar"
      className="flex items-center justify-between px-8 py-4 bg-card border-b border-border"
      style={{ minHeight: 64 }}
    >
      <h1 className="text-xl font-semibold text-foreground tracking-tight">{title}</h1>
      <div className="flex items-center gap-4">
        {/* Search */}
        <div className="relative hidden md:flex items-center">
          <Search size={15} className="absolute left-3 text-muted-foreground" />
          <input
            type="text"
            placeholder={`Поиск...`}
            className="pl-9 pr-4 py-2 text-sm rounded-lg border border-border bg-muted focus:outline-none focus:ring-2 focus:ring-ring"
            style={{ width: 220 }}
          />
        </div>

        {/* Notifications */}
        <button className="relative p-2 rounded-lg hover:bg-accent transition-colors">
          <Bell size={18} className="text-muted-foreground" />
          <span
            className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full"
            style={{ background: `var(--destructive)` }}
          />
        </button>

        {/* Avatar */}
        <div className="flex items-center gap-2 cursor-pointer hover:bg-accent rounded-lg px-2 py-1 transition-colors">
          <div
            className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-semibold text-primary-foreground"
            style={{ background: `var(--primary)` }}
          >
            АА
          </div>
          <span className="text-sm font-medium text-foreground hidden lg:block">Анна Алёхина</span>
          <ChevronDown size={14} className="text-muted-foreground hidden lg:block" />
        </div>
      </div>
    </header>
  );
}

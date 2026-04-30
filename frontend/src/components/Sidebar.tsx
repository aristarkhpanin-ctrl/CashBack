import { LayoutDashboard, Megaphone, BarChart3, FlaskConical, Settings, ChevronLeft, ChevronRight, CreditCard } from 'lucide-react';
import { useNavigate, useLocation } from 'react-router-dom';

interface SidebarProps {
  collapsed?: boolean;
  onToggle?: () => void;
}

const navItems = [
  { label: `Дашборд`, icon: LayoutDashboard, path: `/` },
  { label: `Кампании`, icon: Megaphone, path: `/campaigns` },
  { label: `Аналитика`, icon: BarChart3, path: `/analytics` },
  { label: `Эксперименты`, icon: FlaskConical, path: `/experiments` },
];

export default function Sidebar({ collapsed = false, onToggle = () => {} }: SidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <aside
      data-cmp="Sidebar"
      className="flex flex-col h-screen sticky top-0 transition-all duration-200"
      style={{
        width: collapsed ? 64 : 240,
        minWidth: collapsed ? 64 : 240,
        background: `var(--sidebar)`,
      }}
    >
      {/* Logo */}
      <div className="flex items-center px-4 py-5 border-b" style={{ borderColor: `var(--sidebar-border)` }}>
        <div className="flex items-center justify-center w-8 h-8 rounded-lg" style={{ background: `var(--primary)` }}>
          <CreditCard size={16} color="#fff" />
        </div>
        <span
          className="ml-3 font-semibold text-sm tracking-wide overflow-hidden transition-all duration-200"
          style={{
            color: `var(--sidebar-primary-foreground)`,
            width: collapsed ? 0 : `auto`,
            opacity: collapsed ? 0 : 1,
            whiteSpace: `nowrap`,
          }}
        >
          CashbackAdmin
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2 py-4 flex flex-col gap-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = location.pathname === item.path;
          return (
            <button
              key={item.path}
              onClick={() => navigate(item.path)}
              className={`flex items-center rounded-lg px-3 py-2.5 w-full text-left transition-all duration-150 ${active ? `sidebar-item-active` : `sidebar-item`}`}
            >
              <Icon size={18} style={{ minWidth: 18, color: active ? `#fff` : `var(--sidebar-foreground)` }} />
              <span
                className="ml-3 text-sm font-medium overflow-hidden transition-all duration-200"
                style={{
                  width: collapsed ? 0 : `auto`,
                  opacity: collapsed ? 0 : 1,
                  whiteSpace: `nowrap`,
                }}
              >
                {item.label}
              </span>
            </button>
          );
        })}
      </nav>

      {/* Settings + toggle */}
      <div className="px-2 pb-4 flex flex-col gap-1">
        <button className="sidebar-item flex items-center rounded-lg px-3 py-2.5 w-full text-left">
          <Settings size={18} style={{ minWidth: 18 }} />
          <span
            className="ml-3 text-sm font-medium overflow-hidden transition-all duration-200"
            style={{ width: collapsed ? 0 : `auto`, opacity: collapsed ? 0 : 1, whiteSpace: `nowrap` }}
          >
            Настройки
          </span>
        </button>

        <button
          onClick={onToggle}
          className="sidebar-item flex items-center rounded-lg px-3 py-2.5 w-full text-left"
        >
          {collapsed
            ? <ChevronRight size={18} style={{ minWidth: 18 }} />
            : <ChevronLeft size={18} style={{ minWidth: 18 }} />
          }
          <span
            className="ml-3 text-sm font-medium overflow-hidden transition-all duration-200"
            style={{ width: collapsed ? 0 : `auto`, opacity: collapsed ? 0 : 1, whiteSpace: `nowrap` }}
          >
            Свернуть
          </span>
        </button>
      </div>
    </aside>
  );
}

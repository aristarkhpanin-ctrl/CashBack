import { useNavigate } from 'react-router-dom';

export default function NotFound() {
  const navigate = useNavigate();
  return (
    <div className="flex flex-col items-center justify-center h-screen bg-background">
      <h1 className="text-5xl font-extrabold text-foreground mb-4">404</h1>
      <p className="text-muted-foreground mb-6">Страница не найдена</p>
      <button
        onClick={() => navigate(`/`)}
        className="px-5 py-2.5 rounded-lg text-sm font-semibold text-primary-foreground"
        style={{ background: `var(--primary)` }}
      >
        На главную
      </button>
    </div>
  );
}

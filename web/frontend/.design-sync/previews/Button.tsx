import { Button } from "frontend";

export const Variants = () => (
  <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
    <Button variant="primary">Проверить НПА</Button>
    <Button variant="secondary">Открыть отчёт</Button>
    <Button variant="ghost">Отмена</Button>
  </div>
);

export const Sizes = () => (
  <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
    <Button size="sm">Маленькая</Button>
    <Button size="md">Средняя</Button>
    <Button size="lg">Большая</Button>
  </div>
);

export const AsLink = () => (
  <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
    <Button href="/analyze">Новый анализ</Button>
    <Button href="/reports" variant="secondary">История</Button>
  </div>
);

export const Disabled = () => (
  <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
    <Button disabled>Недоступно</Button>
    <Button variant="secondary" disabled>Недоступно</Button>
  </div>
);

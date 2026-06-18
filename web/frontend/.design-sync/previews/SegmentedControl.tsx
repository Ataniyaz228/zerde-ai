import { SegmentedControl } from "frontend";

export const Filter = () => (
  <SegmentedControl
    ariaLabel="Фильтр отчётов"
    value="all"
    onChange={() => {}}
    options={[
      { value: "all", label: "Все" },
      { value: "confirmed", label: "Подтверждено" },
      { value: "issues", label: "Спорные" },
    ]}
  />
);

export const Language = () => (
  <SegmentedControl
    ariaLabel="Язык интерфейса"
    size="sm"
    value="ru"
    onChange={() => {}}
    options={[
      { value: "ru", label: "RU" },
      { value: "kz", label: "KZ" },
    ]}
  />
);

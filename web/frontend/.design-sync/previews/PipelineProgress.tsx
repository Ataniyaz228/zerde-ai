import { PipelineProgress } from "frontend";

export const Running = () => (
  <div style={{ maxWidth: 560 }}>
    <PipelineProgress
      steps={[
        { id: "extract", name: "Извлечение тезисов", status: "done" },
        { id: "search", name: "Поиск в adilet.zan.kz", status: "done" },
        { id: "verify", name: "Сверка вердиктов", status: "active", message: "Проверка ст. 829-1 КоАП РК…" },
        { id: "report", name: "Сборка отчёта", status: "pending" },
      ]}
    />
  </div>
);

export const WithError = () => (
  <div style={{ maxWidth: 560 }}>
    <PipelineProgress
      steps={[
        { id: "extract", name: "Извлечение тезисов", status: "done" },
        { id: "search", name: "Поиск в adilet.zan.kz", status: "error", message: "Источник временно недоступен" },
        { id: "verify", name: "Сверка вердиктов", status: "pending" },
        { id: "report", name: "Сборка отчёта", status: "pending" },
      ]}
    />
  </div>
);

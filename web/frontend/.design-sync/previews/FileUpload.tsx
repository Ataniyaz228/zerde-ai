import { FileUpload } from "frontend";

// The empty drop zone looks the same idle vs. loading (isLoading only changes
// behaviour after a file is picked), so a single representative cell is the
// honest story.
export const DropZone = () => (
  <div style={{ maxWidth: 560 }}>
    <FileUpload onAnalyze={() => {}} isLoading={false} />
  </div>
);

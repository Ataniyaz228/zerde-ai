"use client";
import { useRef, useState, useCallback, DragEvent } from "react";
import { Upload, FileText, X, AlertTriangle } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { formatBytes } from "@/lib/format";
import s from "./FileUpload.module.css";

interface Props {
  onAnalyze: (file: File) => void;
  isLoading: boolean;
}

const ALLOWED_EXT = [".pdf", ".doc", ".docx", ".txt"];
const MAX_BYTES = 25 * 1024 * 1024;

export default function FileUpload({ onAnalyze, isLoading }: Props) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  const handleFile = useCallback((f: File) => {
    const ext = f.name.slice(f.name.lastIndexOf(".")).toLowerCase();
    if (!ALLOWED_EXT.includes(ext)) {
      setValidationError(t("upload_invalid_type"));
      setFile(null);
      return;
    }
    if (f.size > MAX_BYTES) {
      setValidationError(t("upload_too_large"));
      setFile(null);
      return;
    }
    setValidationError(null);
    setFile(f);
  }, [t]);

  const onDragOver = (e: DragEvent) => {
    e.preventDefault();
    setDragging(true);
  };
  const onDragLeave = () => setDragging(false);
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  };

  const zoneClass = [
    s.zone,
    dragging ? s.zoneDragging : "",
    file ? s.zoneSelected : "",
  ].filter(Boolean).join(" ");

  return (
    <div>
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.doc,.docx,.txt"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) handleFile(f);
        }}
      />

      <div
        className={zoneClass}
        onClick={() => !file && inputRef.current?.click()}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {!file ? (
          <>
            <div className={s.icon}>
              <Upload size={20} strokeWidth={1.5} />
            </div>
            <p className={s.dropText}>{t("upload_drop")}</p>
            <p className={s.orText}>{t("upload_or")}</p>
            <button
              className={s.browseBtn}
              onClick={(e) => { e.stopPropagation(); inputRef.current?.click(); }}
            >
              {t("upload_btn")}
            </button>
            <p className={s.hint}>{t("upload_hint")}</p>
            {validationError && (
              <p className={s.validationError}>
                <AlertTriangle size={13} strokeWidth={2} />
                {validationError}
              </p>
            )}
          </>
        ) : (
          <>
            <div className={s.fileInfo}>
              <div className={s.fileIconWrap}>
                <FileText size={18} strokeWidth={1.5} />
              </div>
              <div>
                <p className={s.fileLabel}>{t("upload_selected")}</p>
                <p className={s.fileName}>{file.name}</p>
                <p className={s.fileSize}>{formatBytes(file.size)}</p>
              </div>
            </div>

            <div className={s.actions}>
              <button
                className={`${s.startBtn} ${isLoading ? s.startBtnLoading : ""}`}
                onClick={(e) => { e.stopPropagation(); onAnalyze(file); }}
              >
                {isLoading ? (
                  <><div className={s.spinner} /> {t("upload_analyzing")}</>
                ) : (
                  t("upload_start")
                )}
              </button>
              <button
                className={s.changeBtn}
                onClick={(e) => {
                  e.stopPropagation();
                  setFile(null);
                  if (inputRef.current) inputRef.current.value = "";
                }}
              >
                <X size={14} />
                {t("upload_change")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

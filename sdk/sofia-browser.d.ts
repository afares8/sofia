export interface SofiaConfig {
  serviceId?: string;
  serviceName?: string;
  sofiaUrl?: string;
  environment?: string | null;
  release?: string | null;
  tags?: Record<string, unknown> | null;
  enabled?: boolean;
  slowMs?: number;
}

export function initSofia(options?: SofiaConfig): void;
export function reportReactError(
  error: unknown,
  errorInfo?: string,
  componentStack?: string,
): void;
export function captureError(
  message: string,
  detail?: string,
  level?: string,
): void;
export function addBreadcrumb(
  category: string,
  message: string,
  data?: Record<string, unknown>,
  level?: string,
): void;

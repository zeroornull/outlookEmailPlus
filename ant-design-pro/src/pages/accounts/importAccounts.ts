export type AccountImportPayload = {
  account_string: string;
  add_to_pool: boolean;
  duplicate_strategy: 'confirm' | 'overwrite';
  preserve_existing_group?: true;
  provider?: string;
  group_id?: unknown;
  imap_host?: unknown;
  imap_port?: unknown;
};

export type AccountImportResult =
  | { outcome: 'success'; response: Record<string, unknown> }
  | { outcome: 'cancelled' }
  | { outcome: 'failed'; message: string; response?: unknown };

const duplicateEmails = (data: unknown): string[] | null => {
  if (!data || typeof data !== 'object') return null;
  if (!('error' in data) || !('duplicates' in data)) return null;
  const error = data.error;
  const duplicates = data.duplicates;
  if (!error || typeof error !== 'object' || !('code' in error)) return null;
  if (error.code !== 'ACCOUNT_IMPORT_DUPLICATES' || !Array.isArray(duplicates)) {
    return null;
  }
  const emails = duplicates.filter(
    (item): item is string => typeof item === 'string' && item.trim().length > 0,
  );
  return emails.length === duplicates.length ? emails : null;
};

const conflictPayload = (error: unknown): unknown => {
  if (!error || typeof error !== 'object') return undefined;
  if ('response' in error) {
    const response = error.response;
    if (response && typeof response === 'object' && 'data' in response) {
      return response.data;
    }
  }
  if ('data' in error && duplicateEmails(error.data)) return error.data;
  return duplicateEmails(error) ? error : undefined;
};

const importSucceeded = (response: Record<string, unknown>): boolean => {
  if (response.success === false) return false;
  const summary = response.summary;
  if (!summary || typeof summary !== 'object') return response.success !== false;
  const imported = Number('imported' in summary ? summary.imported : 0);
  const updated = Number('updated' in summary ? summary.updated : 0);
  const applied =
    (Number.isFinite(imported) ? imported : 0) +
    (Number.isFinite(updated) ? updated : 0);
  const failed = Number('failed' in summary ? summary.failed : 0);
  const errors = Array.isArray(response.errors) ? response.errors.length : 0;
  const errorsTotal = Number('errors_total' in summary ? summary.errors_total : 0);
  if (applied === 0 && (failed > 0 || errors > 0 || errorsTotal > 0)) return false;
  if (applied === 0 && failed === 0 && errors === 0 && errorsTotal === 0) {
    const skipped = Number('skipped' in summary ? summary.skipped : 0);
    return skipped > 0;
  }
  return applied > 0;
};

export const formatAccountImportMessage = (
  response: Record<string, unknown>,
): string => {
  const base = typeof response.message === 'string' ? response.message : '导入成功';
  const summary = response.summary;
  if (!summary || typeof summary !== 'object' || !('mode' in summary) || summary.mode !== 'auto') {
    return base;
  }
  const byProvider = 'by_provider' in summary ? summary.by_provider : undefined;
  if (!byProvider || typeof byProvider !== 'object') return base;
  const parts = Object.entries(byProvider as Record<string, unknown>).map(
    ([provider, stats]) => {
      const row = stats && typeof stats === 'object' ? stats : {};
      const imported = 'imported' in row ? row.imported : 0;
      const skipped = 'skipped' in row && row.skipped ? `，跳过 ${row.skipped}` : '';
      const failed = 'failed' in row && row.failed ? `，失败 ${row.failed}` : '';
      return `${provider}: 成功 ${imported || 0}${skipped}${failed}`;
    },
  );
  return parts.length ? `${base}（${parts.join('；')}）` : base;
};

export async function submitAccountImport(input: {
  payload: AccountImportPayload;
  create: (body: AccountImportPayload) => Promise<unknown>;
  confirmOverwrite: (emails: string[]) => Promise<boolean>;
}): Promise<AccountImportResult> {
  const readResponse = async (body: AccountImportPayload) => {
    try {
      return { response: await input.create(body), thrown: undefined as unknown };
    } catch (error) {
      return { response: conflictPayload(error), thrown: error };
    }
  };

  const first = await readResponse(input.payload);
  let response = first.response;
  const duplicates =
    response && typeof response === 'object' ? duplicateEmails(response) : null;
  if (duplicates) {
    const overwrite = await input.confirmOverwrite(duplicates);
    if (!overwrite) return { outcome: 'cancelled' };
    const retry = await readResponse({
      ...input.payload,
      duplicate_strategy: 'overwrite',
      preserve_existing_group: true,
    });
    if (retry.thrown && !retry.response) {
      const messageText =
        retry.thrown instanceof Error ? retry.thrown.message : '导入失败';
      return { outcome: 'failed', message: messageText };
    }
    response = retry.response;
  } else if (first.thrown) {
    const messageText =
      first.thrown instanceof Error ? first.thrown.message : '导入失败';
    return { outcome: 'failed', message: messageText, response: first.response };
  }

  if (!response || typeof response !== 'object') {
    return { outcome: 'failed', message: '导入失败' };
  }
  const record = response as Record<string, unknown>;
  if (!importSucceeded(record)) {
    const messageText =
      typeof record.message === 'string' ? record.message : '导入失败';
    return { outcome: 'failed', message: messageText, response: record };
  }
  return { outcome: 'success', response: record };
}

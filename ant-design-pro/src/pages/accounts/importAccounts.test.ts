import { describe, expect, it, vi } from 'vitest';
import {
  formatAccountImportMessage,
  submitAccountImport,
  type AccountImportPayload,
} from './importAccounts';

const payload: AccountImportPayload = {
  account_string: 'user@example.com----secret',
  add_to_pool: false,
  duplicate_strategy: 'confirm',
  provider: 'outlook',
  group_id: 3,
};

const duplicateBody = {
  success: false,
  error: {
    code: 'ACCOUNT_IMPORT_DUPLICATES',
    message: '存在同名邮箱账号，请确认是否覆盖',
  },
  duplicates: ['user@example.com'],
};

describe('submitAccountImport', () => {
  it('asks before overwrite and retries the original payload once', async () => {
    const create = vi
      .fn()
      .mockRejectedValueOnce(
        Object.assign(new Error('存在同名邮箱账号，请确认是否覆盖'), {
          response: { data: duplicateBody },
          data: duplicateBody,
        }),
      )
      .mockResolvedValueOnce({
        success: true,
        message: '导入完成：成功 1 个，失败 0 个',
        summary: { imported: 1, updated: 1, failed: 0 },
      });
    const confirmOverwrite = vi.fn(async () => true);

    const result = await submitAccountImport({
      payload,
      create,
      confirmOverwrite,
    });

    expect(confirmOverwrite).toHaveBeenCalledWith(['user@example.com']);
    expect(create).toHaveBeenNthCalledWith(1, payload);
    expect(create).toHaveBeenNthCalledWith(2, {
      ...payload,
      duplicate_strategy: 'overwrite',
      preserve_existing_group: true,
    });
    expect(result.outcome).toBe('success');
  });

  it('keeps the import open when overwrite is cancelled', async () => {
    const create = vi.fn().mockResolvedValue(duplicateBody);
    const result = await submitAccountImport({
      payload,
      create,
      confirmOverwrite: async () => false,
    });

    expect(result).toEqual({ outcome: 'cancelled' });
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('surfaces a retry failure without treating it as success', async () => {
    const create = vi
      .fn()
      .mockResolvedValueOnce(duplicateBody)
      .mockRejectedValueOnce(new Error('导入失败'));

    const result = await submitAccountImport({
      payload,
      create,
      confirmOverwrite: async () => true,
    });

    expect(result.outcome).toBe('failed');
    if (result.outcome === 'failed') {
      expect(result.message).toBe('导入失败');
    }
  });

  it('does not claim success when nothing was imported and errors remain', async () => {
    const create = vi.fn().mockResolvedValue({
      success: true,
      message: '导入完成：成功 0 个，失败 1 个',
      summary: { imported: 0, failed: 1, errors_total: 1 },
      errors: [{ line: 1, email: 'user@example.com', error: '写入失败' }],
    });

    const result = await submitAccountImport({
      payload,
      create,
      confirmOverwrite: async () => true,
    });

    expect(result.outcome).toBe('failed');
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('counts an update with zero new imports as success', async () => {
    const response = {
      success: true,
      message: '导入完成：成功 1 个，失败 0 个',
      summary: { imported: 0, updated: 1, failed: 0 },
    };
    const result = await submitAccountImport({
      payload,
      create: vi.fn().mockResolvedValue(response),
      confirmOverwrite: async () => true,
    });

    expect(result).toEqual({ outcome: 'success', response });
  });
});

describe('formatAccountImportMessage', () => {
  it('includes provider counts from the server summary', () => {
    expect(
      formatAccountImportMessage({
        message: '混合导入完成：成功 1 个，跳过 0 个，失败 1 个',
        summary: {
          mode: 'auto',
          by_provider: {
            outlook: { imported: 1, skipped: 0, failed: 0 },
            imap: { imported: 0, skipped: 0, failed: 1 },
          },
        },
      }),
    ).toBe(
      '混合导入完成：成功 1 个，跳过 0 个，失败 1 个（outlook: 成功 1；imap: 成功 0，失败 1）',
    );
  });
});

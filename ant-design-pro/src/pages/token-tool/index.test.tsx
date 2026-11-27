import React from 'react';
import type * as Antd from 'antd';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  fetchConfig: vi.fn(),
  prepare: vi.fn(),
  writeText: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

vi.mock('@umijs/max', () => ({
  useIntl: () => ({ formatMessage: ({ defaultMessage }: { defaultMessage: string }) => defaultMessage }),
}));
vi.mock('@ant-design/pro-components', () => ({
  PageContainer: ({ children }: React.PropsWithChildren) => <div>{children}</div>,
  ProCard: ({ children }: React.PropsWithChildren) => <div>{children}</div>,
}));
vi.mock('antd', async (importOriginal) => ({
  ...(await importOriginal<typeof Antd>()),
  App: { useApp: () => ({ message: mocks.message }) },
}));
vi.mock('./TokenToolGuide', () => ({ default: () => null }));
vi.mock('@/services/outlook/tokenTool', () => ({
  fetchTokenToolConfig: mocks.fetchConfig,
  prepareOAuth: mocks.prepare,
  saveTokenToolConfig: vi.fn(),
  exchangeToken: vi.fn(),
  pickTokenToolError: (_error: unknown, fallback: string) => fallback,
}));

import TokenToolPage from './index';

const authorizeUrl = 'https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?state=test';

async function generateLink() {
  render(<TokenToolPage />);
  await waitFor(() => expect(mocks.fetchConfig).toHaveBeenCalledOnce());
  const button = await screen.findByRole('button', { name: '生成并复制授权链接' });
  await waitFor(() => expect(button).not.toHaveClass('ant-btn-loading'));
  fireEvent.click(button);
}

describe('TokenToolPage authorization link', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.fetchConfig.mockResolvedValue({ success: true, data: { client_id: 'test-client' } });
    mocks.prepare.mockResolvedValue({ success: true, data: { authorize_url: authorizeUrl } });
    mocks.writeText.mockResolvedValue(undefined);
    vi.stubGlobal('navigator', Object.create(navigator, {
      clipboard: { value: { writeText: mocks.writeText }, configurable: true },
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('generates, displays and copies the returned authorization URL', async () => {
    await generateLink();

    await waitFor(() => expect(mocks.writeText).toHaveBeenCalledExactlyOnceWith(authorizeUrl));
    expect(screen.getByDisplayValue(authorizeUrl)).toBeVisible();
    expect(mocks.prepare).toHaveBeenCalledOnce();
    expect(mocks.message.success).toHaveBeenCalledWith('授权链接已生成并复制');
    expect(mocks.message.error).not.toHaveBeenCalled();
  });

  it('retains the link and allows a manual copy retry when automatic copying is rejected', async () => {
    mocks.writeText.mockRejectedValueOnce(new Error('Clipboard denied'));
    await generateLink();

    await waitFor(() => expect(mocks.message.warning).toHaveBeenCalledOnce());
    expect(screen.getByDisplayValue(authorizeUrl)).toBeVisible();
    expect(mocks.message.error).not.toHaveBeenCalled();
    expect(mocks.message.success).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /^复\s*制$/ }));
    await waitFor(() => expect(mocks.writeText).toHaveBeenCalledTimes(2));
    expect(mocks.writeText).toHaveBeenLastCalledWith(authorizeUrl);
    await waitFor(() => expect(mocks.message.success).toHaveBeenCalledWith('已复制'));
  });

  it.each([
    ['an unsuccessful response', () => mocks.prepare.mockResolvedValue({ success: false })],
    ['a rejected request', () => mocks.prepare.mockRejectedValue(new Error('Request failed'))],
    ['an empty URL', () => mocks.prepare.mockResolvedValue({ success: true, data: { authorize_url: '' } })],
  ])('does not copy after %s', async (_description, configure) => {
    configure();
    await generateLink();

    await waitFor(() => expect(mocks.message.error).toHaveBeenCalledOnce());
    expect(mocks.writeText).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue(authorizeUrl)).not.toBeInTheDocument();
  });
});
